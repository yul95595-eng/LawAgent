"""复核红队（P3）—— 落库前挑自己人的错。

守四件事：
1. 过强表述必须拦（法律场景不能出现「保证胜诉」）；
2. 无条文依据的结论必须拦（法条库未覆盖且已声明时，降级为提示不阻断）；
3. 打回最多一次，之后 forced_pass 交人审，绝不无限重写；
4. 正常案件不被误伤（pass），否则评测会掉。
"""
from __future__ import annotations

import pytest

from backend.agents.case_agent import case_agent, review_draft_node, route_after_review
from backend.agents.review import review_case_output

pytestmark = pytest.mark.usefixtures("_prepare_db")

_OK_STATE = {
    "case_type": "labor_termination",
    "applicable_law": [{"cite": "劳动合同法第八十七条"}],
    "legal_issues": [{"issue": "解除是否违法", "analysis": "公司未举证法定情形，解除违法",
                      "basis": "《劳动合同法》第八十七条"}],
    "limitation": {"period_text": "1 年"},
    "framework": {"text": "建议围绕违法解除组织证据，胜诉可能性较高。"},
}


class TestReviewRules:
    def test_clean_output_passes(self):
        findings, verdict = review_case_output(_OK_STATE)
        assert findings == []
        assert verdict == "pass"

    def test_absolute_claim_is_rejected(self):
        state = {**_OK_STATE,
                 "framework": {"text": "本案必定胜诉，可以保证胜诉。"}}
        findings, verdict = review_case_output(state)
        assert verdict == "reject"
        assert any(f["rule"] == "overconfident" for f in findings)
        assert all(f["level"] == "high" for f in findings)

    def test_conclusion_without_law_is_rejected(self):
        state = {**_OK_STATE, "applicable_law": []}
        findings, verdict = review_case_output(state)
        assert verdict == "reject"
        assert any(f["rule"] == "no_citation" for f in findings)

    def test_declared_gap_is_warn_not_block(self):
        """库里没覆盖且已明确声明 → 只提示，不阻断（否则改也改不好）。"""
        state = {**_OK_STATE, "applicable_law": [],
                 "law_gap_note": "法条库未收录该领域条文"}
        findings, verdict = review_case_output(state)
        assert verdict == "warn"
        assert any(f["rule"] == "no_citation_declared" for f in findings)

    def test_missing_limitation_is_warn(self):
        state = {**_OK_STATE, "limitation": {}}
        findings, verdict = review_case_output(state)
        assert verdict == "warn"
        assert any(f["rule"] == "missing_limitation" for f in findings)


class TestReviewLoop:
    async def test_first_reject_gives_one_rewrite_chance(self):
        state = {**_OK_STATE, "framework": {"text": "保证胜诉"}, "review_retry": 0}
        out = await review_draft_node(state)
        assert out["review_verdict"] == "reject"
        assert out["review_retry"] == 1
        assert out["review_feedback"]
        assert route_after_review(out) == "generate_analysis_framework"

    async def test_second_reject_forces_pass_not_loop(self):
        state = {**_OK_STATE, "framework": {"text": "保证胜诉"}, "review_retry": 1}
        out = await review_draft_node(state)
        assert out["review_verdict"] == "forced_pass"
        assert route_after_review(out) == "save_case_analysis"

    async def test_clean_case_goes_straight_to_save(self):
        out = await review_draft_node(_OK_STATE)
        assert out["review_verdict"] == "pass"
        assert route_after_review(out) == "save_case_analysis"


class TestReviewIntegration:
    async def test_normal_case_is_not_flagged(self):
        """正常案件必须 pass，否则复核会变成噪声源。"""
        final = await case_agent.ainvoke(
            {"case_text": "劳动者2019年3月入职，月薪2万元，工作3年2个月，"
                          "2022年5月公司以组织架构调整为由口头解除劳动合同。",
             "thread_id": "t-review", "tenant_id": "tenant_firm",
             "user_id": "u_l1001", "pipeline_context": {}},
            config={"configurable": {"thread_id": "test-review-1"}},
        )
        assert final["applicable_law"], "前置条件：应召回法条"
        assert final["review_verdict"] == "pass"
        assert final["review_findings"] == []
        assert final["saved_case_id"]
