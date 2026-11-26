"""FastAPI 应用 —— LawAgent 智能法律咨询 Agent 系统。

接口总览（31 个 RESTful + Prometheus 指标）：
  GET  /api/health                              健康检查
  GET  /metrics                                 Prometheus 指标（可配 METRICS_TOKEN）
  POST /api/v1/auth/register                    工号注册（审核流：落库待审）
  POST /api/v1/auth/login                       工号登录（双令牌 JWT + 限流）
  POST /api/v1/auth/refresh                     刷新令牌（refresh 换新双令牌）
  GET  /api/v1/auth/pending                     待审核账号列表（合伙人）
  POST /api/v1/auth/approve/{uid}               放行注册申请（合伙人）
  POST /api/v1/auth/reject/{uid}                拒绝注册申请（合伙人）
  GET  /api/v1/auth/me                          当前用户
  POST /api/v1/chat/stream                      SSE 流式对话（意图路由+节点进度）
  GET  /api/v1/chat/threads                     历史任务列表
  GET  /api/v1/chat/threads/{tid}               线程详情回溯
  DELETE /api/v1/chat/threads/{tid}             删除线程
  GET  /api/v1/reviews/pending                  待审意见书列表（合伙人）
  POST /api/v1/reviews/{rid}/confirm            合伙人确认（resume 图）
  POST /api/v1/reviews/{rid}/reject             合伙人退回（回写意见节点）
  POST /api/v1/cases/analyze                    直达案件分析
  POST /api/v1/cases/faq                        时效咨询直达（案件域 FAQ 小图）
  GET  /api/v1/cases                            案件分析列表
  GET  /api/v1/cases/{cid}                      案件分析详情
  POST /api/v1/contracts/review                 直达合同审查
  GET  /api/v1/contracts                        审查记录列表
  GET  /api/v1/contracts/{rid}                  审查记录详情
  POST /api/v1/compliance/check                 直达合规体检/意见书
  GET  /api/v1/compliance                       合规档案列表
  GET  /api/v1/compliance/{pid}                 合规档案详情
  GET  /api/v1/stats/overview                   风险看板统计
  GET  /api/v1/laws/search                      法条库检索
  POST /api/v1/files/parse                      文件解析（docx/pdf/txt/md → 正文）
  GET  /api/v1/reviews/{rid}/export             意见书导出 Word
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from backend.core import metrics
from backend.core.config import settings
from backend.memory.models import init_db
from backend.api.v1 import auth, cases, chat, compliance, contracts, files, reviews, stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("数据库初始化完成（%s）", "SQLite 开发态" if settings.use_sqlite else "PostgreSQL 生产态")
    # 持久化 Checkpointer（sqlite/postgres）：先建 saver，再重编译三图，
    # 合伙人 HitL 挂起状态跨重启可恢复；memory 模式行为与历史一致。
    from backend.memory.checkpointer import close_checkpointer, setup_checkpointer
    saver = await setup_checkpointer()
    if saver is not None:
        from backend.orchestrator.orchestrator import rebuild_agents
        rebuild_agents()
        logger.info("三个 Agent 已用持久化 Checkpointer 重编译（HITL 跨重启可恢复）")
    yield
    await close_checkpointer()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 内网演示环境；生产收敛为律所域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, chat.router, reviews.router, cases.router,
          contracts.router, compliance.router, stats.router, files.router):
    app.include_router(r, prefix="/api/v1")


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """HTTP 层埋点：请求量 / 耗时 / 状态码。

    路由标签取 FastAPI 解析出的 route.path 模板（如 `/api/v1/reviews/{rid}/export`），
    不取真实 URL —— 否则每个 review_id 都会生成一条独立时序，Prometheus 会爆。
    /metrics 自身不计数，避免抓取流量污染业务指标。
    """
    if not settings.metrics_enabled or request.url.path == "/metrics":
        return await call_next(request)

    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        metrics.observe(
            "lawagent_http_request_duration_seconds", time.perf_counter() - started,
            {"method": request.method, "path": path},
            help_text="HTTP 请求耗时（秒）",
        )
        metrics.inc(
            "lawagent_http_requests_total",
            {"method": request.method, "path": path, "status": str(status_code)},
            help_text="HTTP 请求总数（按路由模板/状态码）",
        )


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request, token: str = ""):
    """Prometheus 抓取端点（text/plain; version=0.0.4）。

    配了 METRICS_TOKEN 时要求 `Authorization: Bearer <token>` 或 `?token=`；
    生产建议放在内网/反代白名单后面，别直接暴露到公网。
    """
    if settings.metrics_token:
        auth_header = request.headers.get("authorization", "")
        given = token or (auth_header[7:] if auth_header.lower().startswith("bearer ") else "")
        if given != settings.metrics_token:
            raise HTTPException(status_code=401, detail="指标端点需要有效令牌")
    return PlainTextResponse(metrics.render(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/health")
async def health():
    from backend.retrieval.precedents import stats as precedent_stats
    from backend.retrieval.statutes import load_statutes
    from backend.tools.adapters import get_enterprise_adapter

    arts = load_statutes()
    return {
        "status": "ok",
        "app": settings.app_name,
        "llm_mode": "llm" if settings.llm_enabled else "mock（未配 Key，规则降级）",
        "checkpointer": settings.checkpointer_backend,
        "knowledge_base": {
            "statute_articles": len(arts),
            "statute_laws": len({a.law for a in arts}),
            "precedents": precedent_stats()["total"],
        },
        # 阶段3 数据接真：当前实际生效的数据源（mock 会明确标出来）
        "data_sources": {
            "enterprise_credit": get_enterprise_adapter().name,
            "precedent": settings.precedent_data_source if settings.precedent_api_url
            else "local",
        },
        "agents": {"case": 11, "contract": 8, "compliance": 6, "case_faq": 1, "total_nodes": 26},
    }
