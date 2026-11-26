"""评测门禁 —— 把「效果不许变差」变成一条会红的测试。

两层：
1) 评测集结构自检（永远执行）：id 唯一、endpoint 合法、expect 非空。
2) 指标门禁（有跑分结果时执行）：读 evals/runs 最近一次结果，低于阈值即失败。

跑分本身不在 pytest 里跑（真实模型一轮几分钟，且要外部服务），
CI 口径是：先 `python scripts/eval_run.py --inproc`，再 `pytest`。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases_v1.json"
RUNS = ROOT / "evals" / "runs"

# 门禁阈值：Mock 基线 80%，留 5 个点给波动；引用准确率是硬指标
MIN_PASS_RATE = 0.75
MIN_CITATION_ACCURACY = 0.95
KNOWN_ENDPOINTS = {
    "/api/v1/cases/analyze",
    "/api/v1/cases/faq",
    "/api/v1/contracts/review",
    "/api/v1/compliance/check",
    "/api/v1/chat/stream",
}


class TestEvalSetShape:
    """评测集本身的结构校验 —— 防止手改 JSON 时把题写坏。"""

    @pytest.fixture(scope="class")
    def spec(self):
        assert CASES.exists(), "evals/cases_v1.json 不存在"
        return json.loads(CASES.read_text(encoding="utf-8"))

    def test_ids_unique(self, spec):
        ids = [c["id"] for c in spec["cases"]]
        assert len(ids) == len(set(ids)), f"用例 id 重复：{ids}"

    def test_endpoints_known(self, spec):
        for c in spec["cases"]:
            assert c["endpoint"] in KNOWN_ENDPOINTS, f"{c['id']} 的接口不存在：{c['endpoint']}"

    def test_every_case_has_expectation(self, spec):
        for c in spec["cases"]:
            assert c.get("expect"), f"{c['id']} 没有判分口径，等于白跑"

    def test_input_not_empty(self, spec):
        for c in spec["cases"]:
            assert any(str(v).strip() for v in c["input"].values()), f"{c['id']} 输入为空"

    def test_groups_covered(self, spec):
        groups = {c["group"] for c in spec["cases"]}
        assert {"case_analysis", "limitation", "contract", "compliance", "boundary"} <= groups


class TestEvalGate:
    """最近一次跑分的指标门禁。没有跑分结果时跳过（不阻塞纯单测场景）。"""

    @pytest.fixture(scope="class")
    def latest_run(self):
        files = sorted(RUNS.glob("*.json")) if RUNS.exists() else []
        if not files:
            pytest.skip("还没有跑分结果：先运行 python scripts/eval_run.py --inproc")
        run = json.loads(files[-1].read_text(encoding="utf-8"))
        print(f"\n[gate] 使用跑分 {files[-1].name}（{run['meta']['mode']}）")
        return run

    def test_pass_rate(self, latest_run):
        rate = latest_run["summary"]["pass_rate"]
        assert rate >= MIN_PASS_RATE, (
            f"通过率 {rate:.1%} 低于门禁 {MIN_PASS_RATE:.0%}；"
            f"用 scripts/eval_diff.py 看哪几题退化"
        )

    def test_citation_accuracy(self, latest_run):
        acc = latest_run["summary"].get("citation_accuracy")
        if acc is None:
            pytest.skip("本次跑分没有引用类断言")
        assert acc >= MIN_CITATION_ACCURACY, f"引用准确率 {acc:.1%} 低于门禁 {MIN_CITATION_ACCURACY:.0%}"

    def test_boundary_all_refused(self, latest_run):
        """越界问题必须 100% 守住 —— 法律场景里编造条文是执业事故，不设容错。"""
        rows = [r for r in latest_run["results"] if r["group"] == "boundary"]
        if not rows:
            pytest.skip("本次跑分没包含越界用例")
        bad = [r["id"] for r in rows if not r["passed"]]
        assert not bad, f"越界用例未守住：{bad}"

    def test_no_regression_marker(self, latest_run):
        """接口层面：不允许有 5xx（400/401 属于用例配置问题，另说）。"""
        bad = [r["id"] for r in latest_run["results"] if r["status"] >= 500]
        assert not bad, f"这些用例把服务打挂了：{bad}"
