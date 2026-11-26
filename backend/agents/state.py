"""三个 Agent 的共享 State 定义。

设计要点：
1. 每个 Agent 一张独立 TypedDict State（CaseState / ContractState / ComplianceState），
   字段即该 Agent 26 节点中读写的数据总线；
2. Pipeline（跨 Agent 串联）靠显式的 context 传递函数：上游 Agent 结果里
   提取关键字段（企业信息、风险清单）注入下游初始 state，而不是共享一个大
   state —— 边界清晰，哪个 Agent 消费什么一目了然；
3. 所有 list 字段不用 reducer（本系统节点是串行链，单写者语义），
   避免旧项目里 operator.add 的二次累加陷阱。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def overwrite(left: Any, right: Any) -> Any:
    """默认 reducer：后写覆盖（串行链的直白语义）。"""
    return right if right is not None else left


class CaseState(TypedDict, total=False):
    """CaseAgent 案件分析 —— 12 节点主链 + 1 个追问侧分支。"""

    # 输入
    thread_id: str
    tenant_id: str
    user_id: str
    case_text: str                     # 案情描述/起诉材料摘录
    case_id: str                       # 外部案件管理系统案件号（可选）
    focus_case_type: str               # 用户指定案由（precedent_search 意图）
    pipeline_context: dict             # Pipeline 跨 Agent 上下文（编排器注入）
    # 中间产物
    case_type: str                     # 8 类案由
    case_type_label: str
    case_facts: dict                   # 结构化事实（年限/月薪/解除方式…）
    clarify: dict                      # 槽位完整性报告（缺什么、问什么、是否阻塞）
    clarify_pending: bool              # 真：信息不足已中断，未出结论、未落库
    applicable_law: list[dict]         # 适用法条
    law_gap_note: str                  # 知识库未覆盖时的显式说明（不硬凑条文）
    retrieval_trace: list[dict]        # 每轮检索的 query/分数/是否采纳（可解释性）
    law_coverage: dict                 # 关键条文覆盖核验（缺哪条、补检几次）
    law_retry: int                     # 核验打回检索的补检次数（上限 1）
    retry_hint: str                    # 打回时带的缺口提示（检索 Agent 补搜用）
    review_findings: list[dict]        # 复核红队发现（规则 / 等级 / 说明 / 改法）
    review_verdict: str                # pass / warn / reject / forced_pass
    review_retry: int                  # 复核打回起草的次数（上限 1）
    review_feedback: str               # 打回时给起草的具体修改要求
    precedents: list[dict]             # 类案检索结果
    legal_issues: list[dict]           # 争议焦点分析
    limitation: dict                   # 时效结论
    win_rate: float
    win_rate_reason: str
    framework: dict                    # 分析框架/工作大纲
    saved_case_id: str                 # 落库后 case_analyses.case_id
    # 输出
    summary: str


class LimitationFaqState(TypedDict, total=False):
    """时效咨询 FAQ 小图（案件域，limitation_inquiry 意图）—— 单节点免落库。

    与 CaseState 分离：FAQ 是纯咨询轻链路，不需要案件域中间产物
    （类案/争议焦点/胜诉率…），独立小状态总线边界更清晰。
    """

    # 输入
    thread_id: str
    tenant_id: str
    user_id: str
    inquiry_text: str                  # 时效咨询问题
    pipeline_context: dict
    # 中间产物
    citation_check: dict               # 引用回条文库验真（公共核验设施）
    # 输出
    report: dict                       # {kind: limitation_inquiry, answer, limitation, citations}
    summary: str


class ContractState(TypedDict, total=False):
    """ContractAgent 合同审查 —— 8 节点 + interrupt HitL。"""

    # 输入
    thread_id: str
    tenant_id: str
    user_id: str
    contract_text: str
    contract_title: str
    contract_id: str                   # 外部合同管理系统文件号（可选）
    clause_focus: str                  # clause_analysis 意图：只分析该条款
    pipeline_context: dict             # Pipeline 跨 Agent 上下文（编排器注入）
    # 中间产物
    clauses: list[dict]                # 切分后的条款 [{clause_id, heading, text}]
    clause_risks: list[dict]           # 逐条审查风险（LLM 隐性引擎）
    rule_risks: list[dict]             # 规则显式命中（30+ 清单的可确定性子集）
    risk_points: list[dict]            # 合并后的风险清单
    risk_level: str                    # low/medium/high（评分公式）
    format_check: dict                 # 8 大必备条款校验
    review_opinion: str                # 审查意见书
    revision_notes: str                # 合伙人退回的修改要求
    review_decision: str               # confirmed / returned
    review_comment: str                # 合伙人批注
    reviewer_id: str
    # 输出
    review_id: str                     # 落库 contract_reviews.review_id
    summary: str


class ComplianceState(TypedDict, total=False):
    """ComplianceAgent 合规检测 —— 6 节点串行链（企业合规体检单一职责）。"""

    # 输入
    thread_id: str
    tenant_id: str
    user_id: str
    enterprise_name: str               # 企业名称（工商库 Mock 查询键）
    enterprise_text: str               # 企业提供的材料（自由文本）
    pipeline_context: dict             # Pipeline 跨 Agent 上下文（编排器注入）
    risk_score: float                  # 风险评分 0~100（报告节点写入）
    # 中间产物
    enterprise_info: dict              # 工商信息（外部 API Mock）
    scope_check: dict                  # 经营范围合规
    license_check: dict                # 资质许可合规
    opinion_draft: str                 # 意见书草稿（legal_opinion 意图）
    citation_check: dict               # 法条引用校验结果（幻觉治理）
    report: dict                       # 合规报告
    saved_profile_id: str              # 落库 enterprise_compliance_profiles
    # 输出
    summary: str
