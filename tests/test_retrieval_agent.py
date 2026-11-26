"""检索子 Agent（P1）与关键条文核验打回（P2）的回归测试。

覆盖四类风险：
1. 自主循环不能改变融合口径（第八十七条仍要召回）；
2. 硬约束不能靠 prompt 自觉（候选不足不许收口、轮数上限、query 去重）；
3. 降级路径必须可跑（LLM 不可用 → 固定顺序三路）；
4. 开关关闭时行为与改造前一致（可回滚）。
"""
from __future__ import annotations

import pytest

from backend.agents import retrieval_agent as ra
from backend.agents.case_agent import (
    route_after_law_coverage,
    search_applicable_law_node,
    verify_law_coverage_node,
)
from backend.agents.retrieval_agent import build_retrieval_agent
from backend.core.config import settings

pytestmark = pytest.mark.usefixtures("_prepare_db")

_DEMO_CASE_TEXT = (
    "劳动者王某2019年3月入职杭州某科技有限公司，任高级工程师，月薪2万元，工作年限3年2个月。"
    "2022年5月10日，公司以「组织架构调整」为由口头通知解除劳动合同，未支付任何经济补偿。"
)

_FACTS = {"years_of_service_text": "3年2个月", "monthly_salary": 20000.0,
          "termination_manner": "口头解除"}


def _state(**over) -> dict:
    base = {
        "case_text": _DEMO_CASE_TEXT,
        "case_type": "labor_termination",
        "case_type_label": "违法解除劳动合同纠纷",
        "case_facts": _FACTS,
        "candidates": {},
        "rounds_data": [],
        "tried": [],
        "tried_actions": [],
        "duplicates": 0,
        "rounds": 0,
    }
    base.update(over)
    return base


class TestRetrievalAgentLoop:
    async def test_deterministic_loop_runs_three_routes_and_recalls_87(self):
        """LLM 不可用（pytest 默认 Mock）→ 固定顺序跑满三路，口径与旧版一致。"""
        out = await build_retrieval_agent().ainvoke(_state())

        assert out["rounds"] == 3
        trace = out["retrieval_trace"]
        assert 1 <= len(trace) <= 3
        assert trace[-1]["last"] is True
        assert {row["actor"] for row in trace} == {"policy", "fallback"}
        assert trace[0]["actor"] == "policy"
        cites = [law["cite"] for law in out["laws"]]
        assert any("第八十七条" in c for c in cites), f"未召回第八十七条，实际={cites}"

    async def test_finish_is_rejected_when_candidates_insufficient(self, monkeypatch):
        """候选不足 3 条时，协调器即便想收口也必须再搜一轮（硬约束）。"""
        monkeypatch.setattr(type(settings), "llm_enabled", property(lambda self: True))

        async def _fake_decide(_state):
            return "finish", ""

        monkeypatch.setattr(ra, "_llm_decide", _fake_decide)
        out = await ra.coordinator_node(_state(candidates={}))
        assert out["last_action"] != "finish"

    async def test_first_round_is_always_term_search(self, monkeypatch):
        """第一轮固定术语短查询，不烧模型调用（省钱且路径可预测）。"""
        monkeypatch.setattr(type(settings), "llm_enabled", property(lambda self: True))

        async def _fake_decide(_state):
            return "finish", ""

        monkeypatch.setattr(ra, "_llm_decide", _fake_decide)
        out = await ra.coordinator_node(_state(rounds=0))
        assert out["last_action"] == "term_search"

    async def test_retry_hint_becomes_first_query(self):
        """核验打回带的缺口提示，必须成为下一轮的第一条检索式（否则补检无意义）。"""
        hint = "违法解除劳动合同纠纷 第八十七条"
        out = await ra.coordinator_node(_state(retry_hint=hint))
        assert out["last_action"] == "term_search"
        assert out["plan_query"] == hint

        # 已经搜过该提示 → 不再重复，回到正常策略
        out2 = await ra.coordinator_node(_state(retry_hint=hint, tried=[hint]))
        assert not (out2["last_action"] == "term_search" and out2.get("plan_query") == hint)

    async def test_round_cap_forces_finish(self):
        """轮数到上限 → 无条件收口，不再调模型。"""
        out = await ra.coordinator_node(_state(rounds=4))
        assert out["last_action"] == "finish"

    async def test_duplicate_query_is_not_researched(self):
        """同一个 query 不重复检索（防模型原地打转）。"""
        label = "违法解除劳动合同纠纷"
        query = f"{label} 工作3年2个月 月薪 口头解除"
        out = await ra.term_search_node(_state(tried=[query], tried_actions=[]))
        assert out["duplicates"] == 1
        assert out.get("rounds", 0) == 0
        assert out.get("rounds_data", []) == []

    async def test_switch_off_matches_legacy_output(self, monkeypatch):
        """开关关闭 → 走旧三路，trace 不带 actor 字段，结果可比。"""
        monkeypatch.setattr(settings, "retrieval_agent_enabled", False)
        out = await search_applicable_law_node({
            "case_type": "labor_termination",
            "case_type_label": "违法解除劳动合同纠纷",
            "case_text": _DEMO_CASE_TEXT,
            "case_facts": _FACTS,
        })
        assert out["applicable_law"]
        assert all("actor" not in row for row in out["retrieval_trace"])

    async def test_switch_on_uses_agent_and_keeps_trace_contract(self, monkeypatch):
        """开关打开 → 走检索子 Agent，trace 仍满足前端契约（last/actor 齐全）。"""
        monkeypatch.setattr(settings, "retrieval_agent_enabled", True)
        out = await search_applicable_law_node({
            "case_type": "labor_termination",
            "case_type_label": "违法解除劳动合同纠纷",
            "case_text": _DEMO_CASE_TEXT,
            "case_facts": _FACTS,
        })
        trace = out["retrieval_trace"]
        assert trace and trace[-1]["last"] is True
        assert {"round", "query", "top1", "hits", "added", "merged", "last", "actor"} <= set(trace[0])
        assert any("第八十七条" in law["cite"] for law in out["applicable_law"])


