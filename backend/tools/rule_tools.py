"""规则引擎工具（4 个）—— 能确定性判定的，绝不交给模型。

四个规则工具全部是纯函数：输入明确、输出可复现、法条依据写死在返回值里。
这是「法务辅助系统」与「AI 自动审」的核心区别，也是把合同审查从 45 分钟
压到 3 分钟的主力（规则部分 0 耗时、0 幻觉）。
"""
from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────
# 工具 1：calculate_limitation_period —— 时效计算（规则直出）
#
# 依据：劳动争议仲裁时效 1 年（劳动争议调解仲裁法第 27 条）；
#      民事诉讼时效 3 年、最长 20 年（民法典第 188 条）；
#      劳动关系存续期间拖欠劳动报酬争议不受 1 年限制（27 条第 4 款）。
# ─────────────────────────────────────────────────────────────

_LABOR_CASE_TYPES = {"labor_termination", "labor_probation", "labor_non_compete", "labor_service_period"}


def calculate_limitation_period(
    case_type: str = "",
    dispute_kind: str = "",       # wage_arrears 拖欠工资 / other 一般争议
    relationship_ongoing: bool = False,
) -> dict:
    """按案件类型计算时效。返回 {period_text, legal_basis, notes, expires_hint}。"""
    if case_type in _LABOR_CASE_TYPES or dispute_kind == "labor":
        if dispute_kind == "wage_arrears" and relationship_ongoing:
            return {
                "period_text": "劳动关系存续期间，追索劳动报酬不受 1 年仲裁时效限制；劳动关系终止的，应自终止之日起 1 年内提出",
                "legal_basis": "《劳动争议调解仲裁法》第二十七条第四款",
                "expires_hint": "劳动关系终止之日起 1 年",
                "notes": "在职期间拖欠工资的时效特殊保护，仅适用于劳动报酬争议",
            }
        return {
            "period_text": "劳动争议申请仲裁的时效期间为 1 年，自知道或应当知道权利被侵害之日起计算",
            "legal_basis": "《劳动争议调解仲裁法》第二十七条",
            "expires_hint": "权利被侵害之日起 1 年（主张权利/请求救济/对方同意履行的，时效中断重算）",
            "notes": "因不可抗力或其他正当理由可中止时效",
        }
    return {
        "period_text": "向人民法院请求保护民事权利的诉讼时效期间为 3 年；自权利受到损害之日起超过 20 年的不予保护",
        "legal_basis": "《民法典》第一百八十八条",
        "expires_hint": "知道或应当知道权利受损及义务人之日起 3 年（最长 20 年）",
        "notes": "法律另有规定的（如涉外、特殊合同），依照其规定",
    }


# ─────────────────────────────────────────────────────────────
# 工具 2：calculate_claim_amount —— 赔偿金计算（规则直出）
#
# 依据：经济补偿 N（劳动合同法第 47 条）；违法解除赔偿金 2N（第 87 条
#       + 第 48 条）；无过失性辞退未提前 30 日书面通知的代通知金 +1
#       （第 40 条）。年限折算：每满 1 年 → 1 个月；6 个月以上不满 1 年
#       → 按 1 年；不满 6 个月 → 0.5 个月。
# ─────────────────────────────────────────────────────────────

_SERVICE_MONTH_CAP = 12  # 社平工资 3 倍封顶情形补偿年限最高 12 年（47 条第 2 款）


def _months_of_service(years_of_service: float) -> float:
    """工作年限 → 经济补偿月数（47 条折算规则）。3年2个月 → 3.5。"""
    years = int(years_of_service)
    months = round((years_of_service - years) * 12)
    extra = 1.0 if months >= 6 else (0.5 if months > 0 else 0.0)
    return float(years) + extra


def calculate_claim_amount(
    scenario: str,                 # economic_compensation / wrongful_termination / no_notice_termination
    years_of_service: float,
    monthly_salary: float,
    salary_over_3x_social_avg: bool = False,
) -> dict:
    """按场景计算赔偿金额。返回 {formula, months, amount, legal_basis, notes}。"""
    if monthly_salary <= 0 or years_of_service <= 0:
        return {"formula": "", "months": 0, "amount": 0, "legal_basis": "", "notes": "工作年限或月薪数据缺失，无法计算"}

    n = _months_of_service(years_of_service)
    # 高收入封顶：月薪超社平 3 倍 → 按 3 倍计、年限最高 12 年（47 条第 2 款）
    capped = False
    if salary_over_3x_social_avg:
        capped = True
        n = min(n, float(_SERVICE_MONTH_CAP))

    if scenario == "economic_compensation":
        months, multiplier = n, 1
        basis = "《劳动合同法》第四十七条（经济补偿按工作年限每满一年支付一个月工资）"
        formula = "N"
    elif scenario == "wrongful_termination":
        months, multiplier = n, 2
        basis = "《劳动合同法》第八十七条（违法解除按经济补偿标准的二倍支付赔偿金）、第四十八条"
        formula = "2N"
    elif scenario == "no_notice_termination":
        months, multiplier = n + 1, 1
        basis = "《劳动合同法》第四十条（未提前 30 日书面通知的，额外支付一个月工资）、第四十七条"
        formula = "N+1"
    else:
        return {"formula": "", "months": 0, "amount": 0, "legal_basis": "", "notes": f"未知场景 {scenario}"}

    amount = monthly_salary * months * multiplier
    notes = []
    if capped:
        notes.append("月薪超当地上年度职工月平均工资 3 倍：按 3 倍标准计、补偿年限最高 12 年（第 47 条第 2 款）")
    notes.append(f"年限折算：{years_of_service} 年 → {months:g} 个月")
    return {
        "formula": formula,
        "months": months * multiplier,
        "amount": round(amount, 2),
        "legal_basis": basis,
        "notes": "；".join(notes),
    }


