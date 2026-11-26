"""LLM 工具（5 个）—— 真模型优先，超时/未配 Key 降级确定性 Mock。

每个工具的结构统一：
  async llm_xxx(...) →
    1. settings.llm_enabled：调真实模型（结构化 JSON 输出）；
    2. 失败（超时/限流/解析失败）→ 记 warning，走 _mock_* 确定性实现；
    3. Mock 实现是关键词驱动的迷你规则器 —— 测试不依赖网络，演示离线可跑，
       结果可复现；返回值带 engine 字段标注 llm/mock（可观测、可评测）。

规格文档节点表里还出现过 llm_parse_contract / llm_extract_case_facts /
llm_estimate_win_rate：实现时归并 —— 三者均为确定性可判定逻辑
（切条款用正则、抽事实用正则、胜诉率用证据评分），按「能规则化的
不交给模型」原则放进了各 Agent 节点内的确定性辅助函数，不再占 LLM 工具名额。
"""
from __future__ import annotations

import json
import logging
import re

from backend.core.config import settings
from backend.core.llm import ainvoke_with_fallback

logger = logging.getLogger(__name__)


# ── 案由关键词表（Mock 分类器 + 真实分类失败时的兜底依据）────────

_CASE_TYPE_KEYWORDS: list[tuple[str, str, list[str]]] = [
    # (case_type, 案由标签, 触发关键词)
    ("labor_termination", "违法解除劳动合同纠纷", ["解除劳动合同", "辞退", "开除", "解雇", "违法解除", "2N", "赔偿金"]),
    ("labor_probation", "试用期纠纷", ["试用期", "试用", "转正"]),
    ("labor_non_compete", "竞业限制纠纷", ["竞业限制", "竞业禁止"]),
    ("labor_service_period", "服务期与违约金纠纷", ["服务期", "培训费", "违约金"]),
    ("contract_equity", "股权转让与对赌纠纷", ["股权", "对赌", "回购", "增资", "投资协议", "反稀释"]),
    ("contract_lending", "民间借贷纠纷", ["借款", "借条", "借贷", "利息", "还款"]),
    ("contract_sales", "买卖合同纠纷", ["买卖", "购销", "交货", "验收", "货款"]),
    ("ip_license", "知识产权合同纠纷", ["专利", "商标", "著作权", "软件许可", "知识产权", "技术秘密"]),
]


# ─────────────────────────────────────────────────────────────
# 工具 1：llm_classify_case_type —— 案由分类
# ─────────────────────────────────────────────────────────────


