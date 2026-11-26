"""Orchestrator 编排器测试 —— 8 意图路由 / 双路由分流 / Pipeline 上下文传递 / 结果卡片 / SSE 事件流。"""
from __future__ import annotations

import pytest

from backend.orchestrator.orchestrator import (
    INTENT_ROUTES,
    NODE_LABELS,
    build_initial_state,
    build_result_card,
    classify_intent_rules,
    classify_intents,
    resolve_risk_assessment_target,
    run_pipeline,
    stream_pipeline,
)


pytestmark = pytest.mark.usefixtures("_prepare_db")


class TestIntentRules:
    def test_all_eight_intents_routed(self):
        assert set(INTENT_ROUTES) == {
            "case_analysis", "precedent_search", "contract_review", "clause_analysis",
            "compliance_check", "legal_opinion", "limitation_inquiry", "risk_assessment",
        }

    def test_keyword_hits(self):
        assert "limitation_inquiry" in classify_intent_rules("劳动仲裁时效是多久")
        assert "contract_review" in classify_intent_rules("帮我审查合同")
        assert "compliance_check" in classify_intent_rules("查一下这家公司资质和许可证")
        assert "case_analysis" in classify_intent_rules("这个案情能打官司吗胜诉把握多大")
        assert "precedent_search" in classify_intent_rules("有没有类似判例")
        assert "legal_opinion" in classify_intent_rules("出一份法律意见书")
        assert "risk_assessment" in classify_intent_rules("这份材料有什么风险点")

    def test_pipeline_combo(self):
        hits = classify_intent_rules("给华创做合规体检，再审查这份合同，最后出一份意见书")
        assert "compliance_check" in hits
        assert "contract_review" in hits
        assert "legal_opinion" in hits
        # 执行顺序：合规 → 合同 → 意见书
        assert hits.index("compliance_check") < hits.index("contract_review") <= hits.index("legal_opinion")

    def test_fallback_default(self):
        # 无任何法律语义 → 超范围兜底，不进业务流水线
        assert classify_intent_rules("你好呀") == ["chitchat"]
        assert classify_intent_rules("今天天气怎么样") == ["chitchat"]
        # 有法律语义但无显式关键词 → 默认案件分析
        assert classify_intent_rules("我有个纠纷想咨询") == ["case_analysis"]

    async def test_cached_entry(self):
        assert await classify_intents("仲裁时效多久") == ["limitation_inquiry"]
        # 同问句二次识别走 LRU 缓存（同结果）
        assert await classify_intents("仲裁时效多久") == ["limitation_inquiry"]


class TestDualRoute:
    def test_resolve_targets(self):
        assert resolve_risk_assessment_target("评估这家公司资质风险") == "compliance"
        assert resolve_risk_assessment_target("评估这份合同的条款风险") == "contract"
        assert resolve_risk_assessment_target("评估这个案子的风险") == "case"


class TestBuildInitialState:
    def test_case_intent(self):
        agent, state = build_initial_state(
            "case_analysis", "员工被违法辞退月薪2万", thread_id="t", tenant_id="x", user_id="y")
        assert agent == "case"
        assert state["case_text"] == "员工被违法辞退月薪2万"

    def test_contract_intent_attachments_win(self):
        agent, state = build_initial_state(
            "contract_review", "审查这份合同 第一条 试用期6个月",
            thread_id="t", tenant_id="x", user_id="y",
            attachments={"contract_text": "第一条 完整合同正文……"},
        )
        assert agent == "contract"
        assert state["contract_text"] == "第一条 完整合同正文……"

    def test_limitation_intent(self):
        agent, state = build_initial_state(
            "limitation_inquiry", "时效是多久", thread_id="t", tenant_id="x", user_id="y")
        assert agent == "case_faq"          # 时效咨询归案件域（解耦后不再走 compliance）
        assert state["inquiry_text"] == "时效是多久"

    def test_compliance_intent(self):
        agent, state = build_initial_state(
            "compliance_check", "华创精密制造股份有限公司违法辞退员工要赔2N吗",
            thread_id="t", tenant_id="x", user_id="y")
        assert agent == "compliance"
        assert "华创精密" in state["enterprise_name"]
        # 合规域瘦身：不再有赔偿/时效咨询字段（解耦契约）
        assert "include_claim" not in state
        assert "claim_scenario" not in state
        assert "inquiry_text" not in state


