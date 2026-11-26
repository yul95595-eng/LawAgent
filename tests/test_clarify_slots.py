"""澄清槽位 + 检索改写 —— 守门测试。

背景（实测数据，见 docs/AGENT_FLOW.md）：
同一套 TF-IDF 引擎，把「整段案情」当 query 时 top1=0.090、正确答案
《劳动合同法》第八十七条不在 top-6；换成术语式短 query 后 top1=0.343、
第八十七条排名第一。引擎没变，变的是问法 —— 所以这两件事必须有测试：
1. 信息不够时要先问（不能静默猜着算）；
2. 信息够了要换对问法（不能把整段案情直接拼进检索式）。
"""
from __future__ import annotations

import json

import pytest

from backend.agents.case_agent import (
    build_law_queries,
    case_agent,
    limitation_faq_agent,
    search_applicable_law_node,
)
from backend.agents.slots import build_clarify_text, check_slots, should_block
from backend.orchestrator.orchestrator import build_result_card

pytestmark = pytest.mark.usefixtures("_prepare_db")


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


_DEMO_CASE_TEXT = (
    "劳动者王某2019年3月入职杭州某科技有限公司，任高级工程师，月薪2万元，工作年限3年2个月。"
    "2022年5月10日，公司以「组织架构调整」为由口头通知解除劳动合同，未支付任何经济补偿。"
    "王某主张公司违法解除，要求支付2N赔偿金14万元。"
)

_VAGUE = "公司把我开了，能赔多少？"


class TestSlotCheck:
    """槽位检查：只问缺的，不重复问已答的。"""

    def test_full_facts_need_no_question(self):
        report = check_slots("labor_termination", {}, _DEMO_CASE_TEXT)
        assert not report.blocking_missing
        blocked, reason = should_block(report, _DEMO_CASE_TEXT)
        assert blocked is False and reason == ""

    def test_vague_case_blocks_and_asks_specific_slots(self):
        """一句话丢过来 + 缺 2 个关键槽位 → 必须追问，不许硬算赔偿。"""
        report = check_slots("labor_termination", {}, _VAGUE)
        assert {s.key for s in report.blocking_missing} == {"years_of_service", "monthly_salary"}
        blocked, reason = should_block(report, _VAGUE)
        assert blocked is True and "关键信息" in reason
        asked = build_clarify_text(report)
        assert "工作年限" in asked and "工资" in asked

    def test_long_text_with_missing_slot_is_not_blocked(self):
        """用户已经写了一大段案情 → 不该拦着他填表，只标注缺口。"""
        text = _DEMO_CASE_TEXT.replace("口头通知解除劳动合同", "解除劳动合同")
        text = text.replace("工作年限3年2个月", "工作年限3年2个月").replace("月薪2万元", "月薪2万元")
        report = check_slots("labor_termination", {}, text)
        blocked, _ = should_block(report, text)
        assert blocked is False

    def test_one_missing_slot_still_blocks_when_text_is_short(self):
        """只要还是「一句话」，缺一项也问 —— 硬算出来的金额就是编的。"""
        text = "我在公司干了3年2个月，现在被辞退了，能赔多少钱？"
        report = check_slots("labor_termination", {}, text)
        assert [s.key for s in report.blocking_missing] == ["monthly_salary"]
        blocked, reason = should_block(report, text)
        assert blocked is True and "关键信息" in reason

    def test_probation_needs_contract_term(self):
        """试用期是否超上限必须知道合同期限（1年/1~3年/3年以上三档）。"""
        report = check_slots("labor_probation", {}, "试用期六个月是否合法")
        assert "contract_term" in {s.key for s in report.blocking_missing}
        blocked, _ = should_block(report, "试用期六个月是否合法")
        assert blocked is True

    def test_facts_dict_counts_as_filled(self):
        """已抽取的事实优先于文本正则：facts 里有值就算填了。"""
        report = check_slots(
            "labor_termination",
            {"years_of_service": 3.17, "monthly_salary": 20000.0},
            "",
        )
        assert {s.key for s in report.filled} >= {"years_of_service", "monthly_salary"}
        assert not report.blocking_missing


