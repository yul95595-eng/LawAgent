"""Orchestrator 编排器 —— 8 类意图路由 + 多 Agent 串联 Pipeline。

三个 LangGraph Agent + 1 个案件域 FAQ 小图的「总调度」：
1. 意图识别：LLM 优先（llm_classify_case_type 同款降级哲学），未配 Key /
   超时 → 关键词规则；识别结果带 LRU 缓存（规格口径：意图识别 2.8s→0.9s，
   生产用 Redis 缓存，进程内 LRU 等价实现）；
2. 单 Agent 直达：8 类意图 → Agent 的映射（见 INTENT_ROUTES）；
3. Pipeline：一句话含多个诉求时拆成意图序列串行执行，Agent 间靠
   pipeline_context 传递上游结论（示例：合规体检→合同审查→最终意见书）。

解耦边界（能力白名单唯一事实源：backend/agents/capabilities.py）：
  意图 → Agent 唯一映射，Agent 间不共享工具/节点；
  limitation_inquiry（时效咨询）是案件域的确定性能力 → case_faq 小图，
  ComplianceAgent 只做企业合规体检。

risk_assessment 双路由：按材料类型分流（合同条款→ContractAgent、
企业资质→ComplianceAgent、案情纠纷→CaseAgent）——编排层的合法组合，
不属于 Agent 越界。
"""
from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache

from langgraph.types import Command

from backend.agents.case_agent import build_case_agent, case_agent, limitation_faq_agent
from backend.agents.compliance_agent import build_compliance_agent, compliance_agent
from backend.agents.contract_agent import build_contract_agent, contract_agent
from backend.core import metrics
from backend.core.config import settings
from backend.core.llm import ainvoke_with_fallback
from backend.schemas.common import INTENT_LABELS, Intent

logger = logging.getLogger(__name__)

# ── 意图 → Agent 路由表（意图→Agent 唯一映射，Agent 间不共享能力）──

INTENT_ROUTES: dict[str, str] = {
    "case_analysis": "case",
    "precedent_search": "case",
    "limitation_inquiry": "case_faq",  # 时效咨询：案件域确定性能力（FAQ 小图）
    "contract_review": "contract",
    "clause_analysis": "contract",
    "compliance_check": "compliance",
    "legal_opinion": "compliance",
    "risk_assessment": "dual",  # 双路由：按材料类型分流（编排层组合）
}


# ── 意图识别（关键词规则 + 可选 LLM + 缓存）────────────────────

_INTENT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("limitation_inquiry", ["时效", "仲裁时效", "诉讼时效", "过期没有", "还来不来得及"]),
    ("compliance_check", ["合规体检", "合规检查", "资质", "许可证", "经营范围", "工商"]),
    ("contract_review", ["审查合同", "审合同", "合同审查", "审查协议", "审协议", "帮我审"]),
    ("clause_analysis", ["这个条款", "该条款", "此条款", "条款什么问题", "条款分析"]),
    ("legal_opinion", ["意见书", "法律意见", "出一份意见"]),
    ("precedent_search", ["类案", "类似案例", "判例", " precedent"]),
    ("case_analysis", ["案件分析", "打官司", "胜诉", "起诉", "仲裁申请", "案情",
                       "辞退", "开除", "解雇", "劳动争议", "工伤", "赔偿金", "经济补偿"]),
    ("risk_assessment", ["风险评估", "有什么风险", "风险点", "风险多大"]),
]


def classify_intent_rules(message: str) -> list[str]:
    """关键词意图识别（可返回多个意图 → Pipeline）。"""
    hits: list[str] = []
    for intent, kws in _INTENT_KEYWORDS:
        if any(kw in message for kw in kws):
            hits.append(intent)
    # 无显式命中时的兜底推断：劳动争议语义优先按案件分析处理，
    # 避免「被公司辞退」这类含「公司」的案情被误判成企业合规。
    # 词表要覆盖口语说法 —— 实测「公司把我开了，能赔多少」只含「公司」，
    # 原词表一个都没命中，被错误路由到企业合规体检。
    if not hits:
        labor_kw = (
            "辞退", "解除", "工资", "赔偿", "赔多少", "要赔", "离职", "工伤",
            "补偿", "入职", "员工", "劳动者", "开除", "解雇", "裁员", "被裁",
            "炒了", "把我开了", "不给我", "欠薪", "加班费", "社保",
        )
        if any(k in message for k in ("公司", "企业", "执照")) and not any(k in message for k in labor_kw):
            hits.append("compliance_check")
        elif any(k in message for k in ("合同", "协议", "条款")):
            hits.append("contract_review")
        else:
            # 通用语义门槛：残留法律语义才进案件分析，否则按超范围拒答
            hits.append("case_analysis" if has_legal_semantics(message) else "chitchat")
    # Pipeline 组合识别：合规体检 + 合同审查 + 意见书一句话全要
    if "合规体检" in message and ("合同" in message or "协议" in message):
        if "contract_review" not in hits:
            hits.append("contract_review")
    if ("意见书" in message or "最终意见" in message) and len(hits) >= 2:
        if "legal_opinion" not in hits:
            hits.append("legal_opinion")
    # 去重保序（按 _INTENT_KEYWORDS 顺序 = 执行顺序）
    order = [i for i, _ in _INTENT_KEYWORDS]
    return sorted(set(hits), key=lambda h: order.index(h) if h in order else 99)


