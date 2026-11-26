"""stats + laws 路由 —— 风险看板统计 + 法条库直查。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from backend.api.deps import get_current_user
from backend.memory.models import (
    CaseAnalysis,
    ChatThread,
    ContractReview,
    EnterpriseComplianceProfile,
    SessionLocal,
)
from backend.retrieval.statutes import search_statutes

router = APIRouter(tags=["misc"])


@router.get("/stats/overview")
async def stats_overview(user: dict = Depends(get_current_user)):
    """风险看板（法务角色右侧面板）：各域任务量 + 风险分布 + 高风险合同数。"""
    async with SessionLocal() as session:
        tenant = user["tenant_id"]
        thread_count = await session.scalar(
            select(func.count()).select_from(ChatThread).where(ChatThread.tenant_id == tenant)
        ) or 0
        case_count = await session.scalar(
            select(func.count()).select_from(CaseAnalysis).where(CaseAnalysis.tenant_id == tenant)
        ) or 0
        review_rows = (await session.execute(
            select(ContractReview).where(ContractReview.tenant_id == tenant)
        )).scalars().all()
        profile_rows = (await session.execute(
            select(EnterpriseComplianceProfile).where(EnterpriseComplianceProfile.tenant_id == tenant)
        )).scalars().all()

    risk_dist = {"high": 0, "medium": 0, "low": 0}
    pending = 0
    for r in review_rows:
        if r.risk_level in risk_dist:
            risk_dist[r.risk_level] += 1
        if r.status == "pending_review":
            pending += 1
    avg_score = (
        round(sum(p.risk_score for p in profile_rows) / len(profile_rows), 1) if profile_rows else 0
    )
    return {
        "threads": thread_count,
        "case_analyses": case_count,
        "contract_reviews": len(review_rows),
        "pending_reviews": pending,
        "risk_distribution": risk_dist,
        "compliance_profiles": len(profile_rows),
        "avg_risk_score": avg_score,
    }


@router.get("/laws/search")
async def laws_search(q: str, top_k: int = 5, user: dict = Depends(get_current_user)):
    """法条知识库直查（MCP search_law_knowledge_base 同源）。"""
    hits = await _search(q, top_k)
    return {"query": q, "results": hits}


async def _search(q: str, top_k: int) -> list[dict]:
    from backend.tools.mcp_tools import search_law_knowledge_base

    return await search_law_knowledge_base(q, top_k=top_k)
