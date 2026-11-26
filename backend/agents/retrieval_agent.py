"""检索子 Agent —— 受限自主循环（P1）。

改造前后差别：
  改造前：三路 query 写死，跑满三路，取并集；
  改造后：协调器看「已有什么、还缺什么」，自己决定下一轮用哪种问法，
         最多 4 轮；候选不足 3 条时不许收口；LLM 不可用按固定顺序降级。

自主权边界（写在代码里，不靠 prompt 自觉）：
  1. 轮数硬上限（settings.retrieval_agent_max_rounds，默认 4）；
  2. 同一个 query 不重复检索；
  3. 候选 <3 条时 finish 无效；
  4. 协调器输出非法/调用失败 → 按 term → pack → raw 的顺序补跑。

融合口径与旧三路完全一致（law_queries.merge_law_hits），保证评测可比。
"""
from __future__ import annotations

import json
import logging
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents.law_queries import CASE_TERMS, build_law_queries, fact_terms, merge_law_hits
from backend.core.config import settings
from backend.core.llm import ainvoke_with_fallback
from backend.tools.mcp_tools import search_law_knowledge_base

logger = logging.getLogger(__name__)

_ACTIONS = ("term_search", "case_pack", "raw_search", "finish")

_PROMPT = """你是法律检索策略员。目标：为下面的案子找到适用法条，最多还能搜 {remain} 轮。

案情：{case_text}
案由：{case_type_label}
已找到 {n} 条候选：{brief}
已搜过：{tried}

可选动作（只能选一个）：
1. term_search：术语式短查询（精度高），query 形如「违法解除 经济补偿 赔偿金」
2. case_pack：用本类案由的标准术语包搜（补用户没提但这类案子必须看的条文）
3. raw_search：用原文关键片段搜（捞长尾细节，如「没签书面合同」「试用期」）
4. finish：认为候选已足够

判断标准：
- 候选 <3 条 → 不能 finish
- 候选缺程序类（通知工会/举证责任）或时效类条文 → 优先 case_pack
- 案情有具体细节但候选没覆盖 → 优先 raw_search
- 同一问法不要重复

只输出 JSON：{{"action":"term_search|case_pack|raw_search|finish","query":"...","reason":"一句话"}}"""


class RetrievalState(TypedDict, total=False):
    """检索子图的状态总线（不污染 CaseState）。"""

    case_text: str
    case_type: str
    case_type_label: str
    case_facts: dict
    # 运行态
    candidates: dict[str, dict]       # cite -> 条文（去重/计分用）
    rounds_data: list[dict]           # 每轮 {action, query, hits, actor}
    tried: list[str]                  # 已检索过的 query
    tried_actions: list[str]          # 已执行过的动作
    duplicates: int                   # 重复 query 次数（可观测）
    rounds: int
    max_rounds: int
    last_action: str
    plan_query: str
    degraded: bool
    actor_hint: str                   # 本轮决策来源：agent / fallback / policy
    retry_hint: str                   # 核验打回时带的缺口提示
    # 输出
    laws: list[dict]
    retrieval_trace: list[dict]
    law_gap_note: str


def _brief(cands: dict[str, dict], limit: int = 4) -> str:
    items = sorted(cands.values(), key=lambda x: x.get("score", 0), reverse=True)[:limit]
    return "；".join(f"{c.get('cite', '')}({c.get('score', 0):.2f})" for c in items) or "（无）"


def _next_deterministic(state: RetrievalState) -> str:
    """LLM 不可用/失败时的固定顺序：term → pack → raw → finish。"""
    tried = state.get("tried_actions", [])
    for action in ("term_search", "case_pack", "raw_search"):
        if action not in tried:
            return action
    return "finish"


async def _llm_decide(state: RetrievalState) -> tuple[str, str]:
    """让协调器做一次判断；返回 (action, query)。"""
    rounds = state.get("rounds", 0)
    max_rounds = state.get("max_rounds") or settings.retrieval_agent_max_rounds
    prompt = _PROMPT.format(
        remain=max(0, max_rounds - rounds),
        case_text=(state.get("case_text", "") or "")[:600],
        case_type_label=state.get("case_type_label", ""),
        n=len(state.get("candidates", {})),
        brief=_brief(state.get("candidates", {})),
        tried="；".join(state.get("tried", [])) or "（无）",
    )
    raw = await ainvoke_with_fallback(prompt)
    data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    action = str(data.get("action", ""))
    if action not in _ACTIONS:
        raise ValueError(f"非法动作：{action}")
    return action, str(data.get("query", "") or "")