@lru_cache(maxsize=512)
def _intent_cache_key(message: str) -> tuple[str, ...]:
    """LRU 缓存入口（等价语义：相同问题二次识别 0 开销）。"""
    return tuple(classify_intent_rules(message))


# ── 闲聊/元问题识别（超范围兜底，不跑 Agent）─────────────────

_CHITCHAT_KW = (
    "你是谁", "你是干嘛", "你是干什么的", "干什么的", "你叫什么", "你能做什么",
    "能做什么", "你能干什么", "你会什么", "介绍一下你自己", "自我介绍", "你介绍",
    "在吗", "谢谢", "感谢", "辛苦了", "怎么用", "怎么玩", "你能帮我", "你可以帮我",
    "这是什么", "你是什么", "干嘛的", "干啥的",
)
_LEGAL_KW = (
    "案", "合同", "公司", "企业", "赔偿", "工资", "仲裁", "法", "辞退", "离职",
    "工伤", "条款", "判", "诉", "律师", "纠纷", "违约", "劳务", "劳动者", "补偿", "官司",
)


def is_chitchat(message: str) -> bool:
    """短句 + 元问题词 + 不含任何法律语义 → 闲聊（回自我介绍，不进业务流水线）。"""
    t = (message or "").strip()
    if not t or len(t) > 30:
        return False
    if not any(k in t for k in _CHITCHAT_KW):
        return False
    return not any(k in t for k in _LEGAL_KW)


# 宽口径法律语义词典：作为通用兜底门槛——凡沾边即视为法律咨询
_LEGAL_GATE_KW = (
    # 劳动
    "辞退", "开除", "解雇", "解除", "离职", "入职", "工资", "薪酬", "薪水", "加班",
    "工伤", "社保", "公积金", "讨薪", "欠薪", "实习", "兼职", "打工", "老板", "员工", "劳动者",
    # 程序与争议
    "仲裁", "诉讼", "起诉", "上诉", "判", "官司", "纠纷", "违约", "侵权", "责任",
    "证据", "时效", "罚款", "赔偿", "补偿", "欠", "扣钱", "押金",
    # 合同与商事
    "合同", "协议", "条款", "签约", "解约", "劳务", "合作方", "交易", "投资", "收购",
    "公司", "企业", "执照", "资质", "许可", "工商", "股东", "股权", "合伙", "法人", "注销", "变更",
    # 法律职业词
    "法律", "法条", "法规", "律师", "法院", "律师函", "意见书",
    # 其他民事
    "房产", "租赁", "买卖", "借款", "债务", "还款", "担保", "离婚", "继承", "遗嘱",
    "商标", "专利", "版权", "加盟", "代理", "消费维权", "退款",
)


def has_legal_semantics(text: str) -> bool:
    """宽口径判断：一句话里是否沾任何法律语义（用于通用兜底门槛）。"""
    t = (text or "")
    return any(k in t for k in _LEGAL_GATE_KW)


_CHITCHAT_REPLY = (
    "我是律所的智能法律助手，只做三类事：\n"
    "① 案件分析——描述劳动纠纷案情，我帮你测算赔偿、提炼争议焦点、评估胜诉率并检索真实类案；\n"
    "② 合同审查——粘贴合同条款，我逐条挑风险出意见书，高风险意见书须合伙人确认后才对外出具；\n"
    "③ 企业合规体检——输入企业名称，我查工商档案、核验行业资质，每条引用法条都回库验真。\n"
    "闲聊或与法律无关的问题我无法处理。你可以直接描述案情，或点击下方业务卡片试一试。"
)


