"""CaseAgent 案件分析 —— 12 节点主链 + 1 个追问侧分支（规格文档拓扑，节点名严格一致）。

    classify_case_type → extract_case_facts → clarify_slots ─┬→ search_applicable_law
                                                             │     → verify_law_coverage（缺关键条文打回检索）
                                                             │     → match_precedent_cases
                                                             │     → analyze_legal_issues
                                                             │     → calculate_limitation
                                                             │     → estimate_win_rate
                                                             │     → generate_analysis_framework
                                                             │     → review_draft（复核不过打回起草）
                                                             │     → save_case_analysis
                                                             └→ ask_clarify（信息不足，出追问卡即止）

处理意图：case_analysis / precedent_search（precedent_search 在节点 3~4
加权类案检索、精简焦点分析）+ limitation_inquiry（时效咨询 FAQ 小图，
纯咨询无文书场景：规则直出 + 引用回库验真，不落库）。

三个确定性辅助设施（节点内，不占工具名额，规格文档归并口径）：
  _extract_case_facts  正则抽事实（年限/月薪/解除方式）
  _estimate_win_rate   证据评分估计胜诉率
  clarify_slots        槽位完整性检查（backend/agents/slots.py）
原则同规则引擎：能确定性的不交给模型。
"""
from __future__ import annotations

import json
import logging
import re

from langgraph.graph import END, START, StateGraph

from backend.agents.law_queries import (
    CASE_TERMS as _CASE_TERMS,
    build_law_queries as _build_law_queries,
    fact_terms as _fact_terms,
    merge_law_hits,
)
from backend.agents.retrieval_agent import build_retrieval_agent
from backend.agents.review import review_case_output
from backend.agents.slots import build_clarify_text, check_slots, should_block
from backend.agents.state import CaseState, LimitationFaqState
from backend.core.config import settings
from backend.core.security import new_id
from backend.memory.checkpointer import get_checkpointer
from backend.memory.models import CaseAnalysis, SessionLocal
from backend.retrieval.precedents import CASE_TYPE_LABELS
from backend.retrieval.statutes import citation_exists, extract_citations, get_article
from backend.tools.api_tools import get_case_detail
from backend.tools.llm_tools import (
    llm_analyze_legal_risk,
    llm_classify_case_type,
    llm_generate_legal_opinion,
    llm_match_precedent,
)
from backend.tools.mcp_tools import search_law_knowledge_base, search_precedent_cases
from backend.tools.rule_tools import calculate_claim_amount, calculate_limitation_period

logger = logging.getLogger(__name__)


# ── 节点 1：classify_case_type 案由分类 ──────────────────────


