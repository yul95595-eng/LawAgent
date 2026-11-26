"""检索层测试 —— 法条文库 / 判例库 / 引用抽取与校验。"""
from __future__ import annotations

import pytest

from backend.retrieval.precedents import (
    CASE_TYPES,
    load_precedents,
    search_precedents,
    stats,
)
from backend.retrieval.statutes import (
    citation_exists,
    extract_citations,
    get_article,
    load_statutes,
    search_statutes,
)


pytestmark = pytest.mark.usefixtures("_prepare_db")


class TestStatutes:
    def test_library_shape(self):
        arts = load_statutes()
        assert len(arts) >= 30
        assert len({a.law for a in arts}) >= 8  # 12 部法律

    def test_get_article_exact(self):
        art = get_article("劳动合同法", "第十九条")
        assert art is not None
        assert "试用期" in art.text

    def test_get_article_with_full_name(self):
        art = get_article("中华人民共和国劳动合同法", "第八十七条")
        assert art is not None
        assert "二倍" in art.text

    def test_get_article_civil_code(self):
        art = get_article("民法典", "第一百八十八条")
        assert art is not None
        assert "三年" in art.text

    def test_citation_exists_negative(self):
        assert citation_exists("劳动合同法", "第九百九十九条") is False
        assert citation_exists("不存在的法", "第一条") is False

    def test_search_relevant(self):
        hits = search_statutes("违法解除劳动合同赔偿金 2N", top_k=3)
        assert hits
        assert all("score" in h for h in hits)


class TestExtractCitations:
    def test_book_title_style(self):
        cites = extract_citations("依据《劳动合同法》第二十二条及《民法典》第一百八十八条……")
        assert ("劳动合同法", "第二十二条") in cites
        assert ("民法典", "第一百八十八条") in cites

    def test_plain_style(self):
        cites = extract_citations("根据劳动合同法第二十三条第二款，用人单位可以约定竞业限制。")
        assert ("劳动合同法", "第二十三条") in cites

    def test_mixed_and_dedup_ready(self):
        cites = extract_citations("《劳动争议调解仲裁法》第二十七条、劳动合同法第二十五条")
        assert ("劳动争议调解仲裁法", "第二十七条") in cites
        assert ("劳动合同法", "第二十五条") in cites


class TestPrecedents:
    def test_all_case_types_covered(self):
        s = stats()
        assert s["total"] >= 60
        for ct in CASE_TYPES:
            assert s["by_case_type"].get(ct, 0) >= 1, f"案由 {ct} 无判例"

    def test_search_with_filter(self):
        for ct in CASE_TYPES:
            hits = search_precedents(load_precedents()[0]["topic"], case_type=ct, top_k=1)
            assert hits and hits[0]["case_type"] == ct, f"案由 {ct} 过滤失效"

    def test_real_case_no(self):
        hits = search_precedents("与目标公司对赌回购", case_type="contract_equity", top_k=3)
        assert hits
        assert any("民" in h["case_no"] or "提字" in h["case_no"] for h in hits)
