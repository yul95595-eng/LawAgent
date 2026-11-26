"""Agent 能力白名单 —— 三 Agent 解耦契约（唯一事实源）。

解耦原则（可上线的多 Agent 系统边界）：
1. 每个 Agent 只拥有自己业务域的能力：意图（INTENTS）与工具（TOOLS），
   不得引用其他域的工具/节点 —— 越界即架构回归，由 tests/test_decoupling.py
   的 AST 导入扫描 + 独占 token 扫描双重守门；
2. 公共设施（COMMON_TOOLS）是真正的横切能力：法条知识库检索、文书生成、
   条文核验 —— 三个 Agent 都可合法使用，不构成耦合；
3. Agent 间协作只经由 Orchestrator 的 pipeline_context 显式传递（编排层
   组合，Agent 层面互不知晓对方存在）。

域归属：
  case       案件域：案情分析 / 类案检索 / 赔偿与时效计算（含时效咨询 FAQ）
  contract   合同域：合同审查 / 条款分析 / 合伙人 HITL 审核
  compliance 合规域：企业工商体检 / 资质许可核验 / 法条引用验真
"""
from __future__ import annotations

# ── 公共横切设施（三 Agent 均可合法使用，不算耦合）─────────────
COMMON_TOOLS: frozenset[str] = frozenset({
    "search_law_knowledge_base",     # 法条知识库检索（tools.mcp_tools）
    "llm_generate_legal_opinion",    # 意见书/文书生成（tools.llm_tools）
    # 条文核验设施（retrieval.statutes）：引用抽取 + 回库验真
    "extract_citations",
    "get_article",
    "citation_exists",
})

# ── 各 Agent 能力白名单 ───────────────────────────────────────
AGENT_CAPABILITIES: dict[str, dict] = {
    "case": {
        "intents": frozenset({"case_analysis", "precedent_search", "limitation_inquiry"}),
        "tools": frozenset({
            "get_case_detail",           # 案件管理系统档案（tools.api_tools）
            "llm_classify_case_type",    # 案由分类（tools.llm_tools）
            "llm_match_precedent",       # 类案重排（tools.llm_tools）
            "llm_analyze_legal_risk",    # 争议焦点分析（tools.llm_tools）
            "calculate_claim_amount",    # 赔偿金计算（tools.rule_tools）
            "calculate_limitation_period",  # 时效计算（tools.rule_tools）
            "search_precedent_cases",    # 类案检索（tools.mcp_tools）
        }),
    },
    "contract": {
        "intents": frozenset({"contract_review", "clause_analysis"}),
        "tools": frozenset({
            "get_contract_file",         # 合同管理系统调档（tools.api_tools）
            "submit_review_result",      # 审查结果回传（tools.api_tools）
            "llm_review_contract_clause",  # 条款隐性风险（tools.llm_tools）
            "validate_contract_format",  # 必备条款校验（tools.rule_tools）
            "risk_level_assess",         # 风险等级评定（tools.rule_tools）
        }),
    },
    "compliance": {
        "intents": frozenset({"compliance_check", "legal_opinion"}),
        "tools": frozenset({
            "query_enterprise_credit",   # 企业工商档案（tools.api_tools）
        }),
    },
}

# ── 独占 token（第二道防线：禁止出现在其他 Agent 源码中）────────
# 工具名之外还锁定关键状态字段/节点名，防止「死代码复活」式越界。
EXCLUSIVE_TOKENS: dict[str, tuple[str, ...]] = {
    "case": (
        "search_precedent_cases", "calculate_claim_amount", "calculate_limitation_period",
    ),
    "contract": (
        "validate_contract_format", "risk_level_assess", "get_contract_file",
        "submit_review_result", "llm_review_contract_clause",
    ),
    "compliance": (
        "query_enterprise_credit",
    ),
}


def tools_for(agent: str) -> frozenset[str]:
    """某 Agent 的合法工具全集（域内 + 公共设施）。"""
    return AGENT_CAPABILITIES[agent]["tools"] | COMMON_TOOLS