async def llm_classify_case_type(case_text: str) -> dict:
    """案由分类 → {case_type, case_type_label, confidence, reason, engine}。"""
    if settings.llm_enabled:
        try:
            prompt = (
                "你是律所的案件登记员。把下面案情归入且仅归入一个案由，"
                f"可选案由及代码：{json.dumps({ct: label for ct, label, _ in _CASE_TYPE_KEYWORDS}, ensure_ascii=False)}。"
                "只输出 JSON：{\"case_type\": \"代码\", \"confidence\": 0~1, \"reason\": \"一句话\"}。\n\n"
                f"案情：{case_text[:1200]}"
            )
            raw = await ainvoke_with_fallback(prompt)
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            if data.get("case_type"):
                return {
                    "case_type": data["case_type"],
                    "case_type_label": _label_of(data["case_type"]),
                    "confidence": float(data.get("confidence", 0.9)),
                    "reason": data.get("reason", ""),
                    "engine": "llm",
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_classify_case_type 降级 Mock：%s", exc)
    return _mock_classify(case_text)


def _label_of(case_type: str) -> str:
    for ct, label, _ in _CASE_TYPE_KEYWORDS:
        if ct == case_type:
            return label
    return case_type


def _mock_classify(case_text: str) -> dict:
    scores: dict[str, int] = {}
    # 按「命中词种数」打分（不是出现次数）：词频会误伤 —— 实测「违约金」
    # 出现两次会把「竞业限制」出现一次的案子误判成服务期纠纷。
    for ct, label, kws in _CASE_TYPE_KEYWORDS:
        scores[ct] = sum(1 for kw in kws if kw in case_text)
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return {
            "case_type": "labor_termination",
            "case_type_label": _label_of("labor_termination"),
            "confidence": 0.4,
            "reason": "未命中明确案由关键词，按劳动争议兜底（可人工改判）",
            "engine": "mock",
        }
    return {
        "case_type": best,
        "case_type_label": _label_of(best),
        "confidence": min(0.95, 0.6 + 0.1 * scores[best]),
        "reason": f"命中关键词证据 {scores[best]} 处",
        "engine": "mock",
    }


# ─────────────────────────────────────────────────────────────
# 工具 2：llm_review_contract_clause —— 条款风险审查（LLM 隐性引擎）
#
# 与规则显式引擎（validate_contract_format 等）互补：规则抓「能代码判定」
# 的硬违法，LLM 抓「要语义理解」的隐性风险（对赌、反稀释、权利不对等）。
# 双引擎来源在结果里分开标注 source=rule / llm，报告层交叉呈现。
# ─────────────────────────────────────────────────────────────

# 语义风险清单（30+ 类合同风险点清单的「LLM 隐性」子集样例）
_SEMANTIC_RISKS: list[dict] = [
    {"risk_type": "buyback_with_target_company", "risk_name": "与目标公司对赌回购条款",
     "keywords": ["对赌", "回购", "业绩承诺", "股权回购"],
     "level": "high", "confidence": 0.9,
     "reason": "投资方与目标公司对赌，请求回购需审查是否完成减资程序，否则回购请求可能不获支持",
     "suggestion": "改为与创始股东/实际控制人对赌，或明确减资程序安排与时间表",
     "basis": "《全国法院民商事审判工作会议纪要》第5条"},
    {"risk_type": "anti_dilution_unfair", "risk_name": "反稀释条款权利不对等",
     "keywords": ["反稀释", "稀释", "同等条件", "优先认购"],
     "level": "medium", "confidence": 0.85,
     "reason": "反稀释补偿仅约束公司/创始股东、未设例外情形，后续融资定价空间被过度压缩",
     "suggestion": "增加董事会批准的期权池、员工激励等例外情形，明确补偿方式（现金/股权）",
     "basis": "《公司法》第二百二十七条（新增资本优先认缴）"},
    {"risk_type": "liquidation_preference_excessive", "risk_name": "优先清算权约定过高",
     "keywords": ["优先清算", "清算优先", "剩余财产分配"],
     "level": "medium", "confidence": 0.8,
     "reason": "清算优先倍数过高时对创始股东与其他显失公平，极端条款可能被调整",
     "suggestion": "优先清算倍数控制在 1~1.5 倍投资款区间，并明确视同清算事件范围",
     "basis": "《公司法》第二百三十六条（清算剩余财产分配）"},
    {"risk_type": "non_compete_no_pay", "risk_name": "竞业限制未约定经济补偿",
     "keywords": ["竞业限制", "竞业禁止"],
     "level": "high", "confidence": 0.9,
     "reason": "约定竞业限制但未约定离职后按月支付经济补偿，劳动者可主张补偿或主张条款不受保护",
     "suggestion": "补充按月支付不低于离职前平均工资 30% 的经济补偿条款",
     "basis": "《劳动合同法》第二十三条第二款"},
    {"risk_type": "penalty_illegal", "risk_name": "服务期违约金超法定上限",
     "keywords": ["服务期", "违约金"],
     "level": "high", "confidence": 0.85,
     "reason": "劳动者违约金超出培训费用、或非服务期/竞业限制情形约定违约金，超出部分无效",
     "suggestion": "违约金不超过培训费用并按服务期未履行部分分摊；删除法定情形外违约金",
     "basis": "《劳动合同法》第二十二条、第二十五条"},
    {"risk_type": "termination_unilateral", "risk_name": "单方任意解除权条款",
     "keywords": ["单方解除", "任意解除", "随时解除", "无须理由"],
     "level": "medium", "confidence": 0.85,
     "reason": "约定一方任意解除权，超出法定解除情形，加重对方责任",
     "suggestion": "删除任意解除约定，改按法定解除情形列举并设通知程序",
     "basis": "《民法典》第五百六十三条"},
    {"risk_type": "format_clause_unfair", "risk_name": "格式条款显失公平",
     "keywords": ["最终解释权", "概不负责", "免责"],
     "level": "medium", "confidence": 0.8,
     "reason": "不合理免除己方责任、加重对方责任、限制对方主要权利的条款可能无效",
     "suggestion": "删除「最终解释权/概不负责」类表述，按公平原则重拟责任条款",
     "basis": "《民法典》第四百九十六条、第四百九十七条"},
    {"risk_type": "prepay_no_guarantee", "risk_name": "预付款比例过高且无担保",
     "keywords": ["预付", "首付", "预付款"],
     "level": "medium", "confidence": 0.75,
     "reason": "高额预付款与履约保障不对等，付款方资金风险大",
     "suggestion": "降低首付比例至 30% 以内或增加履约保函/共管账户安排",
     "basis": "《民法典》第六百二十八条"},
]


async def llm_review_contract_clause(clause_text: str, clause_id: str = "") -> dict | None:
    """审查单个条款 → 风险 dict（无风险返回 None）。"""
    if settings.llm_enabled:
        try:
            catalog = json.dumps(
                [{"risk_type": r["risk_type"], "risk_name": r["risk_name"]} for r in _SEMANTIC_RISKS],
                ensure_ascii=False,
            )
            prompt = (
                "你是合同审查律师。判断下面条款是否存在以下风险类型之一，没有风险输出 {\"risk\": null}。\n"
                f"风险类型：{catalog}\n"
                "有风险只输出 JSON：{\"risk\": {\"risk_type\": \"...\", \"level\": \"high|medium|low\", "
                "\"reason\": \"...\", \"suggestion\": \"...\", \"confidence\": 0~1}}\n\n"
                f"条款（{clause_id}）：{clause_text[:1500]}"
            )
            raw = await ainvoke_with_fallback(prompt)
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            risk = data.get("risk")
            if risk and risk.get("risk_type"):
                base = next((r for r in _SEMANTIC_RISKS if r["risk_type"] == risk["risk_type"]), {})
                return {
                    "risk_id": "",
                    "clause_id": clause_id,
                    "clause_text": clause_text[:200],
                    "risk_type": risk["risk_type"],
                    "risk_name": base.get("risk_name") or risk["risk_type"],
                    "level": risk.get("level", "medium"),
                    "reason": risk.get("reason", ""),
                    "suggestion": risk.get("suggestion", ""),
                    "basis": base.get("basis", ""),
                    "source": "llm",
                    "confidence": float(risk.get("confidence", 0.8)),
                }
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_review_contract_clause 降级 Mock：%s", exc)
    return _mock_review_clause(clause_text, clause_id)


def _mock_review_clause(clause_text: str, clause_id: str) -> dict | None:
    """确定性条款审查（Mock 引擎）。按语义条件判定，宁可不报也不误报。"""
    t = clause_text

    def _mk(spec: dict) -> dict:
        return {
            "risk_id": "",
            "clause_id": clause_id,
            "clause_text": clause_text[:200],
            "risk_type": spec["risk_type"],
            "risk_name": spec["risk_name"],
            "level": spec["level"],
            "reason": spec["reason"],
            "suggestion": spec["suggestion"],
            "basis": spec["basis"],
            "source": "llm",
            "confidence": spec["confidence"],
        }

    # 竞业限制：未约定离职后按月经济补偿才算风险
    # （「同行业/同业 + 不得」也能锁定竞业限制语义，覆盖不写「竞业」二字的条款）
    if ("竞业限制" in t or "竞业禁止" in t
            or (("同行业" in t or "同业" in t) and "不得" in t)) \
            and not re.search(r"(经济补偿|按月支付|补偿金)", t):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "non_compete_no_pay"))
    # 服务期违约金：金额超过培训费用（或未提培训费）才算风险
    if "服务期" in t and "违约金" in t and not re.search(r"(培训费用|培训费)", t):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "penalty_illegal"))
    # 劳动者违约金且无服务期/竞业场景
    if re.search(r"(劳动者|乙方|员工)[^。；;\n]{0,20}(支付|承担)[^。；;\n]{0,15}违约金", t) \
            and not re.search(r"(服务期|竞业|培训)", t):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "penalty_illegal"))
    # 对赌回购
    if any(kw in t for kw in ("对赌", "业绩承诺", "股权回购", "回购")):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "buyback_with_target_company"))
    # 反稀释
    if "反稀释" in t or ("稀释" in t and "同等条件" in t):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "anti_dilution_unfair"))
    # 优先清算
    if "优先清算" in t or ("清算" in t and "优先" in t):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "liquidation_preference_excessive"))
    # 任意解除权
    if any(kw in t for kw in ("单方解除", "任意解除", "随时解除", "无须理由解除")):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "termination_unilateral"))
    # 格式条款
    if any(kw in t for kw in ("最终解释权", "概不负责", "不承担任何责任")):
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "format_clause_unfair"))
    # 高额预付
    m = re.search(r"(\d{1,3})\s*%", t)
    if any(kw in t for kw in ("预付", "首付")) and m and int(m.group(1)) > 50:
        return _mk(next(s for s in _SEMANTIC_RISKS if s["risk_type"] == "prepay_no_guarantee"))
    return None


