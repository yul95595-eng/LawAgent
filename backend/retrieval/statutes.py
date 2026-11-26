"""法条知识库 —— 真实法律条文的结构化加载与检索。

数据源：data/legal/statutes.json（真实现行法律条文，每条含法律名称、
条文号、条文原文、关键词条目）。用途：
1. search_applicable_law / search_law_knowledge_base（MCP 工具）的检索底座；
2. verify_citation_accuracy 的「对照源」—— 法条引用准确性校验就是拿
   模型/文书里引用的条文号回条文库查原文比对，杜绝对不上号的「法条幻觉」；
3. 合同审查风险点的法条依据回填（宁缺勿假：库内查不到就标「待人工核对」）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

from backend.core.config import LEGAL_DATA_DIR
from backend.retrieval.vectorstore import LocalTfidfStore, VectorDoc

STATUTES_PATH = LEGAL_DATA_DIR / "statutes.json"


@dataclass
class StatuteArticle:
    law: str            # 法律名称，如「中华人民共和国劳动合同法」
    article_no: str     # 条文号，如「第十九条」
    text: str           # 条文原文
    keywords: list[str]  # 检索关键词

    @property
    def cite(self) -> str:
        return f"{short_law(self.law)}{self.article_no}"


def short_law(law: str) -> str:
    """显示用简称：中华人民共和国劳动合同法 → 劳动合同法。"""
    return law.replace("中华人民共和国", "")


@lru_cache(maxsize=1)
def load_statutes() -> list[StatuteArticle]:
    """合并加载 data/legal/ 下全部 statutes*.json（主库 + 扩容库）。

    按 (law, article_no) 去重、先到先得：statutes.json 为主库优先；
    扩容文件只补充主库没有的条文（同一部法律的其余条文全量收录）。
    """
    articles: list[StatuteArticle] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(LEGAL_DATA_DIR.glob("statutes*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for law in data["laws"]:
            for art in law["articles"]:
                key = (law["law"], art["article_no"])
                if key in seen:
                    continue
                seen.add(key)
                articles.append(StatuteArticle(
                    law=law["law"],
                    article_no=art["article_no"],
                    text=art["text"],
                    keywords=art.get("keywords", []),
                ))
    return articles


@lru_cache(maxsize=1)
def _store() -> LocalTfidfStore:
    store = LocalTfidfStore()
    for i, art in enumerate(load_statutes()):
        store.add(VectorDoc(
            doc_id=f"law_{i:03d}",
            text=f"{art.law} {art.article_no} {' '.join(art.keywords)} {art.text}",
            metadata={"law": art.law, "article_no": art.article_no, "cite": art.cite},
        ))
    store.build()
    return store


def search_statutes(query: str, top_k: int = 5) -> list[dict]:
    """向量检索法条。返回 [{law, article_no, cite, text, score}]。"""
    hits = _store().search(query, top_k=top_k)
    out: list[dict] = []
    for h in hits:
        art = get_article(h["metadata"]["law"], h["metadata"]["article_no"])
        out.append({
            "law": h["metadata"]["law"],
            "article_no": h["metadata"]["article_no"],
            "cite": h["metadata"]["cite"],
            "text": art.text if art else h["text"],
            "score": h["score"],
        })
    return out


def get_article(law_hint: str, article_no: str) -> StatuteArticle | None:
    """精确取条文（verify_citation_accuracy 的对照源）。

    两轮匹配：先精确（法名全等/简称全等），再模糊包含——避免
    《劳动合同法》第六条 撞上《劳动合同法实施条例》第六条。
    """
    candidates: list[StatuteArticle] = []
    for art in load_statutes():
        same_law = (
            law_hint in art.law
            or short_law(art.law) in law_hint
            or law_hint.replace("中华人民共和国", "") in art.law
        )
        if same_law and art.article_no == article_no:
            candidates.append(art)
    for art in candidates:  # 第一轮：法名精确等于
        if art.law == law_hint or short_law(art.law) == law_hint:
            return art
    return candidates[0] if candidates else None


def citation_exists(law_hint: str, article_no: str) -> bool:
    """法条引用校验第一问：这条法条在库里存在吗。"""
    return get_article(law_hint, article_no) is not None


_CITE_RE = re.compile(
    r"(?:《([^》]{2,30}?)》|([\u4e00-\u9fff]{2,15}法))\s*第([一二三四五六七八九十百零\d]+)条"
)

# 无书名号写法常见的引导词前缀（「根据劳动合同法第二十三条」会连「根据」一起匹配到）
_LEAD_WORDS = ("根据", "依据", "按照", "依照", "援引", "参见", "适用", "以及", "和", "与")


def _clean_law_name(name: str) -> str:
    """剥离法名前的引导词与「中华人民共和国」前缀。"""
    name = name.replace("中华人民共和国", "").strip()
    changed = True
    while changed:
        changed = False
        for w in _LEAD_WORDS:
            if name.startswith(w) and len(name) - len(w) >= 3:
                name = name[len(w):]
                changed = True
    return name.strip()


def extract_citations(text: str) -> list[tuple[str, str]]:
    """从文本中抽取法条引用 (法律名, 条文号)。verify_citation_accuracy 用。

    兼容三种写法：《劳动合同法》第二十二条 / 中华人民共和国劳动合同法
    第二十二条 / 劳动合同法第二十二条。抽出的引用最终是否有效由
    get_article 回条文库核对（存在性 + 原文对照），不在正则层判真伪。
    """
    out: list[tuple[str, str]] = []
    for m in _CITE_RE.finditer(text):
        law = _clean_law_name(m.group(1) or m.group(2) or "")
        no = f"第{m.group(3)}条"
        if law and len(law) >= 3:
            out.append((law, no))
    return out


if __name__ == "__main__":
    arts = load_statutes()
    print(f"法条库：{len(set(a.law for a in arts))} 部法律 / {len(arts)} 条")
    for h in search_statutes("试用期超过法定上限", top_k=3):
        print(f"  [{h['score']:.3f}] {h['cite']}: {h['text'][:40]}...")
