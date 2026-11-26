"""ContractAgent 合同审查 —— 8 节点 + LangGraph interrupt HitL（核心亮点）。

    parse_contract → review_each_clause → detect_risk_points
      → assess_risk_level → validate_format → generate_review_opinion
      → [risk_level == high] → partner_review（interrupt 合伙人人工审核）
            ├─ 确认 → submit_result
            └─ 退回 → generate_review_opinion（按修改意见重写，再进审核）

处理意图：contract_review / clause_analysis。

HITL 落地（规格文档口径）：
- 风险等级 high 的审查意见书必须经合伙人确认才能对外提交 —— 用
  langgraph.types.interrupt() 在 partner_review 节点挂起图执行；
- 合伙人在前端「待审意见书」列表点确认/退回 → API 用 Command(resume=...)
  恢复图执行，状态由 Checkpointer 精确还原；
- 这是「AI 辅助、责任在人」行业标准（法大大等头部厂商同口径）的工程落地。

双引擎风险（30+ 类清单）：
- 规则显式引擎（本文件 _CONTRACT_RULES，可确定性判定，置信度 1.0）
- LLM 隐性引擎（llm_review_contract_clause，语义风险）
来源分开标注 source=rule/llm，报告交叉呈现。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.agents.state import ContractState
from backend.core.security import new_id
from backend.memory.checkpointer import get_checkpointer
from backend.memory.models import ContractReview, SessionLocal
from backend.tools.api_tools import get_contract_file, submit_review_result
from backend.tools.llm_tools import llm_generate_legal_opinion, llm_review_contract_clause
from backend.tools.rule_tools import risk_level_assess, validate_contract_format

logger = logging.getLogger(__name__)


# ── 节点 1：parse_contract 合同解析（确定性切分）──────────────

_HEADING = re.compile(r"第[一二三四五六七八九十百零\d]+条")


def _split_clauses(text: str) -> list[dict]:
    """按「第X条」切条款；无条款标记时整文为一条。确定性、可复现。"""
    clauses: list[dict] = []
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [{"clause_id": "c_001", "heading": "全文", "text": text.strip()}]
    # 前置段（名称/当事人）
    preamble = text[: matches[0].start()].strip()
    if len(preamble) > 10:
        clauses.append({"clause_id": "c_000", "heading": "首部", "text": preamble[:500]})
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        clauses.append({
            "clause_id": f"c_{i + 1:03d}",
            "heading": m.group(0),
            "text": body,
        })
    return clauses


async def parse_contract_node(state: ContractState) -> dict:
    text, title = state["contract_text"], state.get("contract_title", "")
    # 支持从合同管理系统按文件号调档
    if state.get("contract_id") and not text.strip():
        try:
            f = await get_contract_file(state["contract_id"])
            text = f.get("text", text)
            title = title or f.get("title", "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_contract_file 降级直读文本：%s", exc)
    focus = state.get("clause_focus", "")
    clauses = _split_clauses(text)
    # 标题缺失或是文件名（上传文件场景）时，取合同正文首个非空行作标题
    if not title or re.search(r"\.(docx?|pdf|txt|md)\s*$", title):
        for line in text.splitlines():
            line = line.strip()
            if line:
                title = line[:40]
                break
    if focus:
        clauses = [c for c in clauses if focus[:12] in c["text"] or focus[:12] in c["heading"]] or clauses
    return {
        "clauses": clauses,
        "contract_title": title or "未命名合同",
        # review_id 在入口生成：interrupt 挂起前 API 就能凭它登记「待审」记录
        "review_id": state.get("review_id") or new_id("review"),
        "summary": f"已解析《{title or '未命名合同'}》，切分出 {len(clauses)} 个条款",
    }


# ── 节点 2：review_each_clause 逐条审查（LLM 隐性引擎）────────


async def review_each_clause_node(state: ContractState) -> dict:
    risks: list[dict] = []
    for c in state["clauses"]:
        if not c["text"] or len(c["text"]) < 6:
            continue
        r = await llm_review_contract_clause(c["text"], c["clause_id"])
        if r:
            risks.append(r)
    return {"clause_risks": risks}


# ── 节点 3：detect_risk_points 规则显式引擎 + 合并 ────────────

# 30+ 类合同风险点清单（规则显式 ∪ LLM 隐性 ∪ 格式缺项，README 附全表）
# 这里是「可确定性判定」的规则函数子集：100% 命中率、零幻觉、可复现。


def _rule_trial_over_limit(text: str) -> list[dict]:
    m1 = re.search(r"试用期[为共约]?[^\d]{0,6}(\d{1,2})\s*个?\s*月", text)
    if not m1:
        return []
    trial = int(m1.group(1))
    if "无固定期限" in text:
        cap, desc = 6, "无固定期限"
    else:
        m2 = re.search(r"合同期限[为约]?[^\d]{0,6}(\d{1,2})\s*年", text) or re.search(r"(\d{1,2})\s*年期[限的]", text)
        if not m2:
            return []
        years = int(m2.group(1))
        if years >= 3:
            cap, desc = 6, f"{years}年"
        elif years >= 1:
            cap, desc = 2, f"{years}年"
        else:
            cap, desc = 1, f"{years}年（不满1年）"
        if years < 1 and re.search(r"(不满三个月|3个月以下|期限为\d{1,2}个?月)", text):
            cap, desc = 0, "不满3个月"
    if trial > cap and cap > 0:
        return [{
            "risk_type": "trial_over_limit", "risk_name": "试用期超法定上限", "level": "high",
            "reason": f"约定试用期 {trial} 个月，超过合同期限（{desc}）对应的法定上限 {cap} 个月",
            "suggestion": f"试用期调整至不超过 {cap} 个月",
            "basis": "《劳动合同法》第十九条", "source": "rule", "confidence": 1.0,
            "clause_text": m1.group(0),
        }]
    return []


def _rule_non_compete_over_2y(text: str) -> list[dict]:
    m = re.search(r"竞业(?:限制|禁止)[^。；;\n]{0,20}(\d{1,2})\s*个?\s*月", text) \
        or re.search(r"竞业(?:限制|禁止)[^。；;\n]{0,20}(\d{1,2})\s*年", text)
    if not m:
        return []
    months = int(m.group(1)) * (12 if "年" in m.group(0)[-3:] else 1)
    if months > 24:
        return [{
            "risk_type": "non_compete_over_2y", "risk_name": "竞业限制期限超 2 年", "level": "high",
            "reason": f"竞业限制期限 {months} 个月，超过法定上限 24 个月，超出部分无效",
            "suggestion": "竞业限制期限调整为不超过 2 年",
            "basis": "《劳动合同法》第二十四条", "source": "rule", "confidence": 1.0,
            "clause_text": m.group(0),
        }]
    return []


def _rule_service_penalty_excess(text: str) -> list[dict]:
    """服务期违约金超培训费用：违约金有数额 + 未提培训费 → 命中。"""
    m = re.search(r"违约金[为约人民币\s]*([\d,，\.]+)\s*(万)?元", text)
    if not m or "服务期" not in text:
        return []
    if "培训费" in text or "培训费用" in text:
        return []  # 有培训费约定，需数值比对（留给 LLM 语义判断）
    amount = float(m.group(1).replace(",", "").replace("，", ""))
    if m.group(2):
        amount *= 10000
    return [{
        "risk_type": "penalty_illegal", "risk_name": "服务期违约金未以培训费用为限", "level": "high",
        "reason": f"约定违约金 {amount:,.0f} 元，但未约定培训费用；违约金超过用人单位提供的培训费用部分无效",
        "suggestion": "违约金不得超过实际培训费用，并按服务期未履行部分分摊",
        "basis": "《劳动合同法》第二十二条", "source": "rule", "confidence": 1.0,
        "clause_text": text[max(0, m.start() - 60):m.end() + 30],
    }]


def _rule_private_account(text: str) -> list[dict]:
    m = re.search(r"(私人账户|个人账户|私户|个人银行卡|法定代表人个人账户)", text)
    if m and any(k in text for k in ("收款", "支付", "转账", "结算")):
        return [{
            "risk_type": "private_account_payment", "risk_name": "私户收付款", "level": "high",
            "reason": f"约定通过「{m.group(1)}」收付款项，存在账外经营与偷逃税风险",
            "suggestion": "改为对公账户收付，并开具合规发票",
            "basis": "《税收征收管理法》第六十三条", "source": "rule", "confidence": 1.0,
            "clause_text": m.group(0),
        }]
    return []


def _rule_no_invoice(text: str) -> list[dict]:
    for kw in ("不开票", "不开发票", "无需开具发票", "以收据代替发票"):
        if kw in text:
            return [{
                "risk_type": "invoice_obligation_unclear", "risk_name": "约定不开具发票", "level": "high",
                "reason": f"约定「{kw}」，违反发票管理办法，可能被认定为协助偷逃税",
                "suggestion": "删除不开票约定，明确开票义务与发票类型",
                "basis": "《发票管理办法》第十九条", "source": "rule", "confidence": 1.0,
                "clause_text": kw,
            }]
    return []


_CONTRACT_RULES = (
    _rule_trial_over_limit,
    _rule_non_compete_over_2y,
    _rule_service_penalty_excess,
    _rule_private_account,
    _rule_no_invoice,
)


async def detect_risk_points_node(state: ContractState) -> dict:
    """规则显式引擎逐条跑 + 与 LLM 隐性风险合并（同条款同类型去重）。"""
    full_text = state["contract_text"]
    rule_risks: list[dict] = []
    for fn in _CONTRACT_RULES:
        rule_risks.extend(fn(full_text))
    for r in rule_risks:
        r.setdefault("clause_id", "")
        r.setdefault("risk_id", "")

    merged: list[dict] = list(state.get("clause_risks", []))
    seen = {(r.get("clause_id"), r.get("risk_type")) for r in merged}
    for rr in rule_risks:
        key = (rr.get("clause_id"), rr.get("risk_type"))
        if key in seen:
            # 规则与 LLM 撞车：规则优先（置信度 1.0），LLM 来源记入交叉验证
            for m in merged:
                if (m.get("clause_id"), m.get("risk_type")) == key:
                    m["corroborated_by_rule"] = True
                    m["confidence"] = 1.0
            continue
        merged.append(rr)
    for i, r in enumerate(merged):
        r["risk_id"] = f"risk_{i + 1:02d}"
    return {"rule_risks": rule_risks, "risk_points": merged}


# ── 节点 4：assess_risk_level 风险定级（评分公式）──────────────


def assess_risk_level_node(state: ContractState) -> dict:
    assessment = risk_level_assess(state.get("risk_points", []))
    return {
        "risk_level": assessment["risk_level"],
        "summary": (
            f"风险评估：{assessment['risk_level']}（评分 {assessment['score']}，"
            f"高 {assessment['counts']['high']} / 中 {assessment['counts']['medium']} / "
            f"低 {assessment['counts']['low']}）"
            + (f"；一票否决项：{assessment['veto_hit']}" if assessment["veto_hit"] else "")
        ),
    }


# ── 节点 5：validate_format 8 大必备条款校验（规则工具）────────


def validate_format_node(state: ContractState) -> dict:
    fmt = validate_contract_format(state["contract_text"], contract_kind="labor")
    # 缺失必备条款作为风险点补入（低置信度，供合伙人参考）
    risks = state.get("risk_points", [])
    for name in fmt.get("missing", []):
        risks.append({
            "risk_id": f"risk_fmt_{len(risks) + 1:02d}",
            "clause_id": "",
            "clause_text": "",
            "risk_type": "format_missing",
            "risk_name": f"缺少必备条款：{name}",
            "level": "medium",
            "reason": f"合同缺少劳动合同法第十七条规定的必备条款「{name}」",
            "suggestion": f"补充「{name}」条款",
            "basis": fmt["legal_basis"],
            "source": "rule",
            "confidence": 0.9,
        })
    return {"format_check": fmt, "risk_points": risks}


# ── 节点 6：generate_review_opinion 审查意见书（LLM 工具）──────


async def generate_review_opinion_node(state: ContractState) -> dict:
    risks = state.get("risk_points", [])
    revision = state.get("revision_notes", "")
    context = {
        "title": f"《{state.get('contract_title', '')}》合同审查意见书",
        "contract_title": state.get("contract_title", ""),
        "risks": risks,
        "revision_notes": revision,
        "conclusion": (
            f"整体风险等级 {state.get('risk_level', '-')}。"
            + (f"合伙人退回修改要求：{revision}。本次已按要求修订意见。" if revision else "")
        ),
    }
    opinion = await llm_generate_legal_opinion(context)
    return {"review_opinion": opinion, "revision_notes": revision}


# ── 路由：意见书完成后是否进合伙人审核 ────────────────────────


def route_after_opinion(state: ContractState) -> str:
    """风险 high 必须合伙人审核（HITL 硬门禁）；clause_analysis 单条款轻量直出。"""
    if state.get("risk_level") == "high":
        return "partner_review"
    return "submit_result"


# ── 节点 7：partner_review 合伙人审核（LangGraph interrupt）────


async def partner_review_node(state: ContractState) -> dict:
    """高风险审查意见挂起，等待合伙人 Command(resume=...) 恢复。

    interrupt 载荷即前端「待审意见书」卡片数据；恢复值为
    {"decision": "confirmed"|"returned", "reviewer_id": str, "comment": str}。
    """
    decision = interrupt({
        "prompt": "高风险合同审查意见需要合伙人审核（AI 辅助、责任在人）",
        "review_id": state.get("review_id", ""),
        "thread_id": state.get("thread_id", ""),
        "contract_title": state.get("contract_title", ""),
        "risk_level": state.get("risk_level"),
        "risk_count": len(state.get("risk_points", [])),
        "risk_points": state.get("risk_points", []),
        "format_check": state.get("format_check", {}),
        "review_opinion": state.get("review_opinion", ""),
    })
    return {
        "review_decision": decision.get("decision", "confirmed"),
        "reviewer_id": decision.get("reviewer_id", ""),
        "review_comment": decision.get("comment", ""),
        "revision_notes": decision.get("comment", "") if decision.get("decision") == "returned" else "",
    }


def route_after_partner(state: ContractState) -> str:
    return "generate_review_opinion" if state.get("review_decision") == "returned" else "submit_result"


# ── 节点 8：submit_result 落库 + 回写甲方系统 ─────────────────


async def submit_result_node(state: ContractState) -> dict:
    review_id = state.get("review_id") or new_id("review")
    decision = state.get("review_decision")
    if decision == "confirmed":
        status = "approved"
    elif decision == "returned":
        status = "returned"
    else:
        # 无 interrupt（中低风险直通）：直接定稿
        status = "approved"
    now = datetime.now().isoformat(timespec="seconds")
    async with SessionLocal() as session:
        existing = await session.get(ContractReview, review_id)
        if existing:
            existing.risks_json = json.dumps(state.get("risk_points", []), ensure_ascii=False)
            existing.risk_level = state.get("risk_level", "low")
            existing.review_opinion = state.get("review_opinion", "")
            existing.status = status
            existing.reviewer_id = state.get("reviewer_id") or existing.reviewer_id
            existing.review_comment = state.get("review_comment") or existing.review_comment
            existing.reviewed_at = now if decision else existing.reviewed_at
        else:
            session.add(ContractReview(
                review_id=review_id,
                thread_id=state.get("thread_id", ""),
                tenant_id=state.get("tenant_id", "tenant_firm"),
                user_id=state.get("user_id", ""),
                contract_title=state.get("contract_title", ""),
                contract_text=state.get("contract_text", "")[:20000],
                clauses_json=json.dumps(state.get("clauses", []), ensure_ascii=False),
                risks_json=json.dumps(state.get("risk_points", []), ensure_ascii=False),
                risk_level=state.get("risk_level", "low"),
                review_opinion=state.get("review_opinion", ""),
                format_check_json=json.dumps(state.get("format_check", {}), ensure_ascii=False),
                status=status,
                reviewer_id=state.get("reviewer_id") or None,
                review_comment=state.get("review_comment") or None,
                reviewed_at=now if decision else None,
            ))
        await session.commit()
    # 回写甲方合同管理系统（Mock；失败不影响本地定稿）
    receipt = {}
    try:
        receipt = await submit_review_result(review_id, {
            "risk_level": state.get("risk_level"), "status": status,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("submit_review_result 失败（已本地留存）：%s", exc)
    summary = (
        f"《{state.get('contract_title', '')}》审查完成：风险等级 {state.get('risk_level', '-')}，"
        f"共 {len(state.get('risk_points', []))} 个风险点，状态 {status}。"
        + (f"甲方系统回执 {receipt.get('receipt_no', '')}。" if receipt else "")
        + (f"合伙人（{state.get('reviewer_id', '')}）已{'确认' if state.get('review_decision') == 'confirmed' else '退回'}。"
           if decision else "")
    )
    return {"review_id": review_id, "summary": summary}


# ── 图装配 ───────────────────────────────────────────────────

NODES = [
    "parse_contract", "review_each_clause", "detect_risk_points", "assess_risk_level",
    "validate_format", "generate_review_opinion", "partner_review", "submit_result",
]


def build_contract_agent():
    g = StateGraph(ContractState)
    for name in NODES:
        g.add_node(name, globals()[f"{name}_node"])
    g.add_edge(START, "parse_contract")
    g.add_edge("parse_contract", "review_each_clause")
    g.add_edge("review_each_clause", "detect_risk_points")
    g.add_edge("detect_risk_points", "assess_risk_level")
    g.add_edge("assess_risk_level", "validate_format")
    g.add_edge("validate_format", "generate_review_opinion")
    # 条件边：意见书 →（high?）→ 合伙人审核 / 直出
    g.add_conditional_edges(
        "generate_review_opinion", route_after_opinion,
        {"partner_review": "partner_review", "submit_result": "submit_result"},
    )
    g.add_conditional_edges(
        "partner_review", route_after_partner,
        {"submit_result": "submit_result", "generate_review_opinion": "generate_review_opinion"},
    )
    g.add_edge("submit_result", END)
    return g.compile(checkpointer=get_checkpointer())


contract_agent = build_contract_agent()
