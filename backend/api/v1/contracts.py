"""contracts 路由 —— 直达合同审查 + 审查记录查询。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.audit import audit
from backend.api.deps import get_current_user
from backend.memory.models import ContractReview, SessionLocal
from backend.orchestrator.orchestrator import _AGENTS, _config, build_result_card

router = APIRouter(prefix="/contracts", tags=["contracts"])


class ContractRequest(BaseModel):
    contract_text: str = ""
    contract_id: str = ""       # 外部合同管理系统文件号（可选，优先取档）
    contract_title: str = ""
    clause_focus: str = ""      # 条款分析意图：聚焦单条款
    thread_id: str = ""


@router.post("/review")
async def review_contract(req: ContractRequest, user: dict = Depends(get_current_user)):
    """直达 ContractAgent。高风险 → 图 interrupt → 返回 pending 状态。"""
    if not req.contract_text.strip() and not req.contract_id:
        return {"card": None, "summary": "请提供合同正文或合同管理系统文件号"}
    thread_id = req.thread_id or f"th_ctr_{req.contract_id or 'direct'}"
    state = {
        "thread_id": thread_id, "tenant_id": user["tenant_id"], "user_id": user["user_id"],
        "contract_text": req.contract_text, "contract_id": req.contract_id,
        "contract_title": req.contract_title, "clause_focus": req.clause_focus,
        "pipeline_context": {},
    }
    final = await _AGENTS["contract"].ainvoke(state, config=_config(thread_id))
    card = build_result_card("contract", "contract_review", final)
    await audit(user["tenant_id"], user["user_id"], user["role"], "contract_review",
                "contract_review", final.get("review_id", ""),
                {"risk_level": final.get("risk_level", "")})
    # interrupt 发生时 ainvoke 结果带 "__interrupt__" 键；此路径未登记
    # pending_review（登记逻辑在 chat SSE），接口直达场景这里兜底登记
    interrupted = bool(final.get("__interrupt__"))
    review_id = final.get("review_id", "")
    if interrupted and review_id:
        async with SessionLocal() as session:
            existing = await session.get(ContractReview, review_id)
            if existing is None:
                session.add(ContractReview(
                    review_id=review_id, thread_id=thread_id,
                    tenant_id=user["tenant_id"], user_id=user["user_id"],
                    contract_title=final.get("contract_title", ""),
                    contract_text=req.contract_text[:20000],
                    clauses_json=json.dumps(final.get("clauses", []), ensure_ascii=False),
                    risks_json=json.dumps(final.get("risk_points", []), ensure_ascii=False),
                    risk_level=final.get("risk_level", "high"),
                    review_opinion=final.get("review_opinion", ""),
                    format_check_json=json.dumps(final.get("format_check", {}), ensure_ascii=False),
                    status="pending_review",
                ))
            else:
                existing.status = "pending_review"
            await session.commit()
    return {
        "card": card,
        "summary": final.get("summary", ""),
        "interrupted": interrupted,
        "review_id": review_id,
        # interrupted=True 时记录已登记 pending_review，待合伙人处理
    }


@router.get("")
async def list_contracts(user: dict = Depends(get_current_user)):
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ContractReview)
            .where(ContractReview.tenant_id == user["tenant_id"], ContractReview.user_id == user["user_id"])
            .order_by(ContractReview.created_at.desc()).limit(100)
        )).scalars().all()
        return {"reviews": [
            {
                "review_id": r.review_id, "contract_title": r.contract_title,
                "risk_level": r.risk_level, "status": r.status,
                "reviewer_id": r.reviewer_id, "review_comment": r.review_comment,
                "created_at": r.created_at, "reviewed_at": r.reviewed_at,
            } for r in rows
        ]}


@router.get("/{review_id}")
async def get_contract_review(review_id: str, user: dict = Depends(get_current_user)):
    async with SessionLocal() as session:
        r = await session.get(ContractReview, review_id)
        if r is None or r.tenant_id != user["tenant_id"] or r.user_id != user["user_id"]:
            return {"review": None}
        return {
            "review": {
                "review_id": r.review_id, "contract_title": r.contract_title,
                "risk_level": r.risk_level, "status": r.status,
                "risk_points": json.loads(r.risks_json or "[]"),
                "review_opinion": r.review_opinion,
                "format_check": json.loads(r.format_check_json or "{}"),
                "reviewer_id": r.reviewer_id, "review_comment": r.review_comment,
                "created_at": r.created_at, "reviewed_at": r.reviewed_at,
            }
        }
