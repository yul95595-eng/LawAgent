"""LLM 工具测试（LLM_MOCK=1 确定性路径）。

Mock 引擎的验收口径：能抓硬风险、不误报干净条款（宁可不报也不误报）。
"""
from __future__ import annotations

import pytest

from backend.core.config import settings
from backend.tools.llm_tools import (
    llm_analyze_legal_risk,
    llm_classify_case_type,
    llm_generate_legal_opinion,
    llm_match_precedent,
    llm_review_contract_clause,
)


pytestmark = pytest.mark.usefixtures("_prepare_db")


def test_mock_mode_active():
    assert settings.llm_enabled is False


class TestClassify:
    async def test_labor_termination(self):
        r = await llm_classify_case_type("公司把员工王某违法辞退了，要求支付赔偿金 2N")
        assert r["case_type"] == "labor_termination"
        assert r["engine"] == "mock"

    async def test_non_compete(self):
        r = await llm_classify_case_type("离职后签了竞业限制协议，补偿一直没给")
        assert r["case_type"] == "labor_non_compete"

    async def test_equity_buyback(self):
        r = await llm_classify_case_type("投资协议里的股权回购条款触发，双方对赌失败")
        assert r["case_type"] == "contract_equity"

    async def test_fallback_default(self):
        r = await llm_classify_case_type("今天天气不错")
        assert r["case_type"] in {
            "labor_termination", "labor_probation", "labor_non_compete",
            "labor_service_period", "contract_sales", "contract_equity",
            "contract_lending", "ip_license",
        }
        assert r["confidence"] <= 0.5


class TestReviewClause:
    async def test_trial_over_limit_not_here_but_noncompete(self):
        """竞业限制无补偿 → 风险。"""
        r = await llm_review_contract_clause(
            "乙方离职后 5 年内不得从事同行业工作，不得加入竞争企业。", clause_id="第5条"
        )
        assert r is not None
        assert r["risk_type"] == "non_compete_no_pay"
        assert r["source"] == "llm"
        assert r["clause_id"] == "第5条"

    async def test_non_compete_with_pay_is_clean(self):
        """约定了按月经济补偿 → 不报风险（不误报口径）。"""
        r = await llm_review_contract_clause(
            "乙方离职后 2 年内不得从事同行业工作，甲方按月支付经济补偿。"
        )
        assert r is None

    async def test_service_penalty_without_training(self):
        r = await llm_review_contract_clause(
            "乙方违反服务期约定的，应向甲方支付违约金 100000 元。"
        )
        assert r is not None
        assert r["risk_type"] == "penalty_illegal"

    async def test_buyback(self):
        r = await llm_review_contract_clause(
            "若目标公司未完成业绩承诺，投资方有权要求其回购股权（对赌安排）。"
        )
        assert r is not None
        assert r["risk_type"] == "buyback_with_target_company"

    async def test_anti_dilution(self):
        r = await llm_review_contract_clause(
            "公司后续融资时，投资方有权按反稀释条款获得补偿，同等条件下不受限制。"
        )
        assert r is not None
        assert r["risk_type"] == "anti_dilution_unfair"

    async def test_clean_clause_no_false_positive(self):
        r = await llm_review_contract_clause(
            "甲方应按月支付乙方工资 20000 元，于每月 10 日前发放至乙方工资账户。"
        )
        assert r is None


class TestAnalyzeAndOpinion:
    async def test_analyze_labor(self):
        issues = await llm_analyze_legal_risk(
            "labor_termination",
            {"years_of_service": 3.17, "monthly_salary": 20000,
             "termination_manner": "口头辞退", "claim_amount": {"amount_text": "14 万元"}},
            [],
        )
        assert len(issues) >= 2
        assert all("issue" in i and "basis" in i for i in issues)

    async def test_analyze_generic(self):
        issues = await llm_analyze_legal_risk("contract_sales", {}, [])
        assert len(issues) >= 1

    async def test_opinion_with_context(self):
        text = await llm_generate_legal_opinion({
            "title": "合规法律意见书",
            "enterprise": {"enterprise_name": "华创精密制造股份有限公司",
                           "credit_code": "91110000MA01X2Y3Z4",
                           "registered_capital": "8000 万元",
                           "business_scope_brief": "精密仪器制造"},
            "risks": [{"risk_name": "超经营范围投资", "level": "high"}],
        })
        assert "华创精密" in text
        assert "风险" in text


class TestMatchPrecedent:
    async def test_rank_with_reason(self):
        from backend.tools.mcp_tools import search_precedent_cases

        candidates = await search_precedent_cases(
            "公司违法辞退员工，工作 3 年要求 2N 赔偿", case_type="labor_termination", top_k=3
        )
        assert candidates, "labor_termination 判例库不应为空"
        ranked = await llm_match_precedent("公司违法辞退员工", candidates)
        assert len(ranked) == len(candidates)
        assert all("match_reason" in c for c in ranked)

    async def test_empty_candidates(self):
        assert await llm_match_precedent("x", []) == []