class TestClarifyBranch:
    """图级：条件边真的会走到 ask_clarify，且不检索、不落库。"""

    async def test_vague_case_ends_with_question(self):
        final = await case_agent.ainvoke(
            {"case_text": _VAGUE, "thread_id": "t-clarify-1",
             "tenant_id": "tenant_firm", "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("t-clarify-1"),
        )
        assert final["clarify_pending"] is True
        assert final["clarify"]["blocked"] is True
        assert "工作年限" in final["summary"]
        # 没有检索、没有结论、没有落库
        assert not final.get("applicable_law")
        assert not final.get("saved_case_id")
        assert not final.get("win_rate")

    async def test_full_case_goes_through_normal_chain(self):
        final = await case_agent.ainvoke(
            {"case_text": _DEMO_CASE_TEXT, "thread_id": "t-clarify-2",
             "tenant_id": "tenant_firm", "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("t-clarify-2"),
        )
        assert not final.get("clarify_pending")
        assert final["clarify"]["blocked"] is False
        assert final["saved_case_id"]

    async def test_precedent_search_skips_clarify(self):
        """用户点名案由找类案 → 不是要结论，不该被追问拦下。"""
        final = await case_agent.ainvoke(
            {"case_text": "违法解除劳动合同", "focus_case_type": "labor_termination",
             "thread_id": "t-clarify-3", "tenant_id": "tenant_firm",
             "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("t-clarify-3"),
        )
        assert final["clarify"]["blocked"] is False
        assert "skipped" in final["clarify"]

    def test_clarify_card_has_no_citations(self):
        """追问卡片不能带法条引用（没检索过，带引用就是假证据）。"""
        card = build_result_card("case", "case_analysis", {
            "case_type_label": "违法解除劳动合同纠纷",
            "clarify_pending": True,
            "summary": "请补充工作年限与月工资",
            "clarify": {"blocked": True, "questions": ["工作年限？"]},
        })
        assert card["title"].startswith("需补充信息")
        assert card["citations"] == []
        assert card["result"]["clarify_pending"] is True


class TestLawQueryRewrite:
    """检索改写：这条链路的正确性靠「答案有没有被召回」来守，不靠分数好看。"""

    def test_queries_are_deduplicated_and_bounded(self):
        queries = build_law_queries({
            "case_type": "labor_termination",
            "case_type_label": "违法解除劳动合同纠纷",
            "case_text": _DEMO_CASE_TEXT,
            "case_facts": {"years_of_service_text": "3年2个月", "monthly_salary": 20000.0,
                           "termination_manner": "口头解除"},
        })
        assert 1 <= len(queries) <= 3
        assert len(set(queries)) == len(queries)

    def test_first_query_is_short_and_not_raw_case_text(self):
        """回归守卫：第 1 轮必须是短查询（实测整段案情会把正确答案挤出 top-6）。

        第 3 轮刻意保留原文长查询作为保底，所以只约束第 1 轮的长度。
        """
        queries = build_law_queries({
            "case_type": "labor_termination",
            "case_type_label": "违法解除劳动合同纠纷",
            "case_text": _DEMO_CASE_TEXT,
            "case_facts": {},
        })
        assert queries[0] != _DEMO_CASE_TEXT
        assert len(queries[0]) <= 60
        assert _DEMO_CASE_TEXT not in queries[:2]

    async def test_answer_article_is_recalled(self):
        """违法解除场景必须召回《劳动合同法》第八十七条（2N 的请求权基础）。"""
        out = await search_applicable_law_node({
            "case_type": "labor_termination",
            "case_type_label": "违法解除劳动合同纠纷",
            "case_text": _DEMO_CASE_TEXT,
            "case_facts": {"years_of_service_text": "3年2个月", "monthly_salary": 20000.0,
                           "termination_manner": "口头解除"},
        })
        cites = [law["cite"] for law in out["applicable_law"]]
        assert any("第八十七条" in c for c in cites), f"未召回第八十七条，实际={cites}"
        trace = out["retrieval_trace"]
        assert 1 <= len(trace) <= 3
        assert trace[-1]["last"] is True, f"三路召回应跑完：{trace}"

    async def test_library_gap_is_declared_not_faked(self):
        """库里没收录的领域要明说，不能拿相近条文硬凑（借贷实测零命中）。"""
        out = await search_applicable_law_node({
            "case_type": "contract_lending",
            "case_type_label": "民间借贷纠纷",
            "case_text": "借款50万元，约定月息2分，未约定还款期限，多次催要未果",
            "case_facts": {},
        })
        assert out["applicable_law"] == []
        assert "未收录" in out["law_gap_note"]

    async def test_trace_is_explainable(self):
        """每轮都留下 query / 分数 / 是否采纳 —— 面试与排障都靠这个。"""
        out = await search_applicable_law_node({
            "case_type": "contract_lending",
            "case_type_label": "民间借贷纠纷",
            "case_text": "借款50万元，约定月息2分，未约定还款期限，多次催要未果",
            "case_facts": {},
        })
        for row in out["retrieval_trace"]:
            assert {"round", "query", "top1", "hits", "added", "merged", "last"} <= set(row)


class TestColloquialRouting:
    """口语化提问的路由：不能因为出现「公司」就当成企业合规体检。"""

    def test_vague_firing_routes_to_case(self):
        from backend.orchestrator.orchestrator import classify_intent_rules

        assert classify_intent_rules("公司把我开了，能赔多少？") == ["case_analysis"]
        assert classify_intent_rules("公司突然把我炒了，有补偿吗") == ["case_analysis"]

    def test_enterprise_compliance_still_routes_to_compliance(self):
        from backend.orchestrator.orchestrator import classify_intent_rules

        assert classify_intent_rules("我们公司想做一次合规体检") == ["compliance_check"]


class TestNodeProgressEvents:
    """SSE 必须真的把 node 事件发出去（前端用它画进度时间线）。"""

    async def test_sse_emits_node_events_with_labels(self):
        import httpx

        from backend.api.app import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post("/api/v1/auth/login",
                                  json={"user_no": "L1001", "password": "law123456"})
            assert r.status_code == 200, r.text
            token = r.json()["token"]
            events: list[tuple[str, dict]] = []
            async with client.stream(
                "POST", "/api/v1/chat/stream",
                json={"message": "劳动仲裁时效是多久"},
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                assert resp.status_code == 200
                name, data = "", ""
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if name:
                            events.append((name, json.loads(data)))
                            name, data = "", ""
        nodes = [p for k, p in events if k == "node"]
        assert nodes, f"没有 node 事件，实际事件类型={[k for k, _ in events]}"
        assert all(p.get("label") for p in nodes), "每个 node 事件都要带中文标签"
        assert any(k == "result" for k, _ in events)


class TestLimitationKeywordFix:
    """时效语境词表：漏词的代价是把 1 年答成 3 年。"""

    async def test_non_compete_uses_labor_limitation(self):
        final = await limitation_faq_agent.ainvoke(
            {"inquiry_text": "竞业限制补偿金一直没给，主张权利的时效是多久",
             "thread_id": "t-faq-nc", "tenant_id": "tenant_firm",
             "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("t-faq-nc"),
        )
        answer = final["report"]["answer"]
        assert "1 年" in answer, f"竞业限制属劳动争议，应 1 年：{answer[:80]}"
        assert "第二十七条" in answer

    async def test_civil_contract_still_three_years(self):
        final = await limitation_faq_agent.ainvoke(
            {"inquiry_text": "借款合同的诉讼时效是多久",
             "thread_id": "t-faq-civil", "tenant_id": "tenant_firm",
             "user_id": "u_l1001", "pipeline_context": {}},
            config=_cfg("t-faq-civil"),
        )
        assert "3 年" in final["report"]["answer"]