# ─────────────────────────────────────────────────────────────
# 工具 3：llm_analyze_legal_risk —— 案件争议焦点与法律风险分析
# ─────────────────────────────────────────────────────────────


async def llm_analyze_legal_risk(case_type: str, case_facts: dict, applicable_law: list[dict]) -> list[dict]:
    """争议焦点分析 → [{issue, analysis, basis, favor}]。"""
    if settings.llm_enabled:
        try:
            prompt = (
                "你是资深诉讼律师。基于案件事实与适用法条，输出 2~4 个争议焦点分析。"
                "只输出 JSON 数组：[{\"issue\": \"焦点\", \"analysis\": \"分析\", \"basis\": \"法条\", \"favor\": \"对我方|对方|中性\"}]\n\n"
                f"案由：{case_type}\n事实：{json.dumps(case_facts, ensure_ascii=False)}\n"
                f"法条：{json.dumps([l.get('cite', '') for l in applicable_law], ensure_ascii=False)}"
            )
            raw = await ainvoke_with_fallback(prompt)
            issues = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
            if isinstance(issues, list) and issues:
                return issues
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_analyze_legal_risk 降级 Mock：%s", exc)
    return _mock_analyze(case_type, case_facts)


def _mock_analyze(case_type: str, case_facts: dict) -> list[dict]:
    if case_type == "labor_termination":
        amount = case_facts.get("claim_amount", {})
        return [
            {
                "issue": "用人单位解除劳动合同是否具备法定情形与合法程序",
                "analysis": (
                    f"劳动者被{case_facts.get('termination_manner', '口头')}解除，"
                    "用人单位未举证存在《劳动合同法》第三十九条至第四十一条法定解除情形，"
                    "也未履行工会通知程序，构成违法解除"
                ),
                "basis": "《劳动合同法》第三十九条至第四十一条、第四十三条、第四十八条",
                "favor": "对我方",
            },
            {
                "issue": "违法解除赔偿金的计算基数与年限",
                "analysis": (
                    f"工作年限 {case_facts.get('years_of_service', 'N')} 年，"
                    f"月薪 {case_facts.get('monthly_salary', 'N')} 元，"
                    f"按 2N 计算赔偿金 {amount.get('amount_text', '（见赔偿计算）')}；"
                    "月工资按解除前十二个月平均工资计"
                ),
                "basis": "《劳动合同法》第四十七条、第八十七条",
                "favor": "对我方",
            },
            {
                "issue": "仲裁时效是否经过",
                "analysis": "自解除之日起 1 年内申请仲裁即可；注意保留解除通知、工资流水、考勤记录等证据",
                "basis": "《劳动争议调解仲裁法》第二十七条",
                "favor": "中性",
            },
        ]
    generic = [
        {
            "issue": "合同效力与核心义务履行情况",
            "analysis": "核查合同成立与生效要件、双方主给付义务的履行证据链",
            "basis": "《民法典》第五百零二条、第五百零九条、第五百七十七条",
            "favor": "中性",
        },
        {
            "issue": "违约责任与损失赔偿范围",
            "analysis": "损失赔偿不得超过违约方订立合同时可预见的损失；注意减损义务",
            "basis": "《民法典》第五百八十四条",
            "favor": "中性",
        },
    ]
    return generic


