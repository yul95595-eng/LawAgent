"""Agent 图级测试 —— CaseAgent 10 节点主链（+1 追问分支）/ ComplianceAgent 6 节点 / 时效 FAQ 小图（含引用校验）。"""
from __future__ import annotations

import pytest

from backend.agents.case_agent import case_agent, limitation_faq_agent
from backend.agents.compliance_agent import compliance_agent
from backend.memory.models import CaseAnalysis, SessionLocal
from backend.orchestrator.orchestrator import NODE_LABELS
from sqlalchemy import select


pytestmark = pytest.mark.usefixtures("_prepare_db")


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


_DEMO_CASE_TEXT = (
    "劳动者王某2019年3月入职杭州某科技有限公司，任高级工程师，月薪2万元，工作年限3年2个月。"
    "2022年5月10日，公司以「组织架构调整」为由口头通知解除劳动合同，未支付任何经济补偿。"
    "王某主张公司违法解除，要求支付2N赔偿金14万元。"
)


class TestCaseAgent:
    async def test_demo1_nine_nodes_and_2n(self):
        """demo1：违法解除 → 主链全走 + 2N=14万 + 胜诉率 + 判例 + 落库。"""
        final = await case_agent.ainvoke(
            {"case_text": _DEMO_CASE_TEXT, "thread_id": "t-test-case",
             "tenant_id": "tenant_firm", "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("test-case-1"),
        )
        assert final["case_type"] == "labor_termination"
        facts = final["case_facts"]
        assert facts["years_of_service"] == 3.17
        assert facts["monthly_salary"] == 20000.0
        assert facts["termination_manner"] == "口头解除"
        claim = facts["claim_amount"]
        assert claim["formula"] == "2N"
        assert claim["amount"] == 140000.0
        # 主链产出齐全
        assert final["applicable_law"], "适用法条不应为空"
        assert final["precedents"], "类案检索不应为空"
        assert all("rank" in p for p in final["precedents"])
        assert len(final["legal_issues"]) >= 2
        assert "1 年" in final["limitation"]["period_text"]
        assert final["win_rate"] >= 0.7
        assert final["framework"]["title"]
        assert final["saved_case_id"]
        # 落库断言（永久保存口径）
        async with SessionLocal() as session:
            row = await session.get(CaseAnalysis, final["saved_case_id"])
            assert row is not None
            assert row.case_type == "labor_termination"
            assert row.win_rate == final["win_rate"]

    async def test_case_id_shortcut(self):
        """案件管理系统档案直达：case_id 命中时不走 LLM 分类。"""
        final = await case_agent.ainvoke(
            {"case_text": "", "case_id": "CASE-2026-0891", "thread_id": "t-test-case2",
             "tenant_id": "tenant_firm", "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("test-case-2"),
        )
        assert final["case_type"] == "labor_termination"
        assert "CASE-2026-0891" in final["summary"]

    def test_all_nodes_have_labels(self):
        for n in ("classify_case_type", "extract_case_facts", "search_applicable_law",
                  "match_precedent_cases", "analyze_legal_issues", "calculate_limitation",
                  "estimate_win_rate", "generate_analysis_framework", "save_case_analysis"):
            assert n in NODE_LABELS


class TestLimitationFaqAgent:
    async def test_labor_limitation_faq(self):
        """纯咨询无文书：案件域 FAQ 小图规则直出时效答复（时效计算归案件域）。"""
        final = await limitation_faq_agent.ainvoke(
            {"inquiry_text": "劳动仲裁时效是多久",
             "thread_id": "t-test-faq1", "tenant_id": "tenant_firm",
             "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("test-faq-1"),
        )
        report = final["report"]
        assert report["kind"] == "limitation_inquiry"
        assert "1 年" in report["answer"]
        assert "第二十七条" in report["answer"]
        # 引用回条文库验真（公共核验设施）
        cc = final["citation_check"]
        assert cc["checked"] >= 1
        assert cc["invalid_count"] == 0

    async def test_civil_limitation_faq(self):
        """非劳动语境 → 民事诉讼时效 3 年。"""
        final = await limitation_faq_agent.ainvoke(
            {"inquiry_text": "借款合同的诉讼时效是多久",
             "thread_id": "t-test-faq2", "tenant_id": "tenant_firm",
             "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("test-faq-2"),
        )
        assert "3 年" in final["report"]["answer"]
        assert "第一百八十八条" in final["report"]["answer"]


class TestComplianceAgent:
    async def test_demo3_enterprise_check(self):
        """demo3：华创精密（制造企业）经营范围不含投资 → 超范围风险 + 引用全过校验。"""
        final = await compliance_agent.ainvoke(
            {"enterprise_name": "华创精密制造股份有限公司",
             "enterprise_text": "华创精密制造股份有限公司拟对外股权投资，请做合规体检。",
             "thread_id": "t-test-comp", "tenant_id": "tenant_firm",
             "user_id": "u_l3001", "pipeline_context": {}},
            config=_cfg("test-comp-1"),
        )
        scope = final["scope_check"]
        assert scope["skipped"] is False
        assert scope["has_ultra_vires"] is True
        lic = final["license_check"]
        assert lic["skipped"] is False
        # 引用校验（幻觉治理）：全部引用回条文库核验通过
        cc = final["citation_check"]
        assert cc["checked"] >= 1
        assert cc["invalid_count"] == 0
        assert cc["accuracy"] == 1.0
        report = final["report"]
        assert report["risk_items"], "超经营范围应产生风险项"
        assert final["saved_profile_id"]

    async def test_all_nodes_have_labels(self):
        for n in ("load_enterprise_info", "check_business_scope", "check_license_qualification",
                  "verify_citation_accuracy", "generate_compliance_report", "save_compliance_report"):
            assert n in NODE_LABELS