# ─────────────────────────────────────────────────────────────
# 工具 3：validate_contract_format —— 8 大必备条款校验（规则直出）
#
# 依据：劳动合同必备条款（劳动合同法第 17 条第 1 款，9 项归并为 8 大类）
#      一般合同主要条款（民法典第 470 条建议条款）。
# ─────────────────────────────────────────────────────────────

_LABOR_REQUIRED: list[dict] = [
    {"key": "parties", "name": "用人单位与劳动者基本信息", "keywords": ["公司", "用人单位", "甲方", "乙方", "劳动者", "身份证"]},
    {"key": "term", "name": "劳动合同期限", "keywords": ["合同期限", "固定期限", "无固定期限", "试用期", "期限为", "期限自"]},
    {"key": "work_content", "name": "工作内容和工作地点", "keywords": ["工作岗位", "工作内容", "工作地点", "职责"]},
    {"key": "work_time", "name": "工作时间和休息休假", "keywords": ["工作时间", "工时", "休假", "年休假", "休息"]},
    {"key": "remuneration", "name": "劳动报酬", "keywords": ["工资", "劳动报酬", "月薪", "薪酬", "报酬"]},
    {"key": "social_insurance", "name": "社会保险", "keywords": ["社会保险", "社保", "保险"]},
    {"key": "protection", "name": "劳动保护/劳动条件/职业危害防护", "keywords": ["劳动保护", "劳动条件", "职业危害", "防护", "安全生产"]},
]

_GENERAL_REQUIRED: list[dict] = [
    {"key": "subject", "name": "标的（交易对象）", "keywords": ["标的", "产品", "货物", "股权", "服务内容", "标的物"]},
    {"key": "quantity_quality", "name": "数量与质量", "keywords": ["数量", "质量", "规格", "标准"]},
    {"key": "price", "name": "价款或报酬", "keywords": ["价款", "价格", "金额", "费用", "报酬"]},
    {"key": "performance", "name": "履行期限/地点/方式", "keywords": ["履行", "交付", "支付方式", "履行期限", "交货"]},
    {"key": "liability", "name": "违约责任", "keywords": ["违约", "赔偿责任", "违约金"]},
    {"key": "dispute", "name": "争议解决方式", "keywords": ["争议", "诉讼", "仲裁", "管辖"]},
]


def validate_contract_format(text: str, contract_kind: str = "labor") -> dict:
    """必备条款校验。返回 {complete, missing[], present[], legal_basis}。"""
    checklist = _LABOR_REQUIRED if contract_kind == "labor" else _GENERAL_REQUIRED
    present, missing = [], []
    for item in checklist:
        if any(kw in text for kw in item["keywords"]):
            present.append(item["name"])
        else:
            missing.append(item["name"])
    basis = (
        "《劳动合同法》第十七条（劳动合同必备条款）"
        if contract_kind == "labor"
        else "《民法典》第四百七十条（合同一般条款）"
    )
    return {
        "complete": len(missing) == 0,
        "present": present,
        "missing": missing,
        "legal_basis": basis,
        "checked_count": len(checklist),
    }


# ─────────────────────────────────────────────────────────────
# 工具 4：risk_level_assess —— 风险等级评定（评分公式，规则直出）
#
# 公式：high=30 分 / medium=10 分 / low=3 分累加；总分 ≥30 → 高风险，
#      ≥10 → 中风险，否则低风险；存在「违反强制性规定」类风险（如
#      法定外违约金、试用期超限）直接判高风险（一票否决）。
# ─────────────────────────────────────────────────────────────

_SCORE = {"high": 30, "medium": 10, "low": 3}

# 一票否决型风险：命中即高风险（违反法律强制性规定 / 重大资金风险）
_VETO_RISK_TYPES = {
    "penalty_illegal",              # 法定外劳动者违约金（劳动合同法25条）
    "trial_over_limit",             # 试用期超法定上限（19条）
    "non_compete_over_2y",          # 竞业限制超 2 年（24条）
    "private_account_payment",      # 私户收付款（税收征管法63条）
    "buyback_with_target_company",  # 与目标公司对赌未完成减资（九民纪要5条）
    "ultra_vires_business",         # 超经营范围经营（公司法）
}


def risk_level_assess(risk_points: list[dict]) -> dict:
    """评分定级。返回 {risk_level, score, counts{high/medium/low}, veto_hit}。"""
    score = 0
    counts = {"high": 0, "medium": 0, "low": 0}
    veto_hit = ""
    for r in risk_points:
        level = r.get("level", "low")
        counts[level] = counts.get(level, 0) + 1
        score += _SCORE.get(level, 3)
        if not veto_hit and r.get("risk_type") in _VETO_RISK_TYPES:
            veto_hit = r.get("risk_name") or r.get("risk_type", "")
    if veto_hit or score >= 30:
        risk_level = "high"
    elif score >= 10:
        risk_level = "medium"
    else:
        risk_level = "low"
    return {
        "risk_level": risk_level,
        "score": score,
        "counts": counts,
        "veto_hit": veto_hit,
        "formula": "high=30/medium=10/low=3 累加；≥30 高、≥10 中；强制性规定类风险一票否决",
    }
