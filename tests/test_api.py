"""API 层测试 —— JWT 登录 / SSE 全流程（含 HitL interrupt → 合伙人确认）/ 权限门禁 / 直达接口。"""
from __future__ import annotations

import json

import httpx
import pytest

from backend.memory.models import ContractReview, SessionLocal


pytestmark = pytest.mark.usefixtures("_prepare_db")


async def _make_client():
    from backend.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login(client: httpx.AsyncClient, user_no: str, password: str = "law123456") -> dict:
    r = await client.post("/api/v1/auth/login", json={"user_no": user_no, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _parse_sse(client: httpx.AsyncClient, token: str, payload: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", "/api/v1/chat/stream", json=payload, headers=await _auth_headers(token)
    ) as resp:
        assert resp.status_code == 200
        event, data = "", ""
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
                if event:
                    events.append((event, json.loads(data)))
                    event, data = "", ""
    return events


class TestAuth:
    async def test_health(self):
        async with await _make_client() as client:
            r = await client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "ok"
            assert body["agents"]["total_nodes"] == 26
            assert body["knowledge_base"]["precedents"] >= 60

    async def test_register_login_me(self):
        """注册审核流：注册落库 pending（不发 token）→ 合伙人放行 → 登录成功。"""
        async with await _make_client() as client:
            r = await client.post("/api/v1/auth/register", json={
                "user_no": "T9001", "username": "测试律师", "password": "test123456",
                "role": "lawyer",
            })
            assert r.status_code == 200
            assert r.json()["status"] == "pending"
            # 未放行不能登录
            r = await client.post("/api/v1/auth/login",
                                  json={"user_no": "T9001", "password": "test123456"})
            assert r.status_code == 403
            # 合伙人放行
            p = await _login(client, "P2001")
            r = await client.get("/api/v1/auth/pending",
                                 headers=await _auth_headers(p["token"]))
            pend = [u for u in r.json()["users"] if u["user_no"] == "T9001"]
            assert pend, "T9001 应在待审列表"
            r = await client.post(f"/api/v1/auth/approve/{pend[0]['user_id']}",
                                  headers=await _auth_headers(p["token"]))
            assert r.status_code == 200
            # 放行后登录成功，响应含双令牌
            r = await client.post("/api/v1/auth/login",
                                  json={"user_no": "T9001", "password": "test123456"})
            assert r.status_code == 200
            body = r.json()
            assert body["token"] and body["refresh_token"]
            r = await client.get("/api/v1/auth/me", headers=await _auth_headers(body["token"]))
            assert r.status_code == 200
            assert r.json()["user"]["user_no"] == "T9001"

    async def test_register_partner_role_blocked(self):
        """防自封审核权：自助注册不允许 partner/admin 角色。"""
        async with await _make_client() as client:
            r = await client.post("/api/v1/auth/register", json={
                "user_no": "BAD001", "username": "越权者", "password": "test123456",
                "role": "partner",
            })
            assert r.status_code == 422

    async def test_login_demo_users(self):
        async with await _make_client() as client:
            for user_no, role in (("L1001", "lawyer"), ("P2001", "partner"), ("L3001", "legal")):
                body = await _login(client, user_no)
                assert body["user"]["role"] == role

    async def test_wrong_password(self):
        async with await _make_client() as client:
            r = await client.post("/api/v1/auth/login",
                                  json={"user_no": "L1001", "password": "badbadbad"})
            assert r.status_code == 401

    async def test_unauthorized_blocked(self):
        async with await _make_client() as client:
            r = await client.get("/api/v1/chat/threads")
            assert r.status_code == 401


class TestChatSSE:
    async def test_limitation_inquiry_full_flow(self):
        """demo 前置：纯咨询无文书 → meta → node… → result → done，线程与消息落库。"""
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            events = await _parse_sse(
                client, token,
                {"message": "劳动仲裁时效是多久？过了还能仲裁吗"},
            )
        kinds = [k for k, _ in events]
        assert kinds[0] == "meta"
        assert kinds[-1] == "done"
        assert "result" in kinds
        meta = events[0][1]
        assert meta["intents"] == ["limitation_inquiry"]
        result = next(p for k, p in events if k == "result")
        assert result["result"]["report"]["kind"] == "limitation_inquiry"
        assert "1 年" in result["result"]["report"]["answer"]

    async def test_hitl_flow_via_api(self):
        """核心链路：SSE 高风险合同 → interrupt → 合伙人待审列表 → confirm → approved。"""
        async with await _make_client() as client:
            lawyer = (await _login(client, "L1001"))["token"]
            contract_text = (
                "劳动合同\n甲方：某某科技有限公司；乙方：李某。\n"
                "第一条 合同期限：固定期限1年。\n"
                "第二条 试用期：试用期6个月。\n"
                "第三条 工作内容：研发工程师，工作地点杭州。\n"
                "第四条 工作时间：标准工时制。\n"
                "第五条 劳动报酬：月薪15000元。\n"
                "第六条 社会保险：依法缴纳社会保险。\n"
                "第七条 竞业限制：乙方离职后5年内不得入职竞争企业。\n"
                "第八条 服务期违约金：违反服务期约定支付违约金100000元。\n"
            )
            events = await _parse_sse(
                client, lawyer,
                {"message": "请审查这份合同", "attachments": {"contract_text": contract_text}},
            )
            kinds = [k for k, _ in events]
            assert "interrupt" in kinds
            assert "result" not in kinds  # HitL 挂起，本轮不产结果卡
            intr = next(p for k, p in events if k == "interrupt")
            review_id = intr["review_id"]
            assert review_id and intr["risk_level"] == "high"
            thread_id = events[0][1]["thread_id"]

            # 线程状态 waiting_review
            r = await client.get(f"/api/v1/chat/threads/{thread_id}",
                                 headers=await _auth_headers(lawyer))
            assert r.json()["thread"]["status"] == "waiting_review"

            # 律师无权审核（403 门禁）
            r = await client.post(f"/api/v1/reviews/{review_id}/confirm",
                                  json={"comment": "ok"},
                                  headers=await _auth_headers(lawyer))
            assert r.status_code == 403

            # 合伙人待审列表可见 + 确认
            partner = (await _login(client, "P2001"))["token"]
            r = await client.get("/api/v1/reviews/pending", headers=await _auth_headers(partner))
            pending_ids = [x["review_id"] for x in r.json()["reviews"]]
            assert review_id in pending_ids

            r = await client.post(f"/api/v1/reviews/{review_id}/confirm",
                                  json={"comment": "同意出具"},
                                  headers=await _auth_headers(partner))
            assert r.status_code == 200
            card = r.json()["card"]
            assert card["result"]["review_status"] == "approved"

            # 记录 approved、线程 done、律师线程里补了审核结论消息
            async with SessionLocal() as session:
                row = await session.get(ContractReview, review_id)
                assert row.status == "approved" and row.reviewer_id
            r = await client.get(f"/api/v1/chat/threads/{thread_id}",
                                 headers=await _auth_headers(lawyer))
            msgs = r.json()["messages"]
            assert any("合伙人已确认" in m["content"] for m in msgs)
            assert r.json()["thread"]["status"] == "done"


class TestDirectApis:
    async def test_case_analyze_direct(self):
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.post("/api/v1/cases/analyze", json={
                "case_text": "劳动者王某2019年3月入职，月薪2万元，工作年限3年2个月，2022年5月被公司口头辞退。",
            }, headers=await _auth_headers(token))
            assert r.status_code == 200
            body = r.json()
            claim = body["card"]["result"]["facts"]["claim_amount"]
            assert claim["amount"] == 140000.0
            case_id = body["card"]["result"]["case_id"]
            r = await client.get(f"/api/v1/cases/{case_id}", headers=await _auth_headers(token))
            assert r.status_code == 200

    async def test_compliance_check_direct(self):
        async with await _make_client() as client:
            token = (await _login(client, "L3001"))["token"]
            r = await client.post("/api/v1/compliance/check", json={
                "enterprise_name": "华创精密制造股份有限公司",
                "enterprise_text": "华创精密制造股份有限公司拟对外股权投资，请做合规体检。",
            }, headers=await _auth_headers(token))
            assert r.status_code == 200
            body = r.json()
            assert body["card"]["result"]["citation_check"]["accuracy"] == 1.0

    async def test_stats_and_law_search(self):
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.get("/api/v1/stats/overview", headers=await _auth_headers(token))
            assert r.status_code == 200
            r = await client.get("/api/v1/laws/search",
                                 params={"q": "试用期", "top_k": 3},
                                 headers=await _auth_headers(token))
            assert r.status_code == 200
            assert r.json()["results"]
