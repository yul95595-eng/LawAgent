"""生成《四类 Agent 产出总览》一页纸 PDF（A4 横向）。

用途：面试/评审时一页讲清四个 Agent 各自交付什么、落在哪张表。
依赖：reportlab（仅文档生成用，不进运行时依赖）。
运行：
  python scripts/make_agent_output_pdf.py
输出：docs/LawAgent-四类Agent产出总览.pdf
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "LawAgent-四类Agent产出总览.pdf"

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
try:
    pdfmetrics.registerFont(TTFont("SimHei", "C:/Windows/Fonts/simhei.ttf"))
    HEAD_FONT = "SimHei"
except Exception:  # noqa: BLE001
    HEAD_FONT = "STSong-Light"

INK = colors.HexColor("#1F2328")
MUTED = colors.HexColor("#5F5E5A")
BLUE = colors.HexColor("#185FA5")
BLUE_BG = colors.HexColor("#E6F1FB")
GRAY_BG = colors.HexColor("#F1EFE8")
AMBER_BG = colors.HexColor("#FAEEDA")
GREEN_BG = colors.HexColor("#EAF3DE")
LINE = colors.HexColor("#D3D1C7")

TITLE = ParagraphStyle("title", fontName=HEAD_FONT, fontSize=19, leading=24, textColor=INK)
SUB = ParagraphStyle("sub", fontName="STSong-Light", fontSize=10.5, leading=15, textColor=MUTED)
CELL = ParagraphStyle("cell", fontName="STSong-Light", fontSize=9.6, leading=14, textColor=INK)
CELL_S = ParagraphStyle("cells", fontName="STSong-Light", fontSize=8.7, leading=12, textColor=MUTED)
HEAD = ParagraphStyle("head", fontName=HEAD_FONT, fontSize=9.5, leading=13, textColor=BLUE)
NAME = ParagraphStyle("name", fontName=HEAD_FONT, fontSize=11.5, leading=15, textColor=INK)
BOX = ParagraphStyle("box", fontName="STSong-Light", fontSize=9, leading=13, textColor=INK)
BOXT = ParagraphStyle("boxt", fontName=HEAD_FONT, fontSize=9, leading=12, textColor=BLUE)


def P(text: str, style=CELL) -> Paragraph:
    return Paragraph(text, style)


ROWS = [
    ("案件分析", "case_analysis<br/>precedent_search",
     "适用法条 6 条 · 类案 3 条 · 争议焦点 3 个<br/>时效结论 · 胜诉率 + 理由 · 分析框架 + 证据清单",
     "「案件分析：<案由>」卡片<br/>赔偿金额 / 焦点 / 条文出处 / 类案 / 风险提示",
     "case_analyses", GREEN_BG),
    ("合同审查", "contract_review<br/>clause_analysis",
     "条款清单 · 逐条风险（高/中/低）· 格式校验<br/>风险等级 · 审查意见书 · 审核状态",
     "「合同审查」卡片<br/>风险清单 + 意见书；高风险挂起等合伙人签字",
     "contract_reviews", GREEN_BG),
    ("企业合规", "compliance_check<br/>legal_opinion",
     "工商档案 · 经营范围核对 · 资质缺口清单<br/>引用核验明细 · 合规报告 + 风险评分",
     "「合规体检」卡片<br/>企业档案 / 缺什么资质 / 体检分 / 出处",
     "enterprise_compliance_profiles", GREEN_BG),
    ("时效咨询", "limitation_inquiry",
     "时效结论（劳动 1 年 / 民事 3 年）<br/>依据条文 · 注意事项",
     "「时效答复」卡片<br/>一句话结论 + 条文出处（轻咨询，不进主链）",
     "不落库", AMBER_BG),
]

BOXES = [
    ("标题", "案件分析：<案由><br/>信息不足时改为「需补充信息」"),
    ("摘要", "案由、时效、赔偿公式<br/>2N = 3 × 8000 × 2 = 48000 元"),
    ("result", "案由 / 事实 / 法条 / 类案 / 焦点<br/>时效 / 胜诉率 / 框架 / case_id"),
    ("citations", "6 条引用：法律名 + 条号 + 原文<br/>点击可核验；追问卡时为空"),
    ("audit_hint", "「AI 辅助分析<br/>结论需承办律师复核后使用」"),
]

TRACE_BOXES = [
    ("引用", "每条结论都挂条文：<br/>法律名 + 条号 + 原文，回库比对过"),
    ("轨迹", "retrieval_trace：每轮 query、top1 分、<br/>新增几条，以及 actor（策略/模型/降级）"),
    ("边界", "law_gap_note：库里没有就明说；<br/>audit_hint：结论需律师复核"),
]


def main() -> None:
    doc = SimpleDocTemplate(
        str(OUT), pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=11 * mm, bottomMargin=9 * mm,
        title="LawAgent 四类 Agent 产出总览", author="LawAgent",
    )
    width = doc.width
    story = [
        P("四类 Agent 产出总览", TITLE),
        Spacer(1, 3),
        P("同一套编排，四种交付物：<b>办案底稿</b> · <b>审查意见</b> · <b>体检报告</b> · <b>时效答复</b>；"
          "区别在「要不要人签字」「要不要落库」。", SUB),
        Spacer(1, 8),
    ]

    head = [P("Agent", HEAD), P("触发意图", HEAD), P("核心产出（内部 state）", HEAD),
            P("用户看到什么", HEAD), P("落库", HEAD)]
    data = [head]
    for name, intents, outputs, seen, table, bg in ROWS:
        data.append([P(name, NAME), P(intents, CELL_S), P(outputs, CELL),
                     P(seen, CELL), P(table, CELL_S)])
    t = Table(data, colWidths=[width * 0.10, width * 0.14, width * 0.30, width * 0.29, width * 0.17],
              repeatRows=1)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), BLUE_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, BLUE),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for i, row in enumerate(ROWS, 1):
        style.append(("BACKGROUND", (4, i), (4, i), row[5]))
    t.setStyle(TableStyle(style))
    story += [t, Spacer(1, 10)]

    story.append(P("案件分析卡片：五段式", HEAD))
    story.append(Spacer(1, 4))
    cells = []
    for title, body in BOXES:
        cells.append([P(title, BOXT), Spacer(1, 1), P(body, BOX)])
    bt = Table([cells], colWidths=[width / 5 - 4] * 5)
    bt.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [bt, Spacer(1, 12)]

    story.append(P("卡片之外，每条结论还带三样东西", HEAD))
    story.append(Spacer(1, 4))
    tb = Table([[[P(a, BOXT), Spacer(1, 1), P(b, BOX)] for a, b in TRACE_BOXES]],
               colWidths=[width / 3 - 4] * 3)
    tb.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), BLUE_BG),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [tb, Spacer(1, 12)]

    story.append(P(
        "一句话记住：案件分析交底稿，合同审查交意见，合规交报告，时效交答复。"
        "要不要人签字、要不要落库，决定它属于哪一种。", SUB))
    story.append(Spacer(1, 10))

    foot = Table([[P("28 个节点（案件 12 +1 追问 / 合同 8 / 合规 6 / 时效 1）", CELL_S),
                   P("18 个工具（规则 4 / LLM 5 / 检索 4 / 业务 API 5）", CELL_S),
                   P("30 题评测 90% · 案件组 10/10 · 引用准确率 100%", CELL_S)]],
                 colWidths=[width / 3] * 3)
    foot.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GRAY_BG),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(foot)

    doc.build(story)
    print(f"已生成：{OUT}")


if __name__ == "__main__":
    main()
