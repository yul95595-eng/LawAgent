"""端到端冒烟测试：mock LLM 下跑通三个 Agent + HITL 中断恢复 + Pipeline。

用法（项目根目录）：
    python scripts/smoke_run.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("USE_SQLITE", "1")
os.environ.setdefault("SQLITE_TEST_DB", ":memory:")
os.environ.setdefault("LLM_MOCK", "1")


async def main() -> None:
    from backend.agents.contract_agent import build_contract_agent
    from backend.memory.models import init_db
    from backend.orchestrator.orchestrator import (
        classify_intents,
        run_pipeline,
        run_single_intent,
    )
    from backend.tools.api_tools import get_contract_file

    await init_db()

    # ── 1. 意图识别 ────────────────────────────────────────────
    probes = [
        ("员工被违法辞退能要多少赔偿", "case_analysis"),
        ("帮我审查这份劳动合同有没有风险", "contract_review"),
        ("华创精密这家公司资质合规吗", "compliance_check"),
        ("劳动仲裁时效是多久", "limitation_inquiry"),
    ]
    for msg, expect in probes:
        got = await classify_intents(msg)
        status = "OK " if expect in got else "BAD"
        print(f"[intent {status}] {msg!r} -> {got} (expect {expect})")

    # ── 2. CaseAgent：demo1 违法解除 2N ───────────────────────
    _, case_final = await run_single_intent(
        "case_analysis",
        "员工王某2023年2月入职，签了3年合同，2026年4月被公司口头违法辞退，月薪2万元。请分析这个劳动争议案件。",
        thread_id="smoke-case",
        tenant_id="t_demo",
        user_id="u_smoke",
    )
    win = case_final.get("win_rate")
    comp = case_final.get("claim_amount")
    print(f"[case] nodes={case_final.get('current_node')}, win={win}, claim={comp}")
    assert case_final.get("case_type") == "wrongful_termination", case_final.get("case_type")

    # ── 3. ContractAgent：demo2 触发 HITL interrupt ──────────
    contract = await get_contract_file("HT-2026-1122")
    text = contract["contract_text"] if isinstance(contract, dict) else str(contract)
    _, ct_final = await run_single_intent(
        "contract_review",
        text,
        thread_id="smoke-contract",
        tenant_id="t_demo",
        user_id="u_smoke",
    )
    interrupted = bool(ct_final.get("__interrupt__"))
    review_id = ct_final.get("review_id")
    n_risks = len(ct_final.get("risk_points") or [])
    print(f"[contract] interrupted={interrupted}, review_id={review_id}, risks={n_risks}")
    assert interrupted, "高风险合同应挂起合伙人审核"
    assert review_id, "interrupt 前应有 review_id"

    # ── 4. 合伙人确认 → resume 走完 ──────────────────────────
    from langgraph.types import Command

    _, resumed = await run_single_intent(
        "contract_review",
        "",
        thread_id="smoke-contract",
        tenant_id="t_demo",
        user_id="u_smoke",
        resume=Command(
            resume={
                "decision": "confirmed",
                "reviewer_id": "L2001",
                "comment": "同意出具意见书",
            }
        ),
    )
    assert not resumed.get("__interrupt__"), "resume 后不应再次挂起"
    print(f"[resume] ok, status={resumed.get('status')}")

    # ── 5. ComplianceAgent：demo3 企业合规 ───────────────────
    _, cp_final = await run_single_intent(
        "compliance_check",
        "帮我查一下华创精密制造股份有限公司的合规情况，它最近想做一笔对外股权投资。",
        thread_id="smoke-compliance",
        tenant_id="t_demo",
        user_id="u_smoke",
    )
    cit = cp_final.get("citation_accuracy")
    print(f"[compliance] risks={len(cp_final.get('risk_points') or [])}, citation_accuracy={cit}")

    # ── 6. Pipeline：案例+合同+合规 三连 ─────────────────────
    pipeline = await run_pipeline(
        ["case_analysis", "contract_review", "compliance_check"],
        "员工被违法辞退月薪2万工作了3年2个月，另外审查这份竞业协议：乙方离职后5年内不得从事同业工作，无经济补偿。顺带查华创精密的合规。",
        thread_id="smoke-pipeline",
        tenant_id="t_demo",
        user_id="u_smoke",
    )
    print(f"[pipeline] {len(pipeline)} intents executed: {[r[0] for r in pipeline]}")

    print("\nSMOKE ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
