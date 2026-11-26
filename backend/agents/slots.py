"""关键槽位检查 —— 检索之前的「问清楚」环节（纯确定性，不调模型）。

为什么要有这一层
----------------
RAG 的失败大多不在检索算法，而在 query 的信息量。实测同一套 TF-IDF 引擎：

    整段案情拼进 query：「违法解除劳动合同纠纷 劳动者王某2019年3月入职…」
        → top1 = 0.090，正确答案《劳动合同法》第八十七条根本不在 top-6
    换成术语式 query：「违法解除 赔偿金 2N」
        → top1 = 0.343，第八十七条排名第一

引擎没变，变的是「问什么」。而 query 里的信息从哪来？用户脑子里。
库里没有的东西，检索算法再强也查不出来 —— 只能问出来。

所以这一层的职责是：**在检索前判断信息够不够，缺的先问，不要猜。**

设计原则
--------
1. 全确定性：槽位填充靠正则 + 已抽取的事实，不调 LLM，不花 token，可回归；
2. 分级：blocking（缺了结论必错）/ recommended（缺了结论打折）；
3. **只问缺的**：已填的不重复问，避免「固定五连问」把用户问烦；
4. 长文本不阻塞：用户既然写了一大段案情，就该给出分析并标注缺口，
   而不是拦着他先填表 —— 只有「一句话丢过来」才值得打断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 用户「一句话丢过来」的判据（字符数）：低于此值且缺关键槽位 → 阻塞追问。
# 口径刻意偏松：打断用户的成本高于多给一段带缺口标注的分析。
SHORT_TEXT_CHARS = 50


@dataclass(frozen=True)
class Slot:
    """一个待填的关键信息。"""

    key: str
    label: str
    question: str
    level: str                      # blocking | recommended
    patterns: tuple[str, ...] = ()  # 文本侧填充判据（正则）
    impact: str = ""                # 缺了会影响什么结论


@dataclass
class SlotReport:
    case_type: str
    filled: list[Slot] = field(default_factory=list)
    blocking_missing: list[Slot] = field(default_factory=list)
    recommended_missing: list[Slot] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.filled) + len(self.blocking_missing) + len(self.recommended_missing)

    @property
    def coverage(self) -> float:
        return round(len(self.filled) / self.total, 3) if self.total else 1.0

    @property
    def questions(self) -> list[str]:
        """按「缺的才问」原则生成追问话术：先关键后建议。"""
        return [s.question for s in self.blocking_missing + self.recommended_missing]

    def to_dict(self) -> dict:
        return {
            "case_type": self.case_type,
            "coverage": self.coverage,
            "filled": [{"key": s.key, "label": s.label} for s in self.filled],
            "missing_blocking": [
                {"key": s.key, "label": s.label, "impact": s.impact}
                for s in self.blocking_missing
            ],
            "missing_recommended": [
                {"key": s.key, "label": s.label, "impact": s.impact}
                for s in self.recommended_missing
            ],
            "questions": self.questions,
        }


# ── 8 类案由的槽位定义（与 retrieval/precedents.py 的 case_type 枚举对齐）──
# blocking：缺了「结论必错」的信息
# recommended：缺了「结论打折」的信息

SLOT_SCHEMAS: dict[str, tuple[Slot, ...]] = {
    "labor_termination": (
        Slot("years_of_service", "工作年限", "从入职到离职一共工作了多久（几年几个月）？",
             "blocking", (r"工作(?:年限)?[为约了]?\s*\d", r"\d+\s*年\s*\d*\s*个?月",
                          r"入职\s*\d+\s*个?月", r"入职.{0,12}年"),
             "决定赔偿金月数（N/N+1/2N）与仲裁时效起算"),
        Slot("monthly_salary", "月工资", "离职前 12 个月的平均工资是多少？",
             "blocking", (r"月(?:薪|工资|平均工资)", r"工资.{0,6}\d"), "决定赔偿金基数（且受社平三倍封顶）"),
        Slot("termination_manner", "解除方式", "公司是口头通知还是出了书面解除通知？",
             "recommended", (r"口头", r"书面", r"辞退", r"解雇", r"解除劳动合同"),
             "影响违法解除的举证责任分配"),
    ),
    "labor_probation": (
        Slot("contract_term", "劳动合同期限", "这份劳动合同签了几年？",
             "blocking", (r"\d+\s*年期", r"合同期限", r"一年期", r"三年期"),
             "试用期上限随合同期限变化：1 年以内→1 个月，1~3 年→2 个月，3 年以上→6 个月"),
        Slot("probation_length", "约定试用期", "约定的试用期是几个月？",
             "blocking", (r"试用期.{0,8}(?:\d+|一|二|三|六)\s*个?月", r"试用期\d"),
             "决定试用期约定是否超过法定上限（超过部分无效）"),
        Slot("probation_salary_ratio", "试用期工资", "试用期工资是转正工资的百分之多少？",
             "recommended", (r"试用期工资", r"转正工资的", r"\d{2}\s*%"),
             "低于 80% 或低于当地最低工资即违法"),
    ),
    "labor_non_compete": (
        Slot("non_compete_term", "竞业限制期限", "竞业限制约定的期限是多久？",
             "blocking", (r"竞业.{0,10}\d+\s*年", r"竞业.{0,6}期限"),
             "超过 2 年的部分无效"),
        Slot("compensation", "竞业补偿", "离职后每月给多少竞业限制补偿？给了几个月？",
             "blocking", (r"竞业.{0,10}补偿", r"经济补偿", r"补偿金"),
             "未约定或未支付补偿满 3 个月，劳动者可请求解除竞业限制"),
        Slot("penalty", "违约金", "协议约定的违约金是多少？",
             "recommended", (r"违约金"), "影响违约金酌减的请求"),
    ),
    "labor_service_period": (
        Slot("training_fee", "培训费用", "公司为你支付的专项培训费是多少？",
             "blocking", (r"培训费", r"培训费用.{0,6}\d"),
             "违约金不得超过培训费用"),
        Slot("service_term", "服务期", "约定的服务期是几年？已履行了多久？",
             "blocking", (r"服务期"), "违约金按未履行部分分摊"),
        Slot("penalty", "违约金", "约定的违约金是多少？",
             "recommended", (r"违约金"), "影响违约金是否过高"),
    ),
    "contract_sales": (
        Slot("amount", "合同价款", "合同总价款是多少？已付多少？",
             "recommended", (r"价款", r"金额", r"\d+\s*万元"), "影响损失与违约金计算"),
        Slot("breach", "违约情形", "对方具体怎么违约的（逾期交货／质量不符／拒付）？",
             "blocking", (r"逾期", r"违约", r"质量.{0,4}(?:瑕疵|问题)", r"未(?:付|交货)"),
             "没有违约事实无法判断解除权与赔偿范围"),
    ),
    "contract_equity": (
        Slot("deal_terms", "核心条款", "协议里有哪些关键条款（对赌／回购／优先清算）？",
             "blocking", (r"对赌", r"回购", r"反稀释", r"优先清算"),
             "决定请求权基础是合同违约还是公司治理争议"),
        Slot("performance", "业绩承诺", "承诺的业绩指标是多少、实际完成了多少？",
             "recommended", (r"业绩", r"承诺"), "影响对赌条件是否触发"),
    ),
    "contract_lending": (
        Slot("principal", "借款本金", "借款本金是多少？",
             "recommended", (r"借款", r"本金", r"\d+\s*万元"), "影响本金返还请求"),
        Slot("interest", "利息约定", "约定的利息是多少（月息／年利率）？",
             "recommended", (r"利息", r"利率", r"月息"), "超过 LPR 四倍的部分不受保护"),
    ),
    "ip_license": (
        Slot("license_fee", "许可费", "许可费是多少、按什么方式支付？",
             "recommended", (r"许可费", r"使用费", r"费用"), "影响欠付许可费的请求金额"),
        Slot("breach", "违约情形", "被许可方具体怎么违约的？",
             "blocking", (r"逾期", r"未(?:支付|付款)", r"违约"), "决定能否解除许可合同"),
    ),
}


def _find(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _is_empty(value) -> bool:
    return value in (None, "", [], {}, 0)


def check_slots(case_type: str, facts: dict, text: str) -> SlotReport:
    """检查某案由下关键槽位的填充情况（已抽取的事实优先，其次文本正则）。"""
    facts = facts or {}
    text = text or ""
    report = SlotReport(case_type=case_type)
    for slot in SLOT_SCHEMAS.get(case_type, ()):
        filled = not _is_empty(facts.get(slot.key)) or _find(text, slot.patterns)
        if filled:
            report.filled.append(slot)
        elif slot.level == "blocking":
            report.blocking_missing.append(slot)
        else:
            report.recommended_missing.append(slot)
    return report


def should_block(report: SlotReport, text: str) -> tuple[bool, str]:
    """是否在检索前打断用户、先要信息。

    判定口径（宁松勿紧：打断用户的成本高于多给一段带缺口标注的分析）：
    - 没有关键槽位缺失 → 不打断；
    - 用户只丢了一句话（< SHORT_TEXT_CHARS）且缺关键槽位 → 打断，先问；
    - 用户已经写了一大段案情 → 不打断，照常分析，缺口在报告里标注。

    为什么不按「缺几个」判：写了两百字案情但没写月薪的，是「漏了一项」，
    应当给分析并提示；写「公司把我开了能赔多少」的，是「只有一句话」，
    硬算出来的金额就是编的 —— 区别在信息总量，不在缺失项数。
    """
    blocking = report.blocking_missing
    if not blocking:
        return False, ""
    length = len((text or "").strip())
    if length < SHORT_TEXT_CHARS:
        labels = "、".join(s.label for s in blocking)
        return True, f"文本仅 {length} 字，缺「{labels}」等关键信息，无法给出可靠结论"
    return False, ""


def build_clarify_text(report: SlotReport) -> str:
    """生成给用户看的追问文案（缺什么问什么，不重复问已答的）。"""
    if not report.questions:
        return ""
    lines = ["【需要补充以下信息，才能给出可靠结论】"]
    for i, s in enumerate(report.blocking_missing, 1):
        lines.append(f"{i}. {s.label}：{s.question}" + (f"（{s.impact}）" if s.impact else ""))
    for s in report.recommended_missing:
        lines.append(f"· 可选补充 —— {s.label}：{s.question}")
    lines.append("补充后我会重新检索法条与类案，再出分析结论；在此之前不输出赔偿金额等确定性结论。")
    return "\n".join(lines)
