"""通用 Schema —— 8 类意图枚举与共享模型。

8 类意图（规格文档口径，Orchestrator 路由依据）：
  case_analysis      案件分析       → CaseAgent
  precedent_search   类案检索       → CaseAgent
  limitation_inquiry 时效查询       → CaseAgent（案件域 FAQ 小图，规则直出）
  contract_review    合同审查       → ContractAgent
  clause_analysis    条款分析       → ContractAgent
  compliance_check   企业合规体检    → ComplianceAgent
  legal_opinion      法律意见书      → ComplianceAgent（引用校验后出意见）
  risk_assessment    风险评估       → Case / Contract / Compliance 双路由（按材料类型）
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Intent(str, Enum):
    case_analysis = "case_analysis"
    precedent_search = "precedent_search"
    contract_review = "contract_review"
    clause_analysis = "clause_analysis"
    compliance_check = "compliance_check"
    legal_opinion = "legal_opinion"
    limitation_inquiry = "limitation_inquiry"
    risk_assessment = "risk_assessment"


INTENT_LABELS: dict[str, str] = {
    "case_analysis": "案件分析",
    "precedent_search": "类案检索",
    "contract_review": "合同审查",
    "clause_analysis": "条款分析",
    "compliance_check": "企业合规体检",
    "legal_opinion": "法律意见书",
    "limitation_inquiry": "诉讼时效查询",
    "risk_assessment": "风险评估",
}


class RiskPoint(BaseModel):
    """风险点 —— 合同审查/合规检查的统一风险结构。"""

    risk_id: str = ""
    clause_id: str = ""
    clause_text: str = ""
    risk_type: str = ""          # 风险类型（30+ 类清单键）
    risk_name: str = ""          # 人话名称
    level: str = Field(default="medium", pattern="^(low|medium|high)$")
    reason: str = ""
    suggestion: str = ""
    basis: str = ""              # 法条依据（如 劳动合同法第二十二条）
    source: str = "llm"          # rule=规则显式 / llm=LLM 隐性（双引擎口径）
    confidence: float = 0.8


class Citation(BaseModel):
    """法条引用（verify_citation_accuracy 的校验对象）。"""

    law: str
    article_no: str
    quote: str = ""              # 引用的条文原文（校验后回填）
    valid: bool = True           # 引用是否真实存在且原文一致
    message: str = ""            # 校验不通过时的原因


class AgentResult(BaseModel):
    """单次 Agent 执行的结构化结果卡片（前端结果卡 + 落库 result_json）。"""

    agent: str                    # case/contract/compliance
    intent: str
    title: str = ""
    summary: str = ""             # 面向用户的自然语言结论
    result: dict = Field(default_factory=dict)   # 各 Agent 的业务结果
    citations: list[Citation] = Field(default_factory=list)
    audit_hint: str = ""          # 合规提示（AI 辅助、需人工复核）
