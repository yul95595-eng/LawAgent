"""法条检索式的构造与结果融合（旧三路写死 / 新检索 Agent 共用）。

抽出来的原因：改造后「三路写死」和「受限自主循环」必须产出**完全一致**
的融合口径（每路头部必进、按分补足、最低采纳分），否则新旧两套结果
不可比，评测 diff 就失去意义。
"""
from __future__ import annotations

MIN_ACCEPT_SCORE = 0.08    # 单条最低采纳分：余弦 0.05 在 329 条语料里属于噪声
MAX_LAW_ROUNDS = 3         # 旧三路口径的轮数上限

# 案由 → 术语式检索包（取自条文库 keywords 的高频法律术语，确定性可回归）
CASE_TERMS: dict[str, str] = {
    "labor_termination": "违法解除 经济补偿 赔偿金 二倍 举证责任",
    "labor_probation": "试用期 期限 工资 标准 转正",
    "labor_non_compete": "竞业限制 经济补偿 违约金 期限",
    "labor_service_period": "服务期 培训费 违约金",
    "contract_sales": "买卖合同 违约责任 解除 质量瑕疵 逾期",
    "contract_equity": "股权转让 对赌 回购 违约责任",
    "contract_lending": "民间借贷 利息 利率 返还 本金",
    "ip_license": "著作权许可 使用费 解除 违约责任",
}


def fact_terms(facts: dict) -> str:
    """把已抽取的事实压成检索词（带数字的长句会稀释 TF-IDF 权重）。"""
    parts: list[str] = []
    if facts.get("years_of_service_text"):
        parts.append(f"工作{facts['years_of_service_text']}")
    if facts.get("monthly_salary"):
        parts.append("月薪")
    if facts.get("termination_manner"):
        parts.append(str(facts["termination_manner"]))
    return " ".join(parts)


def build_law_queries(case_type: str, label: str, facts: dict, text: str) -> list[str]:
    """三路检索式：术语短查询 → 案由术语包 → 原文长查询（顺序即优先级）。

    第 3 路刻意保留「整段案情」这种低分查询：单独用会把正确答案挤出
    top-6，但作为**并集的一员**能补回长尾具体条文。
    """
    terms = CASE_TERMS.get(case_type, label)
    fact = fact_terms(facts)
    text = (text or "").strip()
    raw = [
        f"{label} {fact}".strip() or text[:60],
        terms,
        f"{label} {text[:120]}".strip(),
    ]
    seen: set[str] = set()
    queries: list[str] = []
    for q in raw:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return queries[:MAX_LAW_ROUNDS]


def _key(hit: dict) -> str:
    return hit.get("cite") or f"{hit.get('law', '')}{hit.get('article_no', '')}"


def merge_law_hits(rounds: list[dict]) -> tuple[list[dict], list[dict]]:
    """把多轮检索结果融合成最终 6 条 + 可解释 trace。

    rounds: [{"query": str, "hits": list[dict], "actor": str|None}]
    规则（与改造前逐字节一致）：
      1. 每轮前 2 名无条件入选（防高分短查询压掉长尾条文）；
      2. 其余按分数补足到 6 条，低于 MIN_ACCEPT_SCORE 的丢弃；
      3. trace 的 last 标在最后一轮。
    """
    merged: dict[str, dict] = {}
    guaranteed: list[str] = []
    trace: list[dict] = []

    for i, row in enumerate(rounds, 1):
        hits = row.get("hits") or []
        before = len(merged)
        for h in hits:
            key = _key(h)
            if key not in merged or h.get("score", 0) > merged.get(key, {}).get("score", 0):
                merged[key] = h
        for h in hits[:2]:
            key = _key(h)
            if key not in guaranteed:
                guaranteed.append(key)
        item = {
            "round": i,
            "query": row.get("query", ""),
            "top1": round(hits[0]["score"], 4) if hits else 0.0,
            "hits": len(hits),
            "added": len(merged) - before,
            "merged": len(merged),
            "last": False,
        }
        if row.get("actor"):
            item["actor"] = row["actor"]
        trace.append(item)

    picked: list[dict] = [
        merged[k] for k in guaranteed
        if k in merged and merged[k].get("score", 0) >= MIN_ACCEPT_SCORE
    ]
    picked_keys = {h.get("cite") for h in picked}
    rest = sorted(
        (h for k, h in merged.items()
         if k not in guaranteed and h.get("score", 0) >= MIN_ACCEPT_SCORE),
        key=lambda x: x.get("score", 0), reverse=True,
    )
    for h in rest:
        if len(picked) >= 6:
            break
        if h.get("cite") not in picked_keys:
            picked.append(h)
            picked_keys.add(h.get("cite"))

    if trace:
        trace[-1]["last"] = True
    return picked[:6], trace
