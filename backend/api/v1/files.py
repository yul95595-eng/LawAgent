"""文件上传解析 —— docx / pdf / txt / md → 纯文本。

前端把解析出的正文放进消息 attachments（contract_text / case_text），
复用既有 SSE 审查链路，后端 Agent 零改动。仅解析、不落盘：
内存中抽取文本后立即返回，避免临时文件清理与敏感材料留痕。
"""
from __future__ import annotations

import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from backend.api.deps import get_current_user

router = APIRouter(prefix="/files", tags=["files"])

MAX_BYTES = 10 * 1024 * 1024  # 10MB
ALLOWED_SUFFIX = {".txt", ".md", ".docx", ".pdf"}


def _decode_text(raw: bytes) -> str:
    """txt/md：UTF-8 优先，退 GB18030（Windows 常见中文编码）。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_docx(raw: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(raw))
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:  # 合同常用表格排版（甲乙方信息等）
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append("　|　".join(cells))
    return "\n".join(lines)


def _parse_pdf(raw: bytes) -> str:
    import fitz  # PyMuPDF

    with fitz.open(stream=raw, filetype="pdf") as pdf:
        return "\n".join(page.get_text("text") for page in pdf)


@router.post("/parse")
async def parse_file(file: UploadFile, user: dict = Depends(get_current_user)):
    """上传文件 → 抽取纯文本（不落盘）。前端按语义放进 attachments 走审查链路。"""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise HTTPException(status_code=415, detail=f"不支持的文件类型 {suffix or '(无后缀)'}，仅支持 txt/md/docx/pdf")

    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 10MB 上限")
    if not raw:
        raise HTTPException(status_code=422, detail="文件为空")

    try:
        if suffix == ".docx":
            text = _parse_docx(raw)
        elif suffix == ".pdf":
            text = _parse_pdf(raw)
        else:
            text = _decode_text(raw)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"文件解析失败：{e}") from e

    text = text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="未能从文件中解析出文本（可能是扫描件/图片型 PDF）")
    return {"filename": filename, "suffix": suffix, "chars": len(text), "text": text}