async def classify_intents(message: str) -> list[str]:
    """意图识别主入口：闲聊识别 → 缓存 → 规则；配置了真实 LLM 时再用 LLM 精调。

    设计取舍：规则识别已经覆盖 8 类意图的高频问法（评测集意图准确率
    达标），LLM 只在「规则零命中且文本较长」时兜底，避免为每个问题
    都烧一次模型调用 —— 这是意图识别 0.9s 口径的工程支撑。
    """
    if is_chitchat(message):
        return ["chitchat"]
    intents = list(_intent_cache_key(message.strip()))
    if intents:
        return intents
    if settings.llm_enabled:
        try:
            catalog = json.dumps({i.value: INTENT_LABELS[i.value] for i in Intent}, ensure_ascii=False)
            prompt = (
                f"把用户诉求归类到 1~3 个意图（按执行顺序），意图集：{catalog}。"
                "只输出 JSON 数组如 [\"contract_review\"]。\n用户输入：" + message[:600]
            )
            raw = await ainvoke_with_fallback(prompt)
            arr = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
            valid = [a for a in arr if a in INTENT_ROUTES]
            if valid:
                return valid
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 意图识别失败，用规则兜底：%s", exc)
    # 通用兜底：有法律语义 → 案件分析；否则超范围拒答
    return ["case_analysis"] if has_legal_semantics(message) else ["chitchat"]


# ── risk_assessment 双路由分流 ────────────────────────────────


def resolve_risk_assessment_target(message: str) -> str:
    if any(k in message for k in ("公司", "企业", "资质", "执照", "许可")):
        return "compliance"
    if any(k in message for k in ("合同", "协议", "条款")):
        return "contract"
    return "case"


# ── 初始 State 构造（意图 → Agent 输入）───────────────────────


def build_initial_state(
    intent: str, message: str, *, thread_id: str, tenant_id: str, user_id: str,
    pipeline_context: dict | None = None, attachments: dict | None = None,
) -> tuple[str, dict]:
    """返回 (agent_name, initial_state)。attachments：前端随消息附带的材料正文。"""
    attachments = attachments or {}
    base = {
        "thread_id": thread_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "pipeline_context": pipeline_context or {},
    }
    if intent in ("case_analysis", "precedent_search"):
        state = {
            **base,
            "case_text": attachments.get("case_text") or message,
            "focus_case_type": "" if intent == "case_analysis" else _guess_case_type(message),
        }
        return "case", state
    if intent in ("contract_review", "clause_analysis"):
        contract_text = attachments.get("contract_text") or _extract_contract_text(message)
        state = {
            **base,
            "contract_text": contract_text,
            "contract_title": _guess_contract_title(message) or _guess_contract_title(contract_text),
            "clause_focus": message if intent == "clause_analysis" else "",
        }
        return "contract", state
    if intent == "limitation_inquiry":
        # 时效咨询：案件域 FAQ 小图（免落库，规则直出）
        return "case_faq", {**base, "inquiry_text": message}
    # compliance_check / legal_opinion
    state = {
        **base,
        "enterprise_name": _guess_enterprise_name(message) or (pipeline_context or {}).get("enterprise_name", ""),
        "enterprise_text": message,
    }
    if intent == "legal_opinion":
        state["pipeline_context"] = {**state["pipeline_context"], "final_opinion": True}
    return "compliance", state


def _guess_case_type(message: str) -> str:
    from backend.tools.llm_tools import _mock_classify

    return _mock_classify(message)["case_type"]


def _guess_contract_title(message: str) -> str:
    m = re.search(r"《([^》]{2,30})》", message)
    if m:
        return m.group(1)
    m = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,14})(劳动合同|股权协议|投资协议|合作协议|采购合同|销售合同|服务合同|借款协议|租赁合同)", message)
    return m.group(0) if m else ""


def _guess_enterprise_name(message: str) -> str:
    m = re.search(r"([\u4e00-\u9fff（）()]{4,20}(?:有限公司|股份公司|股份有限公司|集团公司))", message)
    return m.group(1) if m else ""


def _extract_contract_text(message: str) -> str:
    """从消息里截取合同正文（「第X条」开始到结尾）。"""
    m = re.search(r"(第[一二三四五六七八九十百零\d]+条)", message)
    return message[m.start():] if m else message