class TestLawCoverageVerify:
    async def test_missing_key_article_triggers_retry(self):
        """缺请求权基础条文 → 给出缺口提示并打回检索。"""
        out = await verify_law_coverage_node({
            "case_type": "labor_termination", "case_type_label": "违法解除劳动合同纠纷",
            "applicable_law": [], "law_retry": 0,
        })
        assert "第八十七条" in out["law_coverage"]["missing"]
        assert out["law_retry"] == 1
        assert "第八十七条" in out["retry_hint"]
        assert route_after_law_coverage({**out, "law_coverage": out["law_coverage"]}) == \
            "search_applicable_law"

    async def test_present_article_passes(self):
        out = await verify_law_coverage_node({
            "case_type": "labor_termination",
            "case_type_label": "违法解除劳动合同纠纷",
            "applicable_law": [{"cite": "劳动合同法第八十七条"}],
            "law_retry": 0,
        })
        assert out["law_coverage"]["missing"] == []
        assert route_after_law_coverage(out) == "match_precedent_cases"

    async def test_retry_exhausted_declares_gap_not_loops(self):
        """补检额度用完后不再打回，明确写缺口说明（不死循环）。"""
        out = await verify_law_coverage_node({
            "case_type": "labor_termination", "case_type_label": "违法解除劳动合同纠纷",
            "applicable_law": [], "law_retry": 1,
        })
        assert out["law_coverage"]["missing"]
        assert "未召回" in out["law_gap_note"]
        assert route_after_law_coverage(out) == "match_precedent_cases"

    async def test_non_labor_case_has_no_requirement(self):
        """未配置关键条文的案由不做覆盖拦截（宁缺勿假，不硬凑）。"""
        out = await verify_law_coverage_node({
            "case_type": "contract_lending", "case_type_label": "民间借贷纠纷",
            "applicable_law": [], "law_retry": 0,
        })
        assert out["law_coverage"]["missing"] == []