class TestPipeline:
    async def test_pipeline_context_absorption(self):
        results = await run_pipeline(
            ["case_analysis", "compliance_check"],
            "员工被违法辞退月薪2万工作年限3年2个月；另外查一下华创精密制造股份有限公司的合规，它想做对外股权投资",
            thread_id="test-pipe-1", tenant_id="tenant_firm", user_id="u_l1001",
        )
        assert [r[0] for r in results] == ["case_analysis", "compliance_check"]
        _, case_final = results[0][1], results[0][2]
        assert case_final["saved_case_id"]
        comp_final = results[1][2]
        assert comp_final["report"]["kind"] == "compliance_check"

    async def test_stream_events_shape(self):
        events = []
        async for kind, payload in stream_pipeline(
            ["limitation_inquiry"], "劳动仲裁时效是多久",
            thread_id="test-stream-1", tenant_id="tenant_firm", user_id="u_l1001",
        ):
            events.append((kind, payload))
        kinds = [k for k, _ in events]
        assert "node" in kinds and kinds[-1] == "result"
        node_events = [p for k, p in events if k == "node"]
        assert all("label" in p and p["label"] == NODE_LABELS.get(p["node"], p["node"])
                   for p in node_events)
        card = events[-1][1]
        assert card["agent"] == "case_faq"   # 时效 FAQ 小图（案件域）
        assert card["result"]["report"]["kind"] == "limitation_inquiry"


class TestResultCard:
    async def test_compliance_card(self):
        card = build_result_card("compliance", "compliance_check", {
            "report": {"kind": "compliance_check",
                       "enterprise": {"enterprise_name": "华创精密制造股份有限公司"},
                       "risk_items": [{"risk_name": "超范围", "level": "medium"}],
                       "risk_score": 10},
            "summary": "ok",
            "citation_check": {"checked": 3, "valid_count": 3, "invalid_count": 0,
                               "accuracy": 1.0, "invalid": [],
                               "verified": [{"cite": "《公司法》第十四条", "quote": "x", "valid": True}]},
            "risk_score": 10,
            "saved_profile_id": "ecp_1",
        })
        assert card["title"].startswith("合规体检：华创")
        assert card["result"]["citation_check"]["accuracy"] == 1.0
        # cite 字符串在卡片层解析为结构化 Citation
        assert card["citations"][0]["law"] == "公司法"
        assert card["citations"][0]["article_no"] == "第十四条"

    async def test_case_card(self):
        card = build_result_card("case", "case_analysis", {
            "case_type_label": "违法解除劳动合同纠纷",
            "summary": "done",
            "case_facts": {"monthly_salary": 20000},
            "applicable_law": [{"law": "劳动合同法", "article_no": "第八十七条",
                                "cite": "劳动合同法第八十七条", "text": "原文…"}],
            "precedents": [{"case_no": "(2020)京01民终5195号"}],
            "limitation": {"period_text": "1 年"},
            "win_rate": 0.75, "win_rate_reason": "高",
            "framework": {"title": "分析框架"}, "saved_case_id": "case_1",
        })
        assert card["title"] == "案件分析：违法解除劳动合同纠纷"
        assert card["citations"][0]["article_no"] == "第八十七条"
        assert card["result"]["win_rate"] == 0.75
        assert "复核" in card["audit_hint"]

    async def test_contract_card(self):
        card = build_result_card("contract", "contract_review", {
            "contract_title": "劳动合同", "summary": "done",
            "risk_level": "high", "risk_points": [{"risk_type": "trial_over_limit"}],
            "format_check": {"complete": False}, "review_opinion": "意见书正文",
            "review_decision": "confirmed", "review_id": "review_1",
            "clauses": [{"clause_id": "c_001"}],
        })
        assert card["result"]["review_status"] == "approved"
        assert card["result"]["clause_count"] == 1
        assert "HITL" in card["audit_hint"]
