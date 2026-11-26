"""compliance 路由 —— 直达合规体检/意见书 + 合规档案查询。

时效咨询直达走 /api/v1/case/faq（案件域 FAQ 小图），本路由只做企业合规。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.audit import audit
from backend.api.deps import get_current_user
from backend.memory.models import EnterpriseComplianceProfile, SessionLocal
from backend.orchestrator.orchestrator import _AGENTS, _config, build_result_card

router = APIRouter(prefix="/compliance", tags=["compliance"])


class ComplianceRequest(BaseModel):
    enterprise_name: str = ""
    enterprise_text: str = ""       # 企业提供的材料/拟开展交易描述
    final_opinion: bool = False     # Pipeline 末段：出最终意见书
    contract_risks: list = []       # Pipeline 注入：合同审查结论
    thread_id: str = ""


@router.post("/check")
async def compliance_check(req: ComplianceRequest, user: dict = Depends(get_current_user)):
    """直达 ComplianceAgent（企业合规体检单一职责）。"""
    thread_id = req.thread_id or f"th_cmp_{req.enterprise_name or 'direct'}"
    state = {
        "thread_id": thread_id, "tenant_id": user["tenant_id"], "user_id": user["user_id"],
        "enterprise_name": req.enterprise_name, "enterprise_text": req.enterprise_text,
        "pipeline_context": {
            "final_opinion": req.final_opinion,
            "contract_risks": req.contract_risks,
            "conclusion": "建议按风险清单完成整改后再行推进。" if req.contract_risks else "",
        },
    }
    final = await _AGENTS["compliance"].ainvoke(state, config=_config(thread_id))
    intent = "legal_opinion" if req.final_opinion else "compliance_check"
    card = build_result_card("compliance", intent, final)
    await audit(user["tenant_id"], user["user_id"], user["role"], "compliance_check",
                "compliance_profile", final.get("saved_profile_id", ""))
    return {"card": card, "summary": final.get("summary", "")}


@router.get("")
async def list_profiles(user: dict = Depends(get_current_user)):
    """企业合规档案列表（法务角色看板数据源）。"""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(EnterpriseComplianceProfile)
            .where(EnterpriseComplianceProfile.tenant_id == user["tenant_id"],
                   EnterpriseComplianceProfile.user_id == user["user_id"])
            .order_by(EnterpriseComplianceProfile.created_at.desc()).limit(100)
        )).scalars().all()
        return {"profiles": [
            {
                "profile_id": r.profile_id, "enterprise_name": r.enterprise_name,
                "credit_code": r.credit_code, "risk_score": r.risk_score,
                "created_at": r.created_at,
            } for r in rows
        ]}


@router.get("/{profile_id}")
async def get_profile(profile_id: str, user: dict = Depends(get_current_user)):
    async with SessionLocal() as session:
        r = await session.get(EnterpriseComplianceProfile, profile_id)
        if r is None or r.tenant_id != user["tenant_id"] or r.user_id != user["user_id"]:
            return {"profile": None}
        check = json.loads(r.check_json or "{}")
        return {
            "profile": {
                "profile_id": r.profile_id, "enterprise_name": r.enterprise_name,
                "credit_code": r.credit_code, "risk_score": r.risk_score,
                "info": json.loads(r.info_json or "{}"),
                "check": check,
                "citation_check": json.loads(r.citation_check_json or "{}"),
                "created_at": r.created_at,
            }
        }
