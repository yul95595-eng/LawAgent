"""HitL 合伙人审核测试 —— interrupt 挂起 / Command(resume) 恢复 / 退回回环 / 低风险直通。

这是整个系统的核心亮点（「AI 辅助、责任在人」的工程落地）：
高风险合同审查意见书必须经合伙人确认才能对外提交。
"""
from __future__ import annotations

import pytest
from langgraph.types import Command

from backend.memory.models import ContractReview, SessionLocal
from backend.orchestrator.orchestrator import run_single_intent


pytestmark = pytest.mark.usefixtures("_prepare_db")


HIGH_RISK_CONTRACT = """
劳动合同

甲方（用人单位）：某某科技有限公司；乙方（劳动者）：李某。
第一条 合同期限：本合同为固定期限劳动合同，合同期限为1年。
第二条 试用期：试用期为6个月，试用期工资为转正工资的80%。
第三条 工作内容与工作地点：乙方担任研发工程师，工作地点为杭州，岗位职责见附件。
第四条 工作时间和休息休假：标准工时制，乙方依法享有法定节假日与年休假。
第五条 劳动报酬：乙方月薪15000元，甲方于每月10日前发放工资。
第六条 社会保险：甲方依法为乙方缴纳社会保险。
第七条 竞业限制：乙方离职后5年内不得入职与甲方有竞争关系的企业，无须任何理由。
第八条 服务期与违约金：乙方违反服务期约定的，应向甲方支付违约金100000元。
"""

LOW_RISK_CONTRACT = """
技术保密协议

甲方：某某科技有限公司；乙方：李某。
一、乙方对在职期间知悉的甲方技术秘密承担保密义务。
二、保密义务在双方劳动关系终止后两年内持续有效。
三、本协议自双方签字盖章之日起生效，一式两份。
"""


class TestHitL:
    async def test_high_risk_interrupts_at_partner_review(self):
        """高风险 → partner_review 节点 interrupt 挂起，review_id 可用于登记待审。"""
        agent, final = await run_single_intent(
            "contract_review", HIGH_RISK_CONTRACT,
            thread_id="test-hitl-1", tenant_id="tenant_firm", user_id="u_l1001",
        )
        assert agent == "contract"
        assert final.get("__interrupt__"), "高风险必须挂起合伙人审核"
        assert final["risk_level"] == "high"
        assert final["review_id"]
        risk_types = {r["risk_type"] for r in final["risk_points"]}
        assert "trial_over_limit" in risk_types       # 1年合同试用期6个月（一票否决）
        assert "non_compete_over_2y" in risk_types    # 竞业5年
        assert "penalty_illegal" in risk_types        # 服务期违约金10万无培训费
        # DB 里已有 draft 记录（API 层在 interrupt 时登记 pending_review）
        async with SessionLocal() as session:
            row = await session.get(ContractReview, final["review_id"])
            if row is not None:
                assert row.status in ("draft", "pending_review")

    async def test_confirm_resumes_to_approved(self):
        """合伙人确认 → resume → submit_result → 状态 approved。"""
        _, final = await run_single_intent(
            "contract_review", HIGH_RISK_CONTRACT,
            thread_id="test-hitl-2", tenant_id="tenant_firm", user_id="u_l1001",
        )
        review_id = final["review_id"]
        _, resumed = await run_single_intent(
            "contract_review", "",
            thread_id="test-hitl-2", tenant_id="tenant_firm", user_id="u_l1001",
            resume=Command(resume={"decision": "confirmed", "reviewer_id": "P2001",
                                   "comment": "同意出具意见书"}),
        )
        assert not resumed.get("__interrupt__")
        assert resumed["review_decision"] == "confirmed"
        assert resumed["reviewer_id"] == "P2001"
        assert "approved" in resumed["summary"]
        async with SessionLocal() as session:
            row = await session.get(ContractReview, review_id)
            assert row is not None and row.status == "approved"
            assert row.reviewer_id == "P2001"
            assert row.reviewed_at

    async def test_returned_loops_back_then_reconfirm(self):
        """合伙人退回 → 回到 generate_review_opinion 重写 → 再次挂起 → 再确认。"""
        _, final = await run_single_intent(
            "contract_review", HIGH_RISK_CONTRACT,
            thread_id="test-hitl-3", tenant_id="tenant_firm", user_id="u_l1001",
        )
        review_id = final["review_id"]
        # 第一次 resume：退回
        _, resumed = await run_single_intent(
            "contract_review", "",
            thread_id="test-hitl-3", tenant_id="tenant_firm", user_id="u_l1001",
            resume=Command(resume={"decision": "returned", "reviewer_id": "P2001",
                                   "comment": "补充竞业限制经济补偿分析后再报"}),
        )
        assert resumed.get("__interrupt__"), "退回后重写意见应再次进合伙人审核"
        assert resumed["review_decision"] == "returned"
        # 第二次 resume：确认
        _, done = await run_single_intent(
            "contract_review", "",
            thread_id="test-hitl-3", tenant_id="tenant_firm", user_id="u_l1001",
            resume=Command(resume={"decision": "confirmed", "reviewer_id": "P2001",
                                   "comment": "通过"}),
        )
        assert not done.get("__interrupt__")
        assert done["review_decision"] == "confirmed"
        async with SessionLocal() as session:
            row = await session.get(ContractReview, review_id)
            assert row is not None and row.status == "approved"
            assert "通过" in (row.review_comment or "")

    async def test_low_risk_passes_through(self):
        """低风险合同不挂起，直接 submit_result 定稿（approved）。"""
        agent, final = await run_single_intent(
            "contract_review", LOW_RISK_CONTRACT,
            thread_id="test-hitl-4", tenant_id="tenant_firm", user_id="u_l1001",
        )
        assert not final.get("__interrupt__")
        assert final["risk_level"] in ("low", "medium")
        assert final["review_id"]
        async with SessionLocal() as session:
            row = await session.get(ContractReview, final["review_id"])
            assert row is not None and row.status == "approved"
