"""复核 Agent（P3，红队）—— 在落库前挑自己人的错。

三条规则（确定性、可测、零模型成本）：
  R1 过强表述：出现「必定胜诉 / 保证胜诉 / 100%胜诉」这类绝对化结论；
  R2 无据结论：一条法条都没召回，却仍在输出争议焦点或分析正文；
  R3 时效缺失：劳动类案件没有给出时效结论（中风险，不阻断）。

判定：
  high 级问题 ≥1 → reject（打回起草重写，最多 1 次）；
  只有 medium → warn（照常出，但在卡片上标注）；
  无问题 → pass。

打回上限 1 次仍不通过 → forced_pass，卡片标「存在争议，须人工复核」，
绝不无限重写 —— 法律场景宁可交人，也不让系统自己反复改到"看起来没问题"。
"""
from __future__ import annotations

_ABSOLUTE_CLAIMS = (
    "必定胜诉", "必胜", "保证胜诉", "100%胜诉", "百分之百胜诉",
    "一定能赢", "绝对能赢", "包赢", "稳赢",
)


def _corpus(state: dict) -> str:
    parts = [str((state.get("framework") or {}).get("text", ""))]
    for issue in state.get("legal_issues", []) or []:
        parts.append(str(issue.get("issue", "")))
        parts.append(str(issue.get("analysis", "")))
    return " ".join(parts)


def review_case_output(state: dict) -> tuple[list[dict], str]:
    """返回 (findings, verdict)。纯函数，便于单测与回归。"""
    findings: list[dict] = []
    text = _corpus(state)

    for kw in _ABSOLUTE_CLAIMS:
        if kw in text:
            findings.append({
                "rule": "overconfident",
                "level": "high",
                "detail": f"结论出现绝对化表述「{kw}」",
                "fix": "改为概率性表述（如「胜诉可能性较高」），并注明证据与法条依据",
            })

    if not state.get("applicable_law") and text.strip():
        if state.get("law_gap_note"):
            findings.append({
                "rule": "no_citation_declared",
                "level": "medium",
                "detail": "法条库未覆盖，已声明缺口但仍输出分析",
                "fix": "结论需注明「非法条依据」，并在卡片提示人工核对法规库",
            })
        else:
            findings.append({
                "rule": "no_citation",
                "level": "high",
                "detail": "未召回任何法条，但仍输出了分析结论",
                "fix": "补充检索或明确声明「法条库未覆盖」，不得输出无依据结论",
            })

    if str(state.get("case_type", "")).startswith("labor"):
        if not (state.get("limitation") or {}).get("period_text"):
            findings.append({
                "rule": "missing_limitation",
                "level": "medium",
                "detail": "劳动类案件未给出仲裁时效结论",
                "fix": "补充时效计算（1 年 / 拖欠工资特殊起算）",
            })

    verdict = "pass"
    if findings:
        verdict = "reject" if any(f["level"] == "high" for f in findings) else "warn"
    return findings, verdict