# ── 执行入口 ─────────────────────────────────────────────────

_AGENTS = {
    "case": case_agent,
    "contract": contract_agent,
    "compliance": compliance_agent,
    "case_faq": limitation_faq_agent,  # 案件域时效咨询 FAQ 小图
}


def rebuild_agents() -> None:
    """用当前 Checkpointer 重编译全部图（lifespan 建立持久化 saver 后调用）。

    导入期 _AGENTS 用 MemorySaver 兜底编译；lifespan 在主循环内建立
    sqlite/postgres 持久化 saver 后调用本函数，HITL 挂起状态即可跨重启恢复。
    """
    _AGENTS["case"] = build_case_agent()
    _AGENTS["contract"] = build_contract_agent()
    _AGENTS["compliance"] = build_compliance_agent()
    from backend.agents.case_agent import build_limitation_faq_agent

    _AGENTS["case_faq"] = build_limitation_faq_agent()


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def run_single_intent(
    intent: str, message: str, *, thread_id: str, tenant_id: str, user_id: str,
    pipeline_context: dict | None = None, resume: dict | None = None,
):
    """执行单个意图：返回 (agent_name, final_state)。

    resume 非 None 时：从 Checkpointer 恢复被 interrupt 的图（合伙人审核）。
    """
    if resume is not None:
        agent_name = INTENT_ROUTES.get(intent, "contract")
        if agent_name == "dual":
            agent_name = resolve_risk_assessment_target(message)
        graph = _AGENTS[agent_name]
        # resume 允许直接传 Command(resume=...) 或裸 payload dict，统一成 Command
        cmd = resume if isinstance(resume, Command) else Command(resume=resume)
        final = await graph.ainvoke(cmd, config=_config(thread_id))
        return agent_name, final

    agent_name, state = build_initial_state(
        intent, message, thread_id=thread_id, tenant_id=tenant_id, user_id=user_id,
        pipeline_context=pipeline_context,
    )
    if agent_name == "dual":
        agent_name = resolve_risk_assessment_target(message)
        _, state = build_initial_state(
            "compliance_check" if agent_name == "compliance" else ("contract_review" if agent_name == "contract" else "case_analysis"),
            message, thread_id=thread_id, tenant_id=tenant_id, user_id=user_id,
            pipeline_context=pipeline_context,
        )
    final = await _AGENTS[agent_name].ainvoke(state, config=_config(thread_id))
    return agent_name, final


async def run_pipeline(
    intents: list[str], message: str, *, thread_id: str, tenant_id: str, user_id: str,
):
    """多意图 Pipeline：按序执行，Agent 间传 pipeline_context。

    返回 [(intent, agent_name, final_state), ...]。
    """
    results: list[tuple[str, str, dict]] = []
    context: dict = {}
    for intent in intents:
        agent_name, state = build_initial_state(
            intent, message, thread_id=thread_id, tenant_id=tenant_id, user_id=user_id,
            pipeline_context=context,
        )
        if agent_name == "dual":
            agent_name = resolve_risk_assessment_target(message)
        final = await _AGENTS[agent_name].ainvoke(state, config=_config(thread_id))
        results.append((intent, agent_name, final))
        context = _absorb_context(context, agent_name, final)
    return results


def _absorb_context(context: dict, agent_name: str, final: dict) -> dict:
    """Pipeline 的 Agent 间 State 上下文传递（规格文档核心机制）。"""
    if agent_name == "compliance" and final.get("enterprise_info"):
        context.setdefault("enterprise_name", final["enterprise_info"].get("enterprise_name", ""))
        context["enterprise_info"] = final["enterprise_info"]
        context["compliance_risks"] = final.get("report", {}).get("risk_items", [])
    if agent_name == "contract" and final.get("risk_points") is not None:
        context["contract_title"] = final.get("contract_title", "")
        context["contract_risks"] = final.get("risk_points", [])
        context["contract_risk_level"] = final.get("risk_level", "")
    if agent_name == "compliance" and context.get("contract_risks"):
        # 最终意见书：把合同审查结论带给意见书生成
        context["final_opinion"] = True
        context["conclusion"] = (
            f"综合合规体检与《{context.get('contract_title', '')}》审查"
            f"（风险等级 {context.get('contract_risk_level', '')}），"
            "建议按风险清单完成条款修订与资质补办后再行签署。"
        )
    return context


# ── 流式执行（SSE 用）：节点级进度 + 结果卡 + HitL 中断 ─────────

