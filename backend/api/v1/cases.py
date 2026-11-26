"""cases 路由 —— 直达案件分析 + 分析记录查询（永久保存口径）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.audit import audit
from backend.api.deps import get_current_user
from backend.memory.models import CaseAnalysis, SessionLocal
from backend.orchestrator.orchestrator import _AGENTS, _config, build_result_card

router = APIRouter(prefix="/cases", tags=["cases"])


class CaseRequest(BaseModel):
    case_text: str
    case_id: str = ""            # 外部案件管理系统案件号（可选）
    focus_case_type: str = ""    # 指定案由（可选）
    thread_id: str = ""


class LimitationFaqRequest(BaseModel):
    question: str                # 时效咨询问题（如「劳动仲裁时效是多久」）
    thread_id: str = ""


@router.post("/analyze")
async def analyze_case(req: CaseRequest, user: dict = Depends(get_current_user)):
    """直达 CaseAgent（不经意图识别，接口级调用场景）。"""
    thread_id = req.thread_id or f"th_case_{req.case_id or 'direct'}"
    state = {
        "thread_id": thread_id, "tenant_id": user["tenant_id"], "user_id": user["user_id"],
        "case_text": req.case_text, "case_id": req.case_id,
        "focus_case_type": req.focus_case_type, "pipeline_context": {},
    }
    final = await _AGENTS["case"].ainvoke(state, config=_config(thread_id))
    card = build_result_card("case", "case_analysis", final)
    await audit(user["tenant_id"], user["user_id"], user["role"], "case_analyze", "case",
                final.get("saved_case_id", ""))
    return {"card": card, "summary": final.get("summary", "")}


@router.post("/faq")
async def limitation_faq(req: LimitationFaqRequest, user: dict = Depends(get_current_user)):
    """时效咨询直达：案件域 FAQ 小图（规则直出 + 引用回库验真，不落库）。"""
    thread_id = req.thread_id or "th_faq_direct"
    state = {
        "thread_id": thread_id, "tenant_id": user["tenant_id"], "user_id": user["user_id"],
        "inquiry_text": req.question, "pipeline_context": {},
    }
    final = await _AGENTS["case_faq"].ainvoke(state, config=_config(thread_id))
    card = build_result_card("case_faq", "limitation_inquiry", final)
    return {"card": card, "summary": final.get("summary", "")}


@router.get("")
async def list_cases(user: dict = Depends(get_current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(CaseAnalysis)
            .where(CaseAnalysis.tenant_id == user["tenant_id"], CaseAnalysis.user_id == user["user_id"])
            .order_by(CaseAnalysis.created_at.desc()).limit(100)
        )).scalars().all()
        return {"cases": [
            {
                "case_id": r.case_id, "case_title": r.case_title, "case_type": r.case_type,
                "limitation_text": r.limitation_text, "win_rate": r.win_rate,
                "win_rate_reason": r.win_rate_reason, "created_at": r.created_at,
            } for r in rows
        ]}


@router.get("/{case_id}")
async def get_case(case_id: str, user: dict = Depends(get_current_user)):
    async with SessionLocal() as session:
        r = await session.get(CaseAnalysis, case_id)
        if r is None or r.tenant_id != user["tenant_id"] or r.user_id != user["user_id"]:
            return {"case": None}
        return {
            "case": {
                "case_id": r.case_id, "case_title": r.case_title, "case_type": r.case_type,
                "facts": json.loads(r.case_facts_json or "{}"),
                "applicable_law": json.loads(r.applicable_law_json or "[]"),
                "precedents": json.loads(r.precedents_json or "[]"),
                "issues": json.loads(r.issues_json or "[]"),
                "limitation_text": r.limitation_text,
                "win_rate": r.win_rate, "win_rate_reason": r.win_rate_reason,
                "framework": json.loads(r.framework_json or "{}"),
                "created_at": r.created_at,
            }
        }