async def coordinator_node(state: RetrievalState) -> dict:
    """决策节点：只决定下一步，不执行检索。"""
    rounds = state.get("rounds", 0)
    max_rounds = state.get("max_rounds") or settings.retrieval_agent_max_rounds
    cands = state.get("candidates", {})

    if rounds >= max_rounds:
        return {"last_action": "finish"}

    # 核验打回：优先按缺口提示补搜一轮（提示本身就是一条术语式 query）
    hint = (state.get("retry_hint") or "").strip()
    if hint and hint not in state.get("tried", []):
        return {"last_action": "term_search", "plan_query": hint,
                "degraded": False, "actor_hint": "policy"}

    # 第一轮固定走术语短查询（改造前的最高精度问法），把模型调用留给
    # 「第一轮结果出来后还缺什么」这种真需要判断力的决策 —— 省钱且可预测。
    if rounds == 0 and "term_search" not in state.get("tried_actions", []):
        return {"last_action": "term_search", "degraded": False, "actor_hint": "policy"}

    if not settings.llm_enabled:
        return {"last_action": _next_deterministic(state), "degraded": True,
                "actor_hint": "fallback"}

    try:
        action, query = await _llm_decide(state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("检索协调器降级为固定顺序：%s", exc)
        return {"last_action": _next_deterministic(state), "degraded": True,
                "actor_hint": "fallback"}

    # 硬约束：候选不足不许收口
    if action == "finish" and len(cands) < 3:
        action = _next_deterministic({**state, "last_action": "finish"})
        return {"last_action": action, "plan_query": query, "degraded": True,
                "actor_hint": "fallback"}
    return {"last_action": action, "plan_query": query, "degraded": False,
            "actor_hint": "agent"}


def _query_for(state: RetrievalState, action: str) -> str:
    label = state.get("case_type_label", "") or ""
    if action == "term_search":
        planned = (state.get("plan_query") or "").strip()
        if planned:
            return planned
        return (
            f"{label} {fact_terms(state.get('case_facts', {}))}".strip()
            or (state.get("case_text", "") or "")[:60]
        )
    if action == "case_pack":
        return CASE_TERMS.get(state.get("case_type", ""), label)
    return f"{label} {(state.get('case_text', '') or '')[:120]}".strip()


async def _run_search(state: RetrievalState, action: str) -> dict:
    query = _query_for(state, action)
    tried = list(state.get("tried", []))
    tried_actions = list(state.get("tried_actions", []))

    if not query:
        return {"last_action": action, "tried_actions": [*tried_actions, action]}

    # 同一 query 不重复检索（可观测：duplicates 计数）
    if query in tried:
        return {
            "last_action": action,
            "tried_actions": [*tried_actions, action],
            "duplicates": state.get("duplicates", 0) + 1,
        }

    hits = await search_law_knowledge_base(query, top_k=6)
    candidates = dict(state.get("candidates", {}))
    for h in hits:
        key = h.get("cite") or f"{h.get('law', '')}{h.get('article_no', '')}"
        if key not in candidates or h.get("score", 0) > candidates[key].get("score", 0):
            candidates[key] = h

    return {
        "candidates": candidates,
        "rounds_data": [
            *state.get("rounds_data", []),
            {
                "action": action,
                "query": query,
                "hits": hits,
                "actor": state.get("actor_hint")
                or ("fallback" if state.get("degraded") else "agent"),
            },
        ],
        "rounds": state.get("rounds", 0) + 1,
        "tried": [*tried, query],
        "tried_actions": [*tried_actions, action],
        "last_action": action,
    }


async def term_search_node(state: RetrievalState) -> dict:
    return await _run_search(state, "term_search")


async def case_pack_node(state: RetrievalState) -> dict:
    return await _run_search(state, "case_pack")


async def raw_search_node(state: RetrievalState) -> dict:
    return await _run_search(state, "raw_search")


def finalize_node(state: RetrievalState) -> dict:
    """收口：复用旧三路的融合口径，产出 laws + trace（+ 知识库缺口说明）。"""
    rounds = state.get("rounds_data", [])
    laws, trace = merge_law_hits(rounds)
    out: dict = {"laws": laws, "retrieval_trace": trace}
    if not laws:
        out["law_gap_note"] = (
            f"法条库未收录「{state.get('case_type_label', '')}」相关条文，"
            "本次分析未引用具体法条，请人工核对法规库后补充"
        )
    return out


def route_after_coordinator(state: RetrievalState) -> str:
    action = state.get("last_action", "finish")
    if action in ("term_search", "case_pack", "raw_search"):
        return action
    return "finalize"


def build_retrieval_agent():
    g = StateGraph(RetrievalState)
    g.add_node("coordinator", coordinator_node)
    g.add_node("term_search", term_search_node)
    g.add_node("case_pack", case_pack_node)
    g.add_node("raw_search", raw_search_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "coordinator")
    g.add_conditional_edges(
        "coordinator", route_after_coordinator,
        {
            "term_search": "term_search",
            "case_pack": "case_pack",
            "raw_search": "raw_search",
            "finalize": "finalize",
        },
    )
    for name in ("term_search", "case_pack", "raw_search"):
        g.add_edge(name, "coordinator")
    g.add_edge("finalize", END)
    return g.compile()