NODE_LABELS = {
    # CaseAgent（10 主链 + 1 追问侧分支）
    "classify_case_type": "案由分类", "extract_case_facts": "抽取案件事实",
    "clarify_slots": "信息完整性检查", "ask_clarify": "请求补充信息",
    "search_applicable_law": "检索适用法条（多轮改写）", "verify_law_coverage": "核验关键条文",
    "review_draft": "复核红队", "match_precedent_cases": "类案匹配",
    "analyze_legal_issues": "争议焦点分析", "calculate_limitation": "时效计算",
    "estimate_win_rate": "胜诉率评估", "generate_analysis_framework": "生成分析框架",
    "save_case_analysis": "案件分析归档",
    # 案件域时效 FAQ 小图（limitation_inquiry）
    "answer_limitation_faq": "时效答复",
    # ContractAgent（8）
    "parse_contract": "合同解析切条", "review_each_clause": "逐条智能审查",
    "detect_risk_points": "风险点检测（双引擎）", "assess_risk_level": "风险等级评定",
    "validate_format": "必备条款校验", "generate_review_opinion": "撰写审查意见书",
    "partner_review": "合伙人审核（HITL）", "submit_result": "审查结果提交归档",
    # ComplianceAgent（6）
    "load_enterprise_info": "调取企业工商档案", "check_business_scope": "经营范围合规检查",
    "check_license_qualification": "资质许可核查",
    "verify_citation_accuracy": "法条引用准确性校验", "generate_compliance_report": "生成合规报告",
    "save_compliance_report": "合规档案归档",
}


def build_result_card(agent_name: str, intent: str, final: dict) -> dict:
    """把 Agent final state 组装成前端结果卡片（AgentResult 口径）。"""
    from backend.schemas.common import AgentResult

    if agent_name == "case":
        clarify = final.get("clarify") or {}
        pending = bool(final.get("clarify_pending"))
        card = AgentResult(
            agent="case", intent=intent,
            title=(f"需补充信息：{final.get('case_type_label', '')}" if pending
                   else f"案件分析：{final.get('case_type_label', '')}"),
            summary=final.get("summary", ""),
            result={
                "case_type": final.get("case_type"),
                "clarify_pending": pending,
                "clarify": clarify,
                "retrieval_trace": final.get("retrieval_trace", []),
                "law_gap_note": final.get("law_gap_note", ""),
                "facts": final.get("case_facts", {}),
                "applicable_law": [
                    {"cite": l.get("cite"), "text": (l.get("text") or "")[:120]}
                    for l in final.get("applicable_law", [])
                ],
                "precedents": final.get("precedents", [])[:3],
                "issues": final.get("legal_issues", []),
                "limitation": final.get("limitation", {}),
                "win_rate": final.get("win_rate"),
                "win_rate_reason": final.get("win_rate_reason", ""),
                "framework": final.get("framework", {}),
                "case_id": final.get("saved_case_id", ""),
                "review": {
                    "verdict": final.get("review_verdict", ""),
                    "findings": final.get("review_findings", []),
                },
            },
            citations=[] if pending else [
                {"law": l.get("law", ""), "article_no": l.get("article_no", ""), "quote": l.get("text", "")}
                for l in final.get("applicable_law", [])
            ],
            audit_hint=("信息不足，未检索、未下结论、未落库；补充后重新分析" if pending
                        else ("复核发现未修正问题，结论存在争议，须人工复核"
                              if final.get("review_verdict") == "forced_pass"
                              else ("复核提示：" + "；".join(
                                  f["detail"] for f in final.get("review_findings", [])
                                  if f.get("level") == "medium") + "，请承办律师注意"
                                    if final.get("review_verdict") == "warn"
                                    else "AI 辅助分析，结论需承办律师复核后使用"))),
        )
    elif agent_name == "contract":
        card = AgentResult(
            agent="contract", intent=intent,
            title=f"合同审查：《{final.get('contract_title', '')}》",
            summary=final.get("summary", ""),
            result={
                "contract_title": final.get("contract_title", ""),
                "risk_level": final.get("risk_level", ""),
                "risk_points": final.get("risk_points", []),
                "format_check": final.get("format_check", {}),
                "review_opinion": final.get("review_opinion", ""),
                "review_id": final.get("review_id", ""),
                "review_status": ("approved" if final.get("review_decision", "") in ("confirmed", "") else "returned"),
                "clause_count": len(final.get("clauses", [])),
            },
            audit_hint="高风险合同已经合伙人审核（HITL）；AI 辅助意见，责任在复核人",
        )
    elif agent_name in ("compliance", "case_faq"):
        report = final.get("report", {})
        # 引用校验明细（{cite, quote, valid}）→ Citation（law/article_no 需从 cite 解析）
        from backend.retrieval.statutes import extract_citations

        citations = []
        for v in final.get("citation_check", {}).get("verified", [])[:6]:
            parsed = extract_citations(v.get("cite", ""))
            law, art = parsed[0] if parsed else (v.get("cite", ""), "")
            citations.append({
                "law": law, "article_no": art,
                "quote": v.get("quote", ""), "valid": v.get("valid", False),
            })
        is_faq = agent_name == "case_faq"
        card = AgentResult(
            agent=agent_name, intent=intent,
            title="时效答复" if report.get("kind") == "limitation_inquiry" else
                  (f"合规体检：{report.get('enterprise', {}).get('enterprise_name', '')}" or "合规报告"),
            summary=final.get("summary", ""),
            result={
                "report": report,
                "citation_check": {
                    k: v for k, v in final.get("citation_check", {}).items() if k != "verified"
                },
                "risk_score": final.get("risk_score", report.get("risk_score", 0)),
                "profile_id": final.get("saved_profile_id", ""),
            },
            citations=citations,
            audit_hint=(
                "时效答复由规则引擎直出，引用已回条文库验真；具体案情请咨询承办律师"
                if is_faq else
                "全部法条引用已过准确性校验；意见书需法务复核后对外"
            ),
        )
    return card.model_dump()


