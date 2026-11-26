"""chat 路由 —— SSE 流式对话 + 历史任务（左侧列表）+ HitL 中断事件。

SSE 事件协议（前端 fetch-ReadableStream 解析）：
  meta       {thread_id, intents, agents}        会话元信息
  node       {agent, intent, node, label}        28 节点执行进度
  interrupt  {review_id, contract_title, ...}    合伙人审核挂起（HitL）
  result     AgentResult 卡片                     结构化结果
  done       {}                                   本轮结束
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sse_starlette.sse import EventSourceResponse

from backend.api.audit import audit
from backend.api.deps import get_current_user
from backend.core import metrics
from backend.core.ratelimit import rate_limit_by_user
from backend.core.security import new_id
from backend.memory.models import (
    ChatThread,
    ContractReview,
    SessionLocal,
    ThreadMessage,
)
from backend.orchestrator.orchestrator import (
    INTENT_ROUTES,
    classify_intents,
    resolve_risk_assessment_target,
    stream_pipeline,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    attachments: dict | None = None   # {contract_text / case_text}：随消息附带的材料正文


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


@router.post("/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    rate_limit_by_user(user["user_id"], "chat")  # 阶段2：对话接口 10 次/分钟/用户
    thread_id = req.thread_id or new_id("th")

    async def _gen():
        intents = await classify_intents(req.message)
        metrics.inc("lawagent_chat_streams_total", {"intent": intents[0] if intents else "unknown"},
                    help_text="对话流请求数（按首个意图）")
        agents_raw = sorted({INTENT_ROUTES.get(i, "case") for i in intents})
        agents = [resolve_risk_assessment_target(req.message) if a == "dual" else a for a in agents_raw]
        yield _sse("meta", {"thread_id": thread_id, "intents": intents, "agents": agents})

        # 落库：线程 + 用户消息（左侧历史任务列表数据源）
        async with SessionLocal() as session:
            thread = await session.get(ChatThread, thread_id)
            if thread is None:
                session.add(ChatThread(
                    id=thread_id, tenant_id=user["tenant_id"], user_id=user["user_id"],
                    title=req.message[:24] or "新咨询",
                    intents=",".join(intents), status="active",
                ))
            else:
                thread.intents = ",".join(dict.fromkeys((thread.intents or "").split(",") + intents))
            session.add(ThreadMessage(
                id=new_id("msg"), thread_id=thread_id, role="user", content=req.message
            ))
            await session.commit()
        await audit(user["tenant_id"], user["user_id"], user["role"], "chat", "thread", thread_id,
                    {"intents": intents})

        # 闲聊/元问题：不跑业务流水线，直出自我介绍
        if intents == ["chitchat"]:
            from backend.orchestrator.orchestrator import _CHITCHAT_REPLY
            async with SessionLocal() as session:
                session.add(ThreadMessage(
                    id=new_id("msg"), thread_id=thread_id, role="assistant", content=_CHITCHAT_REPLY,
                ))
                thread = await session.get(ChatThread, thread_id)
                if thread:
                    thread.status = "done"
                    thread.updated_at = _now()
                await session.commit()
            yield _sse("result", {
                "agent": "system", "intent": "chitchat", "title": "自我介绍",
                "summary": _CHITCHAT_REPLY, "result": {}, "citations": [],
                "audit_hint": "法律服务请描述案情 / 粘贴合同条款 / 输入企业名称",
            })
            yield _sse("done", {})
            return

        final_summaries: list[str] = []
        async for kind, payload in stream_pipeline(
            intents, req.message, thread_id=thread_id,
            tenant_id=user["tenant_id"], user_id=user["user_id"],
            attachments=req.attachments,
        ):
            if kind == "node":
                # 节点执行进度：前端 Chat.vue 用它画时间线（此前只处理了
                # interrupt/result，node 事件被静默丢弃，进度条一直是空的）
                yield _sse("node", payload)
                continue
            if kind == "interrupt":
                # HitL：登记「待审意见书」，线程置 waiting_review
                review_id = payload.get("review_id", "")
                async with SessionLocal() as session:
                    row = await session.get(ContractReview, review_id)
                    if row is None:
                        session.add(ContractReview(
                            review_id=review_id,
                            thread_id=thread_id,
                            tenant_id=user["tenant_id"],
                            user_id=user["user_id"],
                            contract_title=payload.get("contract_title", ""),
                            risks_json=json.dumps(payload.get("risk_points", []), ensure_ascii=False),
                            risk_level=payload.get("risk_level", "high"),
                            review_opinion=payload.get("review_opinion", ""),
                            format_check_json=json.dumps(payload.get("format_check", {}), ensure_ascii=False),
                            status="pending_review",
                        ))
                    else:
                        row.status = "pending_review"
                    thread = await session.get(ChatThread, thread_id)
                    if thread:
                        thread.status = "waiting_review"
                        thread.updated_at = _now()
                    await session.commit()
                await audit(user["tenant_id"], user["user_id"], user["role"],
                            "hitl_interrupt", "contract_review", review_id,
                            {"risk_level": payload.get("risk_level")})
                yield _sse("interrupt", payload)
                return
            if kind == "result":
                final_summaries.append(payload.get("summary", ""))
                async with SessionLocal() as session:
                    session.add(ThreadMessage(
                        id=new_id("msg"), thread_id=thread_id, role="assistant",
                        content=payload.get("summary", ""),
                        result_json=json.dumps(payload, ensure_ascii=False),
                    ))
                    thread = await session.get(ChatThread, thread_id)
                    if thread:
                        thread.status = "done"
                        thread.updated_at = _now()
                    await session.commit()
                yield _sse("result", payload)
        yield _sse("done", {})

    async def gen():
        """SSE 生成器包一层：流内异常也要能被指标看见。

        SSE 一旦开始输出，HTTP 状态码就固定成 200 了 —— 节点中途炸掉，
        监控里只能靠这个计数发现「本次对话没跑完」。
        """
        try:
            async for event in _gen():
                yield event
        except Exception:
            metrics.inc("lawagent_chat_stream_errors_total",
                        help_text="对话流中途异常次数（SSE 已返回 200，靠此指标发现）")
            logger.exception("chat_stream 执行失败 thread_id=%s", thread_id)
            raise

    return EventSourceResponse(gen())


@router.get("/threads")
async def list_threads(user: dict = Depends(get_current_user)):
    """历史任务列表（过去 1 年可回溯口径）。"""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ChatThread)
            .where(ChatThread.tenant_id == user["tenant_id"], ChatThread.user_id == user["user_id"])
            .order_by(ChatThread.created_at.desc(), ChatThread.id.desc())
            .limit(100)
        )).scalars().all()
        return {"threads": [
            {
                "id": t.id, "title": t.title, "intents": [i for i in t.intents.split(",") if i],
                "status": t.status, "created_at": t.created_at, "updated_at": t.updated_at,
            } for t in rows
        ]}


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str, user: dict = Depends(get_current_user)):
    """线程详情（含全部消息与结果卡，支持回溯）。"""
    async with SessionLocal() as session:
        thread = await session.get(ChatThread, thread_id)
        if thread is None or thread.tenant_id != user["tenant_id"] or thread.user_id != user["user_id"]:
            return {"thread": None, "messages": []}
        msgs = (await session.execute(
            select(ThreadMessage).where(ThreadMessage.thread_id == thread_id).order_by(ThreadMessage.created_at, ThreadMessage.id)
        )).scalars().all()
        return {
            "thread": {"id": thread.id, "title": thread.title, "intents": thread.intents.split(","), "status": thread.status},
            "messages": [
                {
                    "id": m.id, "role": m.role, "content": m.content,
                    "result": json.loads(m.result_json) if m.result_json else None,
                    "created_at": m.created_at,
                } for m in msgs
            ],
        }


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, user: dict = Depends(get_current_user)):
    async with SessionLocal() as session:
        thread = await session.get(ChatThread, thread_id)
        if thread is None or thread.tenant_id != user["tenant_id"] or thread.user_id != user["user_id"]:
            return {"deleted": False}
        await session.execute(delete(ThreadMessage).where(ThreadMessage.thread_id == thread_id))
        await session.delete(thread)
        await session.commit()
    await audit(user["tenant_id"], user["user_id"], user["role"], "delete", "thread", thread_id)
    return {"deleted": True}