async def classify_case_type_node(state: CaseState) -> dict:
    # 优先用外部案件管理系统的登记案由（有的话），否则 LLM 分类
    case_id = state.get("case_id", "")
    if case_id:
        try:
            detail = await get_case_detail(case_id)
            ct = detail.get("case_type", "")
            if ct in CASE_TYPE_LABELS:
                return {
                    "case_type": ct,
                    "case_type_label": CASE_TYPE_LABELS[ct],
                    "case_text": detail.get("case_text", state.get("case_text", "")),
                    "summary": f"已从案件管理系统调取档案 {case_id}（{detail.get('case_title', '')}）",
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_case_detail 降级 LLM 分类：%s", exc)
    if state.get("focus_case_type"):
        ct = state["focus_case_type"]
        return {"case_type": ct, "case_type_label": CASE_TYPE_LABELS.get(ct, ct)}
    result = await llm_classify_case_type(state["case_text"])
    return {
        "case_type": result["case_type"],
        "case_type_label": result["case_type_label"],
        "summary": f"案由分类：{result['case_type_label']}（置信度 {result['confidence']:.2f}，"
                   f"{'LLM' if result['engine'] == 'llm' else '规则降级'}）",
    }


# ── 确定性事实抽取（正则，可复现）─────────────────────────────


def _extract_case_facts(text: str) -> dict:
    facts: dict = {}
    # 工作年限：「工作年限3年2个月」「工作3年」「入职...年」
    m = re.search(r"工作(?:年限)?[为约了]?\s*(\d{1,2})\s*年(?:\s*(?:零|又)?\s*(\d{1,2})\s*个?月)?", text)
    if m:
        years = int(m.group(1))
        months = int(m.group(2) or 0)
        facts["years_of_service"] = round(years + months / 12, 2)
        facts["years_of_service_text"] = f"{years}年{months}个月" if months else f"{years}年"
    else:
        # 回退：用「入职月份 → 解除月份」日期差折算年限（2023年2月入职…2026年4月被辞退）
        mi = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月[^，。;；]{0,6}?入职", text)
        mt = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月[^，。;；]{0,10}?(解除|辞退|解雇)", text)
        if mi and mt:
            total = (int(mt.group(1)) - int(mi.group(1))) * 12 + (int(mt.group(2)) - int(mi.group(2)))
            if 0 < total < 480:
                facts["years_of_service"] = round(total / 12, 2)
                facts["years_of_service_text"] = f"{total // 12}年{total % 12}个月"
    # 月薪：「月薪2万元」「月工资15,000元」
    m = re.search(r"月(?:薪|工资|平均工资)[为约]?\s*([\d,，\.]+)\s*(万)?元", text)
    if m:
        val = float(m.group(1).replace(",", "").replace("，", ""))
        facts["monthly_salary"] = val * 10000 if m.group(2) else val
    # 解除方式
    if re.search(r"口头(通知)?(解除|辞退|解雇)", text):
        facts["termination_manner"] = "口头解除"
    elif re.search(r"书面(通知)?(解除|辞退)", text):
        facts["termination_manner"] = "书面解除"
    elif "辞退" in text or "解雇" in text or "解除劳动合同" in text:
        facts["termination_manner"] = "解除（方式未明）"
    # 解除时间（时效起算参考）
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月[^，。;；]{0,10}?(解除|辞退|解雇)", text)
    if m:
        facts["termination_date"] = f"{m.group(1)}-{int(m.group(2)):02d}"
    # 是否拖欠工资
    facts["dispute_kind"] = "wage_arrears" if ("拖欠" in text and "工资" in text) else "other"
    facts["relationship_ongoing"] = "在职" in text and "解除" not in text
    return facts


async def extract_case_facts_node(state: CaseState) -> dict:
    facts = _extract_case_facts(state["case_text"])
    # 违法解除场景：直接用规则工具算赔偿（确定性，规则直出）
    claim: dict = {}
    if state.get("case_type") == "labor_termination" and facts.get("years_of_service") \
            and facts.get("monthly_salary"):
        claim = calculate_claim_amount(
            scenario="wrongful_termination",
            years_of_service=facts["years_of_service"],
            monthly_salary=facts["monthly_salary"],
        )
        facts["claim_amount"] = {
            "formula": claim["formula"],
            "months": claim["months"],
            "amount": claim["amount"],
            "amount_text": f"{claim['amount']:,.0f} 元",
            "basis": claim["legal_basis"],
        }
    return {"case_facts": facts}


# ── 节点 3：clarify_slots 信息完整性检查（确定性，检索前拦一道）──
#
# 为什么插在检索前：检索不准，多数时候不是算法问题，是 query 信息量不足。
# 实测（同一套 TF-IDF，只换 query）：
#   整段案情  → top1 0.090，《劳动合同法》第八十七条不在 top-6
#   术语短句  → top1 0.343，第八十七条排名第一
# 信息在用户脑子里，库里没有 → 只能问，不能猜。


async def clarify_slots_node(state: CaseState) -> dict:
    """检查关键槽位；信息不足时把「缺什么」显式化，而不是静默猜。"""
    # 用户点名案由找类案（precedent_search）、或档案来自案件管理系统 → 不追问
    if state.get("focus_case_type") or state.get("case_id"):
        return {"clarify": {"skipped": "已指定案由或来自案件管理系统", "coverage": 1.0,
                            "blocked": False}}
    report = check_slots(
        state.get("case_type", ""), state.get("case_facts", {}), state.get("case_text", "")
    )
    blocked, reason = should_block(report, state.get("case_text", ""))
    payload = report.to_dict()
    payload.update({
        "blocked": blocked,
        "reason": reason,
        "ask_text": build_clarify_text(report) if blocked else "",
    })
    out: dict = {"clarify": payload}
    if blocked:
        out["clarify_pending"] = True
        out["summary"] = payload["ask_text"]
    return out


def route_after_clarify(state: CaseState) -> str:
    """条件边：信息不足 → 走追问分支；否则正常检索。"""
    return "ask_clarify" if (state.get("clarify") or {}).get("blocked") else "search_applicable_law"


async def ask_clarify_node(state: CaseState) -> dict:
    """追问分支终点：只输出「需要补充什么」，不检索、不出结论、不落库。"""
    clarify = state.get("clarify") or {}
    summary = clarify.get("ask_text") or "信息不足，请补充关键事实后重试。"
    return {"summary": summary, "clarify_pending": True}


# ── 节点 4：search_applicable_law 法条检索（3 路召回融合）──────────
#
# 不是「换个检索引擎」，而是「换个问法」。实测同一套 TF-IDF：
#   整段案情拼 query → top1 0.090，《劳动合同法》第八十七条不在 top-6
#   术语式短 query   → top1 0.343，第八十七条排名第一
# 但只用短 query 会丢掉长尾具体条文（实测：试用期、二倍工资两题漏召回），
# 所以不做取舍，做**并集**：
#   第 1 路 术语短查询（精度高）→ 第 2 路 案由术语包（扩召回）
#   → 第 3 路 原文长查询（保底，等价改动前行为）
# 按 cite 去重、按分数排序取 top-6。三路都跑（内存检索，毫秒级）——
# 提前收敛会让第 3 路召回的「二倍工资」类长尾条文永远轮不到（实测踩过）。

# 三路检索式与融合口径已抽到 law_queries（旧三路 / 检索子 Agent 共用，
# 保证改造前后结果逐字段可比）。这里保留同名薄封装，测试与旧调用不受影响。


def build_law_queries(state: CaseState) -> list[str]:
    """三路检索式：术语短查询 → 案由术语包 → 原文长查询（顺序即优先级）。"""
    return _build_law_queries(
        state.get("case_type", ""),
        state.get("case_type_label", "") or "",
        state.get("case_facts", {}) or {},
        state.get("case_text", "") or "",
    )


# 检索子 Agent 编译一次即可（无 checkpointer，状态由父图线程持有）
_RETRIEVAL_AGENT = None


def _get_retrieval_agent():
    global _RETRIEVAL_AGENT
    if _RETRIEVAL_AGENT is None:
        _RETRIEVAL_AGENT = build_retrieval_agent()
    return _RETRIEVAL_AGENT


async def _search_fixed_three_routes(state: CaseState) -> dict:
    """改造前口径：三路写死、跑满、取并集（回滚与评测基线）。"""
    rounds: list[dict] = []
    for query in build_law_queries(state):
        hits = await search_law_knowledge_base(query, top_k=6)
        rounds.append({"query": query, "hits": hits})
    laws, trace = merge_law_hits(rounds)
    return _law_output(state, laws, trace)


async def _search_with_agent(state: CaseState) -> dict:
    """改造后口径：检索子 Agent 受限自主循环（≤4 轮，失败按固定顺序降级）。"""
    out = await _get_retrieval_agent().ainvoke({
        "case_text": state.get("case_text", ""),
        "case_type": state.get("case_type", ""),
        "case_type_label": state.get("case_type_label", ""),
        "case_facts": state.get("case_facts", {}) or {},
        "retry_hint": state.get("retry_hint", ""),
        "candidates": {},
        "rounds_data": [],
        "tried": [],
        "tried_actions": [],
        "duplicates": 0,
        "rounds": 0,
    })
    return _law_output(state, out.get("laws", []), out.get("retrieval_trace", []),
                       out.get("law_gap_note", ""))


def _law_output(state: CaseState, laws: list[dict], trace: list[dict], gap_note: str = "") -> dict:
    out: dict = {"applicable_law": laws, "retrieval_trace": trace}
    note = gap_note or (
        f"法条库未收录「{state.get('case_type_label', '')}」相关条文，"
        "本次分析未引用具体法条，请人工核对法规库后补充"
        if not laws else ""
    )
    if note:
        out["law_gap_note"] = note
    return out


async def search_applicable_law_node(state: CaseState) -> dict:
    """法条检索节点：开关控制走「检索子 Agent」还是「旧三路写死」。"""
    if settings.retrieval_agent_enabled:
        return await _search_with_agent(state)
    return await _search_fixed_three_routes(state)


# ── 节点 4b：verify_law_coverage 关键条文覆盖核验（P2 打回闭环）──
#
# 检索 Agent 自己决定搜几轮，但「够不够」不能由检索自己说了算：
# 本案由的请求权基础条文没召回 → 带着缺口打回检索补搜（最多 1 次）；
# 补不齐就明说缺哪条，不拿相近条文硬凑。

_KEY_ARTICLES: dict[str, tuple[str, ...]] = {
    "labor_termination": ("第八十七条",),      # 违法解除 2N 的请求权基础
    "labor_probation": ("第十九条",),          # 试用期上限
    "labor_non_compete": ("第二十三条",),      # 竞业限制补偿
    "labor_service_period": ("第二十二条",),   # 服务期违约金
}
_MAX_LAW_RETRY = 1


async def verify_law_coverage_node(state: CaseState) -> dict:
    required = _KEY_ARTICLES.get(state.get("case_type", ""), ())
    cites = [str(law.get("cite", "")) for law in state.get("applicable_law", [])]
    missing = [c for c in required if not any(c in cite for cite in cites)]
    retry = int(state.get("law_retry", 0) or 0)
    out: dict = {"law_coverage": {"required": list(required),
                                  "missing": missing, "retry": retry}}
    if not missing:
        return out
    if retry < _MAX_LAW_RETRY:
        out["law_retry"] = retry + 1
        out["retry_hint"] = f"{state.get('case_type_label', '')} {missing[0]}"
        return out
    out["law_gap_note"] = (
        f"关键条文（{'、'.join(missing)}）经 {retry} 次补检仍未召回，"
        "结论可能缺少请求权基础，请人工核对"
    )
    return out


def route_after_law_coverage(state: CaseState) -> str:
    """条件边：缺关键条文且还有补检额度 → 打回检索；否则继续分析。"""
    cov = state.get("law_coverage") or {}
    if cov.get("missing") and int(cov.get("retry", 0) or 0) < _MAX_LAW_RETRY:
        return "search_applicable_law"
    return "match_precedent_cases"


# ── 节点 4：match_precedent_cases 类案检索（MCP + LLM 重排）────


async def match_precedent_cases_node(state: CaseState) -> dict:
    candidates = await search_precedent_cases(
        state["case_text"][:150], case_type=state.get("case_type"), top_k=3
    )
    ranked = await llm_match_precedent(state["case_text"][:200], candidates)
    for i, c in enumerate(ranked, 1):
        c["rank"] = i
    return {"precedents": ranked}


# ── 节点 5：analyze_legal_issues 争议焦点（LLM 工具）──────────


async def analyze_legal_issues_node(state: CaseState) -> dict:
    issues = await llm_analyze_legal_risk(
        state.get("case_type", ""), state.get("case_facts", {}), state.get("applicable_law", [])
    )
    return {"legal_issues": issues}


# ── 节点 6：calculate_limitation 时效计算（规则工具）──────────


def calculate_limitation_node(state: CaseState) -> dict:
    facts = state.get("case_facts", {})
    limitation = calculate_limitation_period(
        case_type=state.get("case_type", ""),
        dispute_kind=facts.get("dispute_kind", "other"),
        relationship_ongoing=facts.get("relationship_ongoing", False),
    )
    return {"limitation": limitation}


# ── 确定性胜诉率估计（证据评分）──────────────────────────────


def _estimate_win_rate(case_type: str, text: str, facts: dict, issues: list[dict]) -> tuple[float, str]:
    score = 0.35
    reasons = []
    if case_type == "labor_termination":
        # 用人单位未举证法定解除情形 → 我方核心优势
        if not re.search(r"(严重违反|规章制度|不能胜任|协商一致|经济性裁员)", text):
            score += 0.30
            reasons.append("用人单位缺少法定解除情形依据")
        else:
            score += 0.05
            reasons.append("用人单位主张了法定解除情形，需逐项质证")
        if facts.get("termination_manner") == "口头解除":
            score += 0.10
            reasons.append("口头解除程序瑕疵明显")
    favor = sum(1 for i in issues if i.get("favor") == "对我方")
    if favor >= 2:
        score += 0.10
        reasons.append(f"争议焦点分析 {favor} 项有利")
    if reasons:
        return min(0.92, round(score, 2)), "；".join(reasons)
    return min(0.6, max(0.3, score)), "证据与情形均衡，需补强举证"


async def estimate_win_rate_node(state: CaseState) -> dict:
    rate, reason = _estimate_win_rate(
        state.get("case_type", ""), state["case_text"], state.get("case_facts", {}),
        state.get("legal_issues", []),
    )
    label = "高" if rate >= 0.7 else ("中" if rate >= 0.5 else "低")
    return {
        "win_rate": rate,
        "win_rate_reason": f"胜诉概率评估：{label}（{rate:.0%}）—— {reason}",
    }


# ── 节点 8：generate_analysis_framework 分析框架（LLM 工具）────


async def generate_analysis_framework_node(state: CaseState) -> dict:
    context = {
        "title": f"{state.get('case_type_label', '案件')}分析框架与工作大纲",
        "kind": "framework",
        "case_type_label": state.get("case_type_label", ""),
        "facts": state.get("case_facts", {}),
        "issues": state.get("legal_issues", []),
        "limitation": state.get("limitation", {}),
        "claim_amount": state.get("case_facts", {}).get("claim_amount"),
        "win_rate_reason": state.get("win_rate_reason", ""),
        "precedents": [
            {"case_no": p.get("case_no", ""), "topic": p.get("topic", "")}
            for p in state.get("precedents", [])
        ],
        "conclusion": (
            f"诉讼要点：围绕{state.get('case_type_label', '')}争议焦点组织证据；"
            f"注意时效（{state.get('limitation', {}).get('expires_hint', '')}）；"
            f"请求权基础见争议焦点分析。"
        ),
    }
    if state.get("review_feedback"):
        # 复核打回时把「上一版哪里有问题」交给起草节点，让它针对性重写
        context["review_feedback"] = state["review_feedback"]
    framework_text = await llm_generate_legal_opinion(context)
    framework = {
        "title": context["title"],
        "text": framework_text,
        "evidence_checklist": [
            "劳动合同 / 用工事实证据", "工资流水与薪酬记录", "解除通知或辞退记录",
            "考勤与工时记录", "社保缴纳记录", "沟通记录（微信/邮件）",
        ] if state.get("case_type", "").startswith("labor") else [
            "合同原件及附件", "履行凭证（付款/交付记录）", "往来函件", "损失证明",
        ],
    }
    return {"framework": framework}


# ── 节点 9b：review_draft 复核红队（P3）────────────────────────
#
# 落库前挑自己人的错：过强表述 / 无条文依据 / 时效缺失。
# 打回起草重写最多 1 次；仍不过 → forced_pass 标「存在争议」，交人审。
# 不追求"改到看起来没问题"——法律场景宁可交人，也不让系统自我美化。

_MAX_REVIEW_RETRY = 1


async def review_draft_node(state: CaseState) -> dict:
    findings, verdict = review_case_output(state)
    retry = int(state.get("review_retry", 0) or 0)
    high_fixes = "；".join(f["fix"] for f in findings if f["level"] == "high")
    out: dict = {"review_findings": findings, "review_verdict": verdict}

    if verdict != "reject":
        return out
    if retry < _MAX_REVIEW_RETRY:
        out["review_retry"] = retry + 1
        out["review_feedback"] = high_fixes
        return out
    # 已给过一次机会仍未修正 → 不再自动重写，标注争议交人工
    out["review_verdict"] = "forced_pass"
    out["review_feedback"] = high_fixes
    return out


def route_after_review(state: CaseState) -> str:
    """条件边：复核不通过且还有重写额度 → 打回起草；否则落库。"""
    if state.get("review_verdict") == "reject" \
            and int(state.get("review_retry", 0) or 0) <= _MAX_REVIEW_RETRY:
        return "generate_analysis_framework"
    return "save_case_analysis"


# ── 节点 9：save_case_analysis 落库（永久保存）────────────────


async def save_case_analysis_node(state: CaseState) -> dict:
    case_id = new_id("case")
    async with SessionLocal() as session:
        session.add(CaseAnalysis(
            case_id=case_id,
            thread_id=state.get("thread_id", ""),
            tenant_id=state.get("tenant_id", "tenant_firm"),
            user_id=state.get("user_id", ""),
            case_title=state["case_text"][:40],
            case_type=state.get("case_type", ""),
            case_facts_json=json.dumps(state.get("case_facts", {}), ensure_ascii=False),
            applicable_law_json=json.dumps(state.get("applicable_law", []), ensure_ascii=False),
            precedents_json=json.dumps(state.get("precedents", []), ensure_ascii=False),
            issues_json=json.dumps(state.get("legal_issues", []), ensure_ascii=False),
            limitation_text=state.get("limitation", {}).get("period_text", ""),
            win_rate=state.get("win_rate"),
            win_rate_reason=state.get("win_rate_reason", ""),
            framework_json=json.dumps(state.get("framework", {}), ensure_ascii=False),
        ))
        await session.commit()
    prefix = f"案件 {state['case_id']}｜" if state.get("case_id") else ""
    summary = (
        f"{prefix}案件分析完成。案由：{state.get('case_type_label', '-')}；"
        f"时效：{state.get('limitation', {}).get('period_text', '-')}；"
        f"{state.get('win_rate_reason', '')}"
    )
    claim = state.get("case_facts", {}).get("claim_amount")
    if claim:
        summary += f"；违法解除赔偿金估算：{claim['formula']} = {claim['amount_text']}（{claim['basis']}）"
    if state.get("law_gap_note"):
        summary += f"；注意：{state['law_gap_note']}"
    return {
        "saved_case_id": case_id,
        "summary": summary,
        "case_facts": {**state.get("case_facts", {}), "claim_amount": claim} if claim else state.get("case_facts", {}),
    }


# ── 图装配 ───────────────────────────────────────────────────

# 主链（串行）；ask_clarify 是 clarify_slots 条件边上的侧分支，不在主链里
CHAIN = [
    "classify_case_type", "extract_case_facts", "clarify_slots", "search_applicable_law",
    "verify_law_coverage",
    "match_precedent_cases", "analyze_legal_issues", "calculate_limitation",
    "estimate_win_rate", "generate_analysis_framework", "review_draft", "save_case_analysis",
]
NODES = CHAIN + ["ask_clarify"]


def build_case_agent():
    g = StateGraph(CaseState)
    for name, fn in (
        ("classify_case_type", classify_case_type_node),
        ("extract_case_facts", extract_case_facts_node),
        ("clarify_slots", clarify_slots_node),
        ("ask_clarify", ask_clarify_node),
        ("search_applicable_law", search_applicable_law_node),
        ("verify_law_coverage", verify_law_coverage_node),
        ("match_precedent_cases", match_precedent_cases_node),
        ("analyze_legal_issues", analyze_legal_issues_node),
        ("calculate_limitation", calculate_limitation_node),
        ("estimate_win_rate", estimate_win_rate_node),
        ("generate_analysis_framework", generate_analysis_framework_node),
        ("review_draft", review_draft_node),
        ("save_case_analysis", save_case_analysis_node),
    ):
        g.add_node(name, fn)
    g.add_edge(START, "classify_case_type")
    g.add_edge("classify_case_type", "extract_case_facts")
    g.add_edge("extract_case_facts", "clarify_slots")
    # 条件边：信息不足 → 先问；信息够 → 继续检索与分析
    g.add_conditional_edges(
        "clarify_slots", route_after_clarify,
        {"ask_clarify": "ask_clarify", "search_applicable_law": "search_applicable_law"},
    )
    g.add_edge("ask_clarify", END)
    # 核验覆盖：缺关键条文 → 打回检索补检（最多 1 次）；够 → 继续分析
    g.add_conditional_edges(
        "verify_law_coverage", route_after_law_coverage,
        {"search_applicable_law": "search_applicable_law",
         "match_precedent_cases": "match_precedent_cases"},
    )
    # 复核红队：不过 → 打回起草（最多 1 次）；过 / 额度用完 → 落库
    g.add_conditional_edges(
        "review_draft", route_after_review,
        {"generate_analysis_framework": "generate_analysis_framework",
         "save_case_analysis": "save_case_analysis"},
    )
    for a, b in zip(CHAIN[3:], CHAIN[4:]):
        if a in ("verify_law_coverage", "review_draft"):
            continue          # 这两条边由上面的条件边决定（通过 / 打回）
        g.add_edge(a, b)
    g.add_edge("save_case_analysis", END)
    # Checkpointer：thread_id 隔离会话记忆；生产切 PostgresSaver（口径见 README）
    return g.compile(checkpointer=get_checkpointer())


case_agent = build_case_agent()


# ── 时效咨询 FAQ（limitation_inquiry 意图）——案件域单节点小图 ──


def answer_limitation_faq_node(state: LimitationFaqState) -> dict:
    """纯咨询无文书：calculate_limitation_period 规则直出 + 引用回条文库验真。

    时效计算是案件域的确定性能力（与主图 calculate_limitation 节点同一
    规则工具）。拆一个免落库的单节点小图承接 limitation_inquiry 意图，
    让 ComplianceAgent 回归「企业合规体检」单一职责 —— 三个 Agent
    各自只拥有自己域的能力（解耦契约见 backend/agents/capabilities.py）。
    """
    inquiry = state.get("inquiry_text", "") or state.get("case_text", "")
    # 时效语境判定（规则，沿用主图口径）：
    # 命中劳动争议语义 → 劳动仲裁时效 1 年；否则民事 3 年。
    # 关键词表必须覆盖劳动领域的常见说法 —— 实测漏掉「竞业」会把
    # 「竞业限制补偿金的时效」答成民事 3 年（应为劳动争议 1 年）。
    _LABOR_CONTEXT_KW = (
        "劳动", "仲裁", "工伤", "工资", "经济补偿", "赔偿金",
        "竞业", "试用期", "服务期", "劳动者", "用人单位", "员工",
        "加班费", "社保", "社会保险", "劳动报酬", "年休假", "辞退", "解雇",
    )
    if any(k in inquiry for k in _LABOR_CONTEXT_KW):
        ct = "labor_termination"
    else:
        ct = "contract_sales"
    dispute_kind = "wage_arrears" if ("拖欠" in inquiry and "工资" in inquiry) else "other"
    ongoing = "在职" in inquiry and "解除" not in inquiry
    lim = calculate_limitation_period(
        case_type=ct, dispute_kind=dispute_kind, relationship_ongoing=ongoing,
    )
    answer = (
        f"【时效答复】{lim['period_text']}\n"
        f"依据：{lim['legal_basis']}\n"
        f"要点：{lim['notes']}"
    )
    # 引用回条文库验真（公共核验设施，与 ComplianceAgent 同一套 retrieval.statutes）
    verified: list[dict] = []
    invalid: list[dict] = []
    for law, no in extract_citations(lim["legal_basis"]):
        art = get_article(law, no) if citation_exists(law, no) else None
        if art is None:
            invalid.append({"law": law, "article_no": no, "cite": f"{law}{no}",
                            "quote": "", "valid": False, "from": "limitation",
                            "message": "条文库中查无此条（疑似幻觉引用）"})
        else:
            verified.append({"law": art.law, "article_no": art.article_no, "cite": art.cite,
                             "quote": art.text, "valid": True, "from": "limitation", "message": ""})
    total = len(verified) + len(invalid)
    report = {
        "kind": "limitation_inquiry",
        "question": inquiry,
        "answer": answer,
        "limitation": lim,
        "citations": [c["cite"] for c in verified],
    }
    return {
        "report": report,
        "citation_check": {
            "checked": total,
            "valid_count": len(verified),
            "invalid_count": len(invalid),
            "accuracy": round(len(verified) / total, 3) if total else 1.0,
            "invalid": invalid,
            "verified": verified,
        },
        "summary": answer,
    }


def build_limitation_faq_agent():
    g = StateGraph(LimitationFaqState)
    g.add_node("answer_limitation_faq", answer_limitation_faq_node)
    g.add_edge(START, "answer_limitation_faq")
    g.add_edge("answer_limitation_faq", END)
    # 纯咨询不落库、不挂 HITL；Checkpointer 仅用于 thread_id 会话一致性
    return g.compile(checkpointer=get_checkpointer())


limitation_faq_agent = build_limitation_faq_agent()
