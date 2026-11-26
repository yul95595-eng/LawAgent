"""解耦守门测试 —— 三 Agent 能力边界（契约唯一事实源：backend/agents/capabilities.py）。

三道防线：
1. AST 导入扫描：每个 Agent 文件从 backend.tools.* 导入的名字必须 ⊆
   白名单（域内工具 + 公共设施），越界导入直接 fail；
2. 独占 token 扫描：域独占的工具/字段 token 不得出现在其他 Agent 源码
   中（防「死代码复活」式越界，比 import 扫描更严）；
3. 意图 → Agent 唯一映射：INTENT_ROUTES 每个意图唯一归属，无交叉。
"""
from __future__ import annotations

import ast
from pathlib import Path

from backend.agents.capabilities import (
    AGENT_CAPABILITIES,
    EXCLUSIVE_TOKENS,
    tools_for,
)
from backend.orchestrator.orchestrator import INTENT_ROUTES, NODE_LABELS

AGENTS_DIR = Path(__file__).resolve().parents[1] / "backend" / "agents"
AGENT_FILES = {
    "case": "case_agent.py",
    "contract": "contract_agent.py",
    "compliance": "compliance_agent.py",
}


def _imported_tool_names(rel_path: str) -> set[str]:
    """AST 解析文件里 from backend.tools.* import 的全部名字。"""
    tree = ast.parse((AGENTS_DIR / rel_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("backend.tools"):
            names.update(alias.name for alias in node.names)
    return names


class TestToolImportBoundary:
    """防线 1：AST 导入扫描 —— 只准用白名单内的工具。"""

    def test_case_agent_tools_in_whitelist(self):
        illegal = _imported_tool_names(AGENT_FILES["case"]) - tools_for("case")
        assert not illegal, f"CaseAgent 越界导入案件域外工具：{illegal}"

    def test_contract_agent_tools_in_whitelist(self):
        illegal = _imported_tool_names(AGENT_FILES["contract"]) - tools_for("contract")
        assert not illegal, f"ContractAgent 越界导入合同域外工具：{illegal}"

    def test_compliance_agent_tools_in_whitelist(self):
        illegal = _imported_tool_names(AGENT_FILES["compliance"]) - tools_for("compliance")
        assert not illegal, f"ComplianceAgent 越界导入合规域外工具：{illegal}"

    def test_domain_tools_pairwise_disjoint(self):
        """域内工具两两不相交（公共设施除外）——Agent 间无共享工具。"""
        domains = ["case", "contract", "compliance"]
        for i, a in enumerate(domains):
            for b in domains[i + 1:]:
                shared = AGENT_CAPABILITIES[a]["tools"] & AGENT_CAPABILITIES[b]["tools"]
                assert not shared, f"{a} 与 {b} 共享域内工具：{shared}"


class TestExclusiveTokenBoundary:
    """防线 2：独占 token 扫描 —— 域外源码连提都不许提。"""

    def test_exclusive_tokens_not_leaked(self):
        for owner, tokens in EXCLUSIVE_TOKENS.items():
            for agent, rel in AGENT_FILES.items():
                if agent == owner:
                    continue
                src = (AGENTS_DIR / rel).read_text(encoding="utf-8")
                leaked = [t for t in tokens if t in src]
                assert not leaked, f"{owner} 域独占能力泄漏进 {rel}：{leaked}"

    def test_compliance_is_pure_compliance(self):
        """ComplianceAgent 纯合规：无赔偿计算/时效咨询/合同校验残留。"""
        src = (AGENTS_DIR / "compliance_agent.py").read_text(encoding="utf-8")
        for tok in ("calculate_claim_amount", "calculate_limitation_period",
                    "validate_contract_format", "risk_level_assess",
                    "limitation_inquiry", "inquiry_text", "include_claim",
                    "claim_scenario", "claim_amount"):
            assert tok not in src, f"ComplianceAgent 残留越界 token：{tok}"


class TestIntentOwnership:
    """防线 3：意图 → Agent 唯一映射。"""

    def test_intent_to_agent_unique_mapping(self):
        assert INTENT_ROUTES["case_analysis"] == "case"
        assert INTENT_ROUTES["precedent_search"] == "case"
        assert INTENT_ROUTES["limitation_inquiry"] == "case_faq"   # 案件域 FAQ 小图
        assert INTENT_ROUTES["contract_review"] == "contract"
        assert INTENT_ROUTES["clause_analysis"] == "contract"
        assert INTENT_ROUTES["compliance_check"] == "compliance"
        assert INTENT_ROUTES["legal_opinion"] == "compliance"
        assert INTENT_ROUTES["risk_assessment"] == "dual"          # 编排层合法组合（按材料分流）

    def test_capability_intents_cover_routes(self):
        """白名单意图与路由表一致（case 域含 case_faq 承接的 limitation_inquiry）。"""
        expected_targets = {
            "case": {"case", "case_faq"},
            "contract": {"contract"},
            "compliance": {"compliance"},
        }
        for agent, cap in AGENT_CAPABILITIES.items():
            for intent in cap["intents"]:
                assert INTENT_ROUTES[intent] in expected_targets[agent], (
                    f"意图 {intent} 白名单归属 {agent}，路由却指向 {INTENT_ROUTES[intent]}"
                )


class TestGraphTopology:
    def test_compliance_has_six_nodes(self):
        from backend.agents.compliance_agent import NODES as COMP_NODES

        assert len(COMP_NODES) == 6
        assert "calculate_claim_amount" not in COMP_NODES

    def test_case_has_thirteen_nodes(self):
        from backend.agents.case_agent import NODES as CASE_NODES

        # 12 节点主链（核验打回 + 复核红队）+ 1 个追问侧分支（ask_clarify）
        assert len(CASE_NODES) == 13

    def test_faq_node_has_label(self):
        assert NODE_LABELS.get("answer_limitation_faq") == "时效答复"
