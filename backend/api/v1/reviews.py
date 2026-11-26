"""合伙人审核路由 —— HitL 待审列表 + 确认/退回（Command resume 恢复图执行）+ 意见书导出 Word。"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from backend.api.audit import audit
from backend.api.deps import get_current_user, require_role
from backend.memory.models import ChatThread, ContractReview, SessionLocal, ThreadMessage
from backend.orchestrator.orchestrator import _AGENTS, _config, build_result_card

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewDecision(BaseModel):
    comment: str = ""


@router.get("/pending")
async def pending_reviews(user: dict = Depends(require_role("partner", "admin", "legal"))):
    """待审意见书列表（合伙人首页右侧面板数据源）。"""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ContractReview)
            .where(ContractReview.status == "pending_review")
            .order_by(ContractReview.created_at.desc())
            .limit(50)
        )).scalars().all()
        return {"reviews": [
            {
                "review_id": r.review_id,
                "thread_id": r.thread_id,
                "contract_title": r.contract_title,
                "risk_level": r.risk_level,
                "risk_points": json.loads(r.risks_json or "[]"),
                "review_opinion": r.review_opinion,
                "submitted_by": r.user_id,
                "created_at": r.created_at,
            } for r in rows
        ]}


async def _resume_review(review_id: str, decision: str, reviewer_id: str, comment: str) -> dict:
    """从 Checkpointer 恢复被 interrupt 的合同审查图。"""
    async with SessionLocal() as session:
        row = await session.get(ContractReview, review_id)
        if row is None or row.status != "pending_review":
            raise HTTPException(status_code=404, detail="待审记录不存在或已处理")
        thread_id = row.thread_id
        tenant_id, owner_id = row.tenant_id, row.user_id

    agent_name = "contract"
    final = await _AGENTS[agent_name].ainvoke(
        Command_resume(decision, reviewer_id, comment), config=_config(thread_id)
    )
    # submit_result 节点已更新记录状态；这里同步审核人信息（幂等兜底）
    async with SessionLocal() as session:
        row = await session.get(ContractReview, review_id)
        if row:
            row.reviewer_id = reviewer_id
            # 退回后图会重写意见并再次挂起在 partner_review —— 记录保持
            # pending_review，待审列表仍可见，等待下一轮确认/退回；
            # 只有确认才把状态推进到 approved（submit_result 已同步写入）。
            if decision == "confirmed":
                row.status = "approved"
            row.review_comment = comment or row.review_comment
            await session.commit()
        thread = await session.get(ChatThread, thread_id)
        if thread and decision == "confirmed":
            thread.status = "done"
            await session.commit()
    card = build_result_card("contract", "contract_review", final)
    await audit(tenant_id, reviewer_id, "partner",
                "review_confirm" if decision == "confirmed" else "review_reject",
                "contract_review", review_id, {"comment": comment})
    # 给提交律师的线程里补一条审核结论消息
    async with SessionLocal() as session:
        session.add(ThreadMessage(
            id=f"msg_{review_id}_{decision}",
            thread_id=thread_id, role="assistant",
            content=(f"合伙人已{'确认' if decision == 'confirmed' else '退回'}审查意见：{comment or '无批注'}"),
            result_json=json.dumps(card, ensure_ascii=False),
        ))
        await session.commit()
    return {"review_id": review_id, "decision": decision, "card": card,
            "summary": final.get("summary", "")}


def Command_resume(decision: str, reviewer_id: str, comment: str) -> dict:
    from langgraph.types import Command

    return Command(resume={"decision": decision, "reviewer_id": reviewer_id, "comment": comment})


@router.post("/{review_id}/confirm")
async def confirm_review(review_id: str, body: ReviewDecision,
                         user: dict = Depends(require_role("partner", "admin"))):
    """合伙人确认 → 图恢复 → submit_result（approved）。"""
    return await _resume_review(review_id, "confirmed", user["user_id"], body.comment)


@router.post("/{review_id}/reject")
async def reject_review(review_id: str, body: ReviewDecision,
                        user: dict = Depends(require_role("partner", "admin"))):
    """合伙人退回 → 图回到 generate_review_opinion 重写意见。"""
    return await _resume_review(review_id, "returned", user["user_id"], body.comment)


# ── 意见书导出 Word（python-docx 内存生成，不落盘）─────────────

_LEVEL_CN = {"high": "高", "medium": "中", "low": "低"}


def _build_review_docx(row: ContractReview) -> BytesIO:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(11)

    title = doc.add_heading("合同审查意见书", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    risk_points = json.loads(row.risks_json or "[]")
    status_cn = {"pending_review": "待合伙人终审", "approved": "合伙人已确认"}.get(row.status, row.status)

    meta = doc.add_table(rows=0, cols=2)
    meta.style = "Table Grid"
    for k, v in (
        ("合同名称", row.contract_title or "未命名合同"),
        ("整体风险等级", _LEVEL_CN.get(row.risk_level, row.risk_level or "-")),
        ("审核状态", status_cn),
        ("提交人", row.user_id or "-"),
        ("审核人", row.reviewer_id or "（待审核）"),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ):
        cells = meta.add_row().cells
        cells[0].text = k
        cells[1].text = str(v)
        cells[0].paragraphs[0].runs[0].bold = True

    doc.add_heading("一、风险点清单", level=1)
    if risk_points:
        table = doc.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        head = table.rows[0].cells
        for i, h in enumerate(("风险点", "等级", "事实/理由", "法律依据", "修改建议")):
            head[i].text = h
            head[i].paragraphs[0].runs[0].bold = True
        for rp in risk_points:
            cells = table.add_row().cells
            cells[0].text = str(rp.get("risk_name", ""))
            cells[1].text = _LEVEL_CN.get(rp.get("level", ""), rp.get("level", "-"))
            cells[2].text = str(rp.get("reason", ""))
            cells[3].text = str(rp.get("basis", ""))
            cells[4].text = str(rp.get("suggestion", ""))
    else:
        doc.add_paragraph("未检测到风险点。")

    doc.add_heading("二、审查意见", level=1)
    for para in (row.review_opinion or "无").split("\n"):
        if para.strip():
            doc.add_paragraph(para)

    doc.add_heading("三、合伙人批注", level=1)
    doc.add_paragraph(row.review_comment or "无")

    tip = doc.add_paragraph()
    run = tip.add_run("免责声明：本意见书由 LawAgent 智能法律咨询系统辅助生成，须经承办律师 / 合伙人复核确认后方可对外使用。")
    run.italic = True
    run.font.size = Pt(9)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


@router.get("/{review_id}/export")
async def export_review_docx(review_id: str, user: dict = Depends(get_current_user)):
    """意见书导出 Word：提交人本人或合伙人/法务/管理员可下载。"""
    async with SessionLocal() as session:
        row = await session.get(ContractReview, review_id)
        if row is None or row.tenant_id != user["tenant_id"]:
            raise HTTPException(status_code=404, detail="审查记录不存在")
        if row.user_id != user["user_id"] and user["role"] not in ("partner", "admin", "legal"):
            raise HTTPException(status_code=403, detail="无权导出他人意见书")

    buf = _build_review_docx(row)
    filename = f"合同审查意见书_{row.contract_title or '未命名合同'}_{datetime.now():%Y%m%d}.docx"
    quoted = urllib.parse.quote(filename)
    await audit(user["tenant_id"], user["user_id"], user["role"], "export", "contract_review", review_id)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )
