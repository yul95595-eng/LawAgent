"""Locust 压测脚本（阶段 4 运维）—— 重点压 SSE 流式对话。

准备：
    pip install -r requirements-ops.txt
    # 压测要把限流放宽，否则 30 并发全撞 429，压出来的是限流器不是系统：
    #   启动后端前设 CHAT_MAX_PER_MIN=100000、AUTH_IP_MAX_PER_MIN=100000

跑：
    locust -f scripts/locustfile.py --host http://127.0.0.1:9089
    # 无头模式（CI/一次性出报告）：50 用户、起 10/s、跑 3 分钟
    locust -f scripts/locustfile.py --host http://127.0.0.1:9089 \
           --headless -u 50 -r 10 -t 3m --only-summary

看什么：
- `/api/v1/chat/stream` 的 P95 与失败率（SSE 首字节延迟远小于整轮耗时，
  所以「首事件到达」和「整轮结束」分开统计）；
- 中途断流会记成 failure（流没读到 done 事件）；
- 真实 LLM 模式单并发就几秒一轮，压测建议先用 LLM_MOCK=1 看框架容量，
  再开真实 key 看下游配额（DashScope 有 QPS 限制，瓶颈通常在那里）。
"""
from __future__ import annotations

import json
import os
import random
import time

from locust import HttpUser, between, events, task

DEMO_PASSWORD = os.getenv("LAWAGENT_DEMO_PASSWORD", "law123456")
DEMO_USER = os.getenv("LAWAGENT_DEMO_USER", "L1001")

CASE_TEXT = (
    "我在某科技公司工作 4 年 3 个月，月薪 15000 元，2026 年 9 月 30 日公司以组织架构调整为由"
    "单方解除劳动合同，未提前 30 日通知也未支付代通知金，也没有支付任何经济补偿。"
    "请问能否主张违法解除赔偿金？大概能拿多少钱？仲裁时效多长？"
)
CONTRACT_TEXT = (
    "劳动合同\n第一条 合同期限 1 年。\n第二条 试用期 6 个月，试用期工资为转正工资的 70%。\n"
    "第三条 竞业限制：离职后 5 年内不得从事同行业工作，公司不支付经济补偿。\n"
    "第四条 劳动者提前离职须支付违约金 10 万元。"
)


class LawyerUser(HttpUser):
    """模拟律师侧典型流量：多数是对话，少数直达接口。"""

    wait_time = between(1, 3)
    token: str = ""

    def on_start(self):
        r = self.client.post(
            "/api/v1/auth/login",
            json={"user_no": DEMO_USER, "password": DEMO_PASSWORD},
            name="/api/v1/auth/login",
        )
        if r.status_code != 200:
            raise RuntimeError(f"压测登录失败：{r.status_code} {r.text[:120]}")
        self.token = r.json()["token"]

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    # ── 主链路：SSE 流式对话（权重最高）──
    @task(5)
    def chat_stream(self):
        payload = {"message": random.choice([CASE_TEXT, CONTRACT_TEXT]), "attachments": None}
        if "劳动合同" in payload["message"]:
            payload["attachments"] = {"contract_text": CONTRACT_TEXT}

        started = time.perf_counter()
        first_event = None
        saw_done = False
        with self.client.post(
            "/api/v1/chat/stream", json=payload, headers=self.headers,
            stream=True, name="/api/v1/chat/stream", catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            try:
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    if first_event is None:
                        first_event = time.perf_counter() - started
                    line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
                    if line.startswith("event: done"):
                        saw_done = True
                        break
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"流读取异常：{exc}")
                return

        total = time.perf_counter() - started
        if not saw_done:
            resp.failure("未收到 done 事件（流提前中断）")
            return
        # 首事件延迟单独打点，便于和整轮耗时区分看
        if first_event is not None:
            events.request.fire(
                request_type="SSE", name="/api/v1/chat/stream::first_event",
                response_time=first_event * 1000, response_length=0, exception=None,
            )
        resp.success()
        _ = total

    @task(2)
    def case_analyze(self):
        self.client.post(
            "/api/v1/cases/analyze", json={"case_text": CASE_TEXT},
            headers=self.headers, name="/api/v1/cases/analyze",
        )

    @task(1)
    def compliance_check(self):
        self.client.post(
            "/api/v1/compliance/check",
            json={"enterprise_name": "华创精密制造股份有限公司",
                  "enterprise_text": "该公司拟开展对外股权投资与货物买卖业务。"},
            headers=self.headers, name="/api/v1/compliance/check",
        )

    @task(2)
    def law_search(self):
        q = random.choice(["试用期", "违法解除", "竞业限制", "二倍工资"])
        self.client.get(
            f"/api/v1/laws/search?q={q}&top_k=5",
            headers=self.headers, name="/api/v1/laws/search",
        )

    @task(1)
    def health(self):
        self.client.get("/api/health", name="/api/health")


@events.test_start.add_listener
def _on_start(environment, **_kwargs):  # pragma: no cover - 压测试运行时才走
    print(json.dumps({
        "hint": "压测开始",
        "提醒": "后端若开着默认限流（chat 10 次/分钟/用户），并发一上来就会 429；"
                "压测请用 CHAT_MAX_PER_MIN=100000 启动后端",
    }, ensure_ascii=False))
