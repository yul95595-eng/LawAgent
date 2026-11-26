"""两次跑分对比 —— 回答「这次改动是变好还是变坏」。

用法：
  python scripts/eval_diff.py                       # 最近两次
  python scripts/eval_diff.py a.json b.json         # 指定两次
  python scripts/eval_diff.py --last 3              # 看最近三次的趋势

重点关注三件事：
  1) 哪几题从过变挂（回归）—— 这是最需要立刻处理的
  2) 哪几题从挂变过（修复）
  3) 聚合指标怎么走（通过率 / 引用准确率 / P95 时延）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "evals" / "runs"


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def brief(run: dict) -> str:
    s, m = run["summary"], run["meta"]
    return (f"{m['run_id']} [{m['mode']}] 通过 {s['passed']}/{s['total']} "
            f"({s['pass_rate']:.1%}) 引用 {s['citation_accuracy']} "
            f"P95 {s['p95_latency_ms']}ms")


def diff(old: dict, new: dict) -> int:
    print(f"基线  {brief(old)}")
    print(f"本次  {brief(new)}")
    os_, ns = old["summary"], new["summary"]
    print("-" * 66)
    print(f"通过率      {os_['pass_rate']:.1%} → {ns['pass_rate']:.1%} "
          f"({ns['pass_rate'] - os_['pass_rate']:+.1%})")
    if os_.get("citation_accuracy") is not None and ns.get("citation_accuracy") is not None:
        print(f"引用准确率  {os_['citation_accuracy']:.1%} → {ns['citation_accuracy']:.1%} "
              f"({ns['citation_accuracy'] - os_['citation_accuracy']:+.1%})")
    print(f"P95 时延    {os_['p95_latency_ms']}ms → {ns['p95_latency_ms']}ms "
          f"({ns['p95_latency_ms'] - os_['p95_latency_ms']:+d}ms)")

    old_map = {r["id"]: r for r in old["results"]}
    new_map = {r["id"]: r for r in new["results"]}
    regressed = [i for i in new_map if i in old_map and old_map[i]["passed"] and not new_map[i]["passed"]]
    fixed = [i for i in new_map if i in old_map and not old_map[i]["passed"] and new_map[i]["passed"]]
    added = [i for i in new_map if i not in old_map]
    removed = [i for i in old_map if i not in new_map]

    print("-" * 66)
    if regressed:
        print(f"回归 {len(regressed)} 题（必须解释）：")
        for i in regressed:
            failed = [c["name"] for c in new_map[i]["checks"] if not c["ok"]]
            print(f"  - {i}: {', '.join(failed)}")
    else:
        print("回归 0 题")
    if fixed:
        print(f"修复 {len(fixed)} 题：{', '.join(fixed)}")
    if added:
        print(f"新增 {len(added)} 题：{', '.join(added)}")
    if removed:
        print(f"移除 {len(removed)} 题：{', '.join(removed)}")

    by_group = {}
    for g in {**old["summary"].get("by_group", {}), **new["summary"].get("by_group", {})}:
        o = old["summary"].get("by_group", {}).get(g, {}).get("pass_rate")
        n = new["summary"].get("by_group", {}).get(g, {}).get("pass_rate")
        by_group[g] = (o, n)
    print("-" * 66)
    for g, (o, n) in by_group.items():
        o_s = f"{o:.0%}" if o is not None else "-"
        n_s = f"{n:.0%}" if n is not None else "-"
        print(f"  {g:<14} {o_s:>5} → {n_s:>5}")
    return 1 if regressed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="对比两次评测结果")
    ap.add_argument("files", nargs="*", help="两个 run json；缺省取最近两次")
    ap.add_argument("--last", type=int, default=0, help="按时间顺序列出最近 N 次的指标")
    args = ap.parse_args()

    files = [Path(f) for f in args.files] if args.files else sorted(RUNS.glob("*.json"))
    if len(files) < 2 and not args.last:
        print("至少需要两次跑分结果，先跑 `python scripts/eval_run.py --inproc` 两次")
        raise SystemExit(2)

    if args.last:
        for f in files[-args.last:]:
            run = load(f)
            s = run["summary"]
            print(f"{run['meta']['ts']}  {brief(run)}  断言 {s['checks_passed']}/{s['checks_total']}")
        return

    raise SystemExit(diff(load(files[-2]), load(files[-1])))


if __name__ == "__main__":
    main()