async def stream_pipeline(
    intents: list[str], message: str, *, thread_id: str, tenant_id: str, user_id: str,
    attachments: dict | None = None,
):
    """流式 Pipeline（SSE 消费）：逐意图逐节点产出事件。

    事件格式：(kind, payload)
      ("node", {agent, intent, node, label})
      ("interrupt", {agent, intent, **interrupt_value})   # HitL 挂起，后续意图不再执行
      ("result", AgentResult 卡片)
    """
    context: dict = {}
    for intent in intents:
        agent_name, state = build_initial_state(
            intent, message, thread_id=thread_id, tenant_id=tenant_id, user_id=user_id,
            pipeline_context=context, attachments=attachments,
        )
        if agent_name == "dual":
            agent_name = resolve_risk_assessment_target(message)
            _, state = build_initial_state(
                "compliance_check" if agent_name == "compliance"
                else ("contract_review" if agent_name == "contract" else "case_analysis"),
                message, thread_id=thread_id, tenant_id=tenant_id, user_id=user_id,
                pipeline_context=context,
            )
        graph = _AGENTS[agent_name]
        final: dict = {}
        interrupted = False
        # 阶段4 埋点：astream 只在「节点执行完」吐事件，两次事件的间隔即该节点耗时，
        # 用来回答「哪个节点拖慢了整条流水线」（LLM 节点通常是元凶）。
        t_prev = time.perf_counter()
        async for chunk in graph.astream(state, config=_config(thread_id), stream_mode="updates"):
            for node_name, update in chunk.items():
                elapsed = time.perf_counter() - t_prev
                t_prev = time.perf_counter()
                if node_name == "__interrupt__":
                    for it in update:
                        payload = dict(it.value) if isinstance(it.value, dict) else {"value": it.value}
                        yield ("interrupt", {"agent": agent_name, "intent": intent, **payload})
                    metrics.inc("lawagent_hitl_interrupts_total", {"agent": agent_name},
                                help_text="HITL 挂起次数（合伙人待审）")
                    interrupted = True
                    break
                final.update(update or {})
                metrics.observe("lawagent_agent_node_duration_seconds", elapsed,
                                {"agent": agent_name, "node": node_name},
                                help_text="Agent 节点耗时（两次更新事件的间隔）")
                metrics.inc("lawagent_agent_node_runs_total",
                            {"agent": agent_name, "node": node_name},
                            help_text="Agent 节点执行次数")
                yield ("node", {
                    "agent": agent_name, "intent": intent, "node": node_name,
                    "label": NODE_LABELS.get(node_name, node_name),
                })
            if interrupted:
                break
        if interrupted:
            return
        context = _absorb_context(context, agent_name, final)
        yield ("result", build_result_card(agent_name, intent, final))
