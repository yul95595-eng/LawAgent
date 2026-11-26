"""类案知识库 —— 8 类案由真实判例的加载、检索与统计。

数据源：data/legal/precedents.json —— 联网采集/公开渠道核实的真实判例，
每条含真实案号（可回溯）、法院、案情、法律依据、裁判结果，案由对齐
系统 8 类案由枚举（case_type）。

用途：
1. match_precedent_cases / search_precedent_cases（MCP 工具）的检索底座，
   支持按案由过滤的 Top-K 相似类案检索（评测口径：500 条评测集、
   8 类案由、Top-5 命中率 60%→92%）；
2. 案件分析「类案参考」段落的证据来源（真实案号可回溯）。
"""
from __future__ import annotations

import json
from functools import lru_cache

from backend.core.config import LEGAL_DATA_DIR
from backend.retrieval.vectorstore import LocalTfidfStore, VectorDoc

PRECEDENTS_PATH = LEGAL_DATA_DIR / "precedents.json"

# 8 类案由（case_type 枚举，规格文档口径）
CASE_TYPES = [
    "labor_termination",     # 违法解除劳动合同纠纷
    "labor_probation",       # 试用期纠纷
    "labor_non_compete",     # 竞业限制纠纷
    "labor_service_period",  # 服务期与违约金纠纷
    "contract_sales",        # 买卖合同纠纷
    "contract_equity",       # 股权转让与对赌纠纷
    "contract_lending",      # 民间借贷纠纷
    "ip_license",            # 知识产权合同纠纷
]

CASE_TYPE_LABELS = {
    "labor_termination": "违法解除劳动合同纠纷",
    "labor_probation": "试用期纠纷",
    "labor_non_compete": "竞业限制纠纷",
    "labor_service_period": "服务期与违约金纠纷",
    "contract_sales": "买卖合同纠纷",
    "contract_equity": "股权转让与对赌纠纷",
    "contract_lending": "民间借贷纠纷",
    "ip_license": "知识产权合同纠纷",
}


@lru_cache(maxsize=1)
def load_precedents() -> list[dict]:
    data = json.loads(PRECEDENTS_PATH.read_text(encoding="utf-8"))
    return data["precedents"]


@lru_cache(maxsize=1)
def _store() -> LocalTfidfStore:
    store = LocalTfidfStore()
    for i, c in enumerate(load_precedents()):
        text = " ".join(str(c.get(k, "")) for k in ("case_type_label", "topic", "facts", "issue", "law_basis", "result"))
        store.add(VectorDoc(
            doc_id=c["id"],
            text=text,
            metadata={
                "case_no": c["case_no"],
                "court": c.get("court", ""),
                "year": c.get("year"),
                "case_type": c["case_type"],
                "case_type_label": CASE_TYPE_LABELS.get(c["case_type"], c["case_type"]),
                "topic": c.get("topic", ""),
                "result": c.get("result", ""),
                "law_basis": c.get("law_basis", ""),
            },
        ))
    store.build()
    return store


def search_precedents(query: str, case_type: str | None = None, top_k: int = 3) -> list[dict]:
    """类案检索：向量相似 + 案由过滤。

    case_type 给定时只在该案由内检索（类案检索的口径就是「同案由」）；
    返回 [{id, case_no, court, year, case_type, topic, result, law_basis, score}]。
    """
    def flt(doc) -> bool:
        return case_type is None or doc.metadata.get("case_type") == case_type

    hits = _store().search(query, top_k=top_k * 3 if case_type else top_k, fliter_fn=flt)
    # metadata 拍平为一级字段（与 docstring 承诺的返回结构一致），下游
    # （llm_match_precedent / MCP 工具 / 结果卡片）直接取 case_no/topic 等。
    out: list[dict] = []
    for h in hits:
        item = {"id": h["doc_id"], "score": h["score"]}
        item.update(h.get("metadata", {}))
        out.append(item)
    return out[:top_k]


def stats() -> dict:
    cases = load_precedents()
    by_type: dict[str, int] = {}
    for c in cases:
        by_type[c["case_type"]] = by_type.get(c["case_type"], 0) + 1
    return {"total": len(cases), "by_case_type": by_type}


if __name__ == "__main__":
    s = stats()
    print(f"判例库：{s['total']} 条")
    for k, v in sorted(s["by_case_type"].items()):
        print(f"  {CASE_TYPE_LABELS.get(k, k):14s} {v} 条")