# ─────────────────────────────────────────────────────────────
# 工具 4：llm_match_precedent —— 类案匹配重排（语义相关性）
# ─────────────────────────────────────────────────────────────


async def llm_match_precedent(query: str, candidates: list[dict]) -> list[dict]:
    """对向量检索出的类案候选重排 + 给匹配理由。"""
    if not candidates:
        return []
    if settings.llm_enabled:
        try:
            brief = json.dumps(
                [{"id": c.get("id", i), "topic": c.get("topic", "")} for i, c in enumerate(candidates)],
                ensure_ascii=False,
            )
            prompt = (
                "按与案情的相关性对类案候选排序（最相关在前），只输出 JSON 数组（候选下标序）："
                f"[{{\"idx\": 0, \"reason\": \"一句话匹配理由\"}}]\n案情：{query[:600]}\n候选：{brief}"
            )
            raw = await ainvoke_with_fallback(prompt)
            ranked = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
            out = []
            for item in ranked:
                if isinstance(item, dict):
                    idx, reason = int(item.get("idx", -1)), str(item.get("reason", ""))
                else:  # 模型可能只回传下标数组 [0, 2, 1]
                    idx, reason = int(item), ""
                if 0 <= idx < len(candidates):
                    c = dict(candidates[idx])
                    c["match_reason"] = reason or c.get("topic", "")[:50]
                    out.append(c)
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_match_precedent 降级 Mock：%s", exc)
    out = []
    for i, c in enumerate(candidates):
        c = dict(c)
        c["match_reason"] = c.get("topic", "")[:50]
        out.append(c)
    return out


