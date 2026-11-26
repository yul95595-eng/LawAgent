"""最小评测中台 —— 批量跑分器。

用法：
  # 1) 对运行中的服务跑真实模型（容器或本地 uvicorn）
  python scripts/eval_run.py --base-url http://127.0.0.1:9089

  # 2) 进程内 Mock 模式，秒级跑完，用于 CI / 改完代码自检
  python scripts/eval_run.py --inproc

  # 3) 只跑某一组，或限定条数
  python scripts/eval_run.py --only contract --limit 3

产物：evals/runs/<时间戳>-<mode>.json（逐题明细 + 汇总指标），供 eval_diff.py 对比。

判分口径（全部确定性，不引入 LLM 裁判）：
  引用命中   期望条文是否出现在 card.citations / applicable_law
  引用合法   返回的每条引用是否能在本地条文库查到（避免编造）
  要素命中   关键结论词是否出现（如「违法解除」「二倍工资」）
  案由一致   result.case_type 与素材标注是否一致
  类案命中   检索到的判例里是否有对应案由
  越界拒答   越界提问必须给出边界说明，且不得出现「第X条」式引用
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LAW_PREFIX = "中华人民共和国"
CITE_RE = re.compile(r"第[一二三四五六七八九十百零0-9]{1,7}条")


def norm(s: str) -> str:
    s = str(s or "")
    for ch in ("《", "》", " ", "\u3000", "\n"):
        s = s.replace(ch, "")
    return s.replace(LAW_PREFIX, "")


def card_of(payload: dict) -> dict:
    """统一取出「结果卡」：直连接口返回 {card, summary}，SSE result 事件本身就是卡片。"""
    if isinstance(payload.get("card"), dict):
        card = payload["card"]
    else:
        card = payload
    card = dict(card)
    card.setdefault("citations", [])
    card.setdefault("result", {})
    if not card.get("summary"):
        card["summary"] = payload.get("summary", "")
    return card


def cites_of(card: dict) -> list[str]:
    out: list[str] = []
    for c in card.get("citations") or []:
        if isinstance(c, dict):
            out.append(norm(f"{c.get('law', '')}{c.get('article_no', '')}"))
        else:
            out.append(norm(c))
    for item in (card.get("result") or {}).get("applicable_law") or []:
        if isinstance(item, dict):
            out.append(norm(item.get("cite", "")))
    return [c for c in dict.fromkeys(out) if c]


def check_case(case: dict, card: dict, citations_valid_check) -> list[dict]:
    """跑一条用例的全部断言，返回逐项结果。"""
    exp = case.get("expect") or {}
    dump = json.dumps(card, ensure_ascii=False)
    res = card.get("result") or {}
    summary = card.get("summary") or ""
    cites = cites_of(card)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for kw in exp.get("must_include", []):
        add(f"要素:{kw}", kw in dump, "" if kw in dump else f"未出现「{kw}」")

    for kw in exp.get("must_not_include", []):
        add(f"禁用:{kw}", kw not in dump, "" if kw not in dump else f"出现了不应出现的「{kw}」")

    if exp.get("case_type"):
        got = res.get("case_type")
        add("案由一致", got == exp["case_type"], f"期望 {exp['case_type']}，实际 {got}")

    if exp.get("citations_any"):
        hit = [e for e in exp["citations_any"] if any(norm(e) in c or c in norm(e) for c in cites)]
        add("期望引用命中", bool(hit), f"期望其一 {exp['citations_any']}；实际 {cites[:4]}")

    if exp.get("min_citations") is not None:
        add("引用数量", len(cites) >= exp["min_citations"], f"要求 ≥{exp['min_citations']}，实际 {len(cites)}")

    if exp.get("citation_all_valid"):
        bad = [c for c in cites if not citations_valid_check(c)]
        add("引用全部可对库", not bad, f"库中查不到：{bad[:3]}" if bad else "")

    if exp.get("precedent_case_type"):
        types = [p.get("case_type") for p in (res.get("precedents") or []) if isinstance(p, dict)]
        add("类案案由命中", exp["precedent_case_type"] in types,
            f"检索到 {types[:4]}")

    if exp.get("risk_level_any"):
        got = res.get("risk_level")
        add("风险等级", got in exp["risk_level_any"], f"期望 {exp['risk_level_any']}，实际 {got}")

    if exp.get("min_risk_points") is not None:
        n = len(res.get("risk_points") or [])
        add("风险点数量", n >= exp["min_risk_points"], f"要求 ≥{exp['min_risk_points']}，实际 {n}")

    if exp.get("refuse_keywords_any"):
        hit = [k for k in exp["refuse_keywords_any"] if k in summary]
        add("越界说明", bool(hit), f"摘要未含边界话术：{summary[:80]}")

    if exp.get("forbid_citation"):
        has_cite = bool(cites) or bool(CITE_RE.search(summary))
        add("未编造条文", not has_cite, f"出现引用：{cites[:3]}" if has_cite else "")

    return checks


def parse_sse(text: str) -> list[dict]:
    """把 SSE 文本切成事件列表：[{event, data}]。"""
    events: list[dict] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        ev, data_lines = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            events.append({"event": ev, "data": json.loads(raw)})
        except json.JSONDecodeError:
            events.append({"event": ev, "data": {"raw": raw}})
    return events


async def run(args) -> int:
    cases_path = Path(args.cases)
    spec = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = spec["cases"]
    if args.only:
        keys = {k.strip() for k in args.only.split(",")}
        cases = [c for c in cases if c["group"] in keys or c["id"] in keys]
    if args.limit:
        cases = cases[: args.limit]

    import httpx

    close_client = None
    if args.inproc:
        os.environ.setdefault("USE_SQLITE", "1")
        os.environ.setdefault("LLM_MOCK", "1")
        os.environ.setdefault("LAWAGENT_SKIP_SEED", "1")
        db = Path(os.environ.get("SQLITE_PATH") or (Path(os.environ["TEMP"]) / "lawagent_eval.db"))
        os.environ["SQLITE_PATH"] = str(db)
        if db.exists():
            db.unlink()

        from backend.memory.models import _seed_demo_users, init_db

        await init_db()
        await _seed_demo_users()
        from backend.api.app import app

        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval")
        close_client = client.aclose
        target, mode = "inproc", "mock"
    else:
        client = httpx.AsyncClient(base_url=args.base_url, timeout=180)
        close_client = client.aclose
        target, mode = args.base_url, "real"

    from backend.retrieval.statutes import citation_exists, extract_citations

    def cite_valid(normed: str) -> bool:
        parsed = extract_citations(normed)
        if parsed:
            law, art = parsed[0]
            return bool(citation_exists(law, art))
        return False

    pwd = os.getenv("DEMO_PASSWORD") or os.getenv("LAWAGENT_DEMO_PASSWORD") or "law123456"
    tokens: dict[str, str] = {}
    for user_no in ("L1001", "L3001", "P2001"):
        r = await client.post("/api/v1/auth/login", json={"user_no": user_no, "password": pwd})
        if r.status_code == 200:
            tokens[user_no] = r.json()["token"]
        else:
            print(f"[warn] 登录失败 {user_no}: {r.status_code} {r.text[:120]}")

    results: list[dict] = []
    started = time.time()
    for case in cases:
        role_user = {"lawyer": "L1001", "legal": "L3001", "partner": "P2001"}[case.get("role", "lawyer")]
        token = tokens.get(role_user)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        t0 = time.time()
        status, payload, err = 0, {}, ""
        try:
            if case["endpoint"].endswith("/chat/stream"):
                async with client.stream("POST", case["endpoint"], json=case["input"],
                                         headers={**headers, "Accept": "text/event-stream"}) as resp:
                    status = resp.status_code
                    body = "".join([chunk async for chunk in resp.aiter_text()])
                events = parse_sse(body)
                payload = next((e["data"] for e in reversed(events) if e["event"] == "result"), {})
            else:
                resp = await client.post(case["endpoint"], json=case["input"], headers=headers)
                status = resp.status_code
                payload = resp.json() if resp.content else {}
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        cost = int((time.time() - t0) * 1000)

        card = card_of(payload) if payload else {}
        checks = check_case(case, card, cite_valid) if status == 200 else [
            {"name": "接口可用", "ok": False, "detail": err or f"HTTP {status}"}
        ]
        passed = bool(checks) and all(c["ok"] for c in checks)
        print(f"  {'PASS' if passed else 'FAIL'}  {case['id']:<9} {cost:>6}ms  "
              f"{'' if passed else '; '.join(c['name'] for c in checks if not c['ok'])}")
        results.append({
            "id": case["id"], "group": case["group"], "endpoint": case["endpoint"],
            "passed": passed, "status": status, "latency_ms": cost,
            "checks": checks, "citations": cites_of(card),
            "summary": (card.get("summary") or "")[:300],
        })
    if close_client:
        await close_client()

    total = len(results)
    passed_n = sum(1 for r in results if r["passed"])
    lat = sorted(r["latency_ms"] for r in results)
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0
    cite_total = sum(len([c for c in r["checks"] if c["name"] in ("期望引用命中", "引用数量", "引用全部可对库")])
                     for r in results)
    cite_hit = sum(len([c for c in r["checks"] if c["ok"] and c["name"] in
                        ("期望引用命中", "引用数量", "引用全部可对库")]) for r in results)
    by_group: dict[str, dict] = {}
    for r in results:
        g = by_group.setdefault(r["group"], {"total": 0, "passed": 0})
        g["total"] += 1
        g["passed"] += int(r["passed"])
    for g in by_group.values():
        g["pass_rate"] = round(g["passed"] / g["total"], 4) if g["total"] else 0.0

    summary = {
        "total": total,
        "passed": passed_n,
        "pass_rate": round(passed_n / total, 4) if total else 0.0,
        "checks_total": sum(len(r["checks"]) for r in results),
        "checks_passed": sum(sum(1 for c in r["checks"] if c["ok"]) for r in results),
        "citation_check_total": cite_total,
        "citation_check_hit": cite_hit,
        "citation_accuracy": round(cite_hit / cite_total, 4) if cite_total else None,
        "avg_latency_ms": int(statistics.mean(lat)) if lat else 0,
        "p95_latency_ms": p95,
        "total_seconds": round(time.time() - started, 1),
        "by_group": by_group,
    }
    run = {
        "meta": {
            "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "mode": mode, "target": target,
            "cases_file": str(cases_path.relative_to(ROOT)) if cases_path.is_relative_to(ROOT) else str(cases_path),
            "cases_version": (spec.get("meta") or {}).get("version"),
            "tag": args.tag or "",
            "model": os.getenv("DASHSCOPE_MODEL", "qwen-plus") if mode == "real" else "mock",
        },
        "summary": summary,
        "results": results,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{run['meta']['run_id']}-{mode}.json"
    out_file.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 66)
    print(f"用例 {passed_n}/{total} 通过（{summary['pass_rate']:.1%}）  "
          f"断言 {summary['checks_passed']}/{summary['checks_total']}  "
          f"引用准确率 {summary['citation_accuracy'] if summary['citation_accuracy'] is not None else '-'}  "
          f"均值 {summary['avg_latency_ms']}ms / P95 {summary['p95_latency_ms']}ms  "
          f"耗时 {summary['total_seconds']}s")
    for g, v in by_group.items():
        print(f"  {g:<14} {v['passed']}/{v['total']}  {v['pass_rate']:.0%}")
    print(f"结果已写入 {out_file}")

    if args.fail_under is not None and summary["pass_rate"] < args.fail_under:
        print(f"[gate] 通过率 {summary['pass_rate']:.1%} < 阈值 {args.fail_under:.0%}")
        return 1
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="LawAgent 批量评测")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases_v1.json"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "runs"))
    ap.add_argument("--base-url", default="http://127.0.0.1:9089")
    ap.add_argument("--inproc", action="store_true", help="进程内 Mock 模式（不依赖任何服务）")
    ap.add_argument("--only", default="", help="只跑指定 group 或 id，逗号分隔")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--fail-under", type=float, default=None, help="通过率低于该值则退出码 1")
    raise SystemExit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
