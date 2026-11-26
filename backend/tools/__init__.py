"""tools —— 18 个工具，四类分工（规格文档口径）。

┌────────────┬────┬──────────────────────────────────────────────┐
│ 类别        │ 数量│ 工具                                          │
├────────────┼────┼──────────────────────────────────────────────┤
│ LLM 工具    │  5 │ llm_classify_case_type / llm_analyze_legal_risk│
│            │    │ llm_review_contract_clause /                   │
│            │    │ llm_generate_legal_opinion / llm_match_precedent│
│ 规则引擎    │  4 │ calculate_limitation_period /                  │
│            │    │ calculate_claim_amount /                       │
│            │    │ validate_contract_format / risk_level_assess   │
│ MCP 工具    │  4 │ search_law_knowledge_base /                    │
│            │    │ search_precedent_cases / web_search_legal /    │
│            │    │ query_legal_standard                           │
│ 外部API封装 │  5 │ get_case_detail / get_contract_file /          │
│            │    │ submit_review_result / query_enterprise_credit/│
│            │    │ save_legal_opinion                             │
└────────────┴────┴──────────────────────────────────────────────┘

容错降级（规格文档口径，逐类落地）：
- 外部 API：重试 1 次 → 备用源（本地 Mock）→ 友好提示；
- LLM：超时 30s / 未配 Key → 确定性 Mock（关键词规则）；
- MCP：检索失败降级关键词匹配 → 联网失败用 7 天缓存。
"""