# ─────────────────────────────────────────────────────────────
# 工具 5：llm_generate_legal_opinion —— 法律意见书/审查意见生成
# ─────────────────────────────────────────────────────────────


async def llm_generate_legal_opinion(context: dict) -> str:
    """按上下文（企业信息/风险清单/审查结论）生成结构化意见书文本。"""
    if settings.llm_enabled:
        try:
            prompt = (
                "你是律所合伙人，撰写一份简明的法律意见书（400 字内，含：一、背景；二、法律分析；"
                "三、结论与建议；四、风险提示）。只输出意见书正文。\n"
                f"上下文：{json.dumps(context, ensure_ascii=False, default=str)[:2500]}"
            )
            return await ainvoke_with_fallback(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_generate_legal_opinion 降级 Mock：%s", exc)
    return _mock_opinion(context)


def _mock_opinion(context: dict) -> str:
    lines: list[str] = []
    title = context.get("title", "法律意见书")
    lines.append(f"【{title}】")
    lines.append("一、背景")
    if context.get("enterprise"):
        ent = context["enterprise"]
        lines.append(f"  委托企业：{ent.get('enterprise_name', '-')}（统一社会信用代码 {ent.get('credit_code', '-')}），"
                     f"注册资本 {ent.get('registered_capital', '-')}，经营范围含 {ent.get('business_scope_brief', '-')}。")
    if context.get("contract_title"):
        lines.append(f"  审查对象：《{context['contract_title']}》。")
    lines.append("二、法律分析")
    risks = context.get("risks", [])
    if risks:
        for i, r in enumerate(risks, 1):
            lines.append(f"  {i}. {r.get('risk_name', r.get('risk_type', ''))}（{r.get('level', '')}）："
                         f"{r.get('reason', '')} 依据：{r.get('basis', '') or '详见法条引用'}。")
    else:
        lines.append("  未发现重大法律风险点，交易结构在现行法律框架内可履行。")
    lines.append("三、结论与建议")
    if context.get("conclusion"):
        lines.append(f"  {context['conclusion']}")
    elif risks:
        lines.append("  建议按上述风险点逐项修订合同条款后再行签署；高风险事项须经合伙人复核确认。")
    else:
        lines.append("  可按当前版本推进，同时保留履约过程证据。")
    lines.append("四、风险提示")
    lines.append("  本意见基于委托时提供的材料与现行有效法律出具，供内部决策参考；"
                 "最终签署版本须经承办律师与合伙人双重复核。")
    return "\n".join(lines)
