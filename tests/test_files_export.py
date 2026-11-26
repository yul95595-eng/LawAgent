"""阶段1 核心真实化测试 —— 文件上传解析管道（docx/pdf/txt/md）+ 意见书导出 Word。"""
from __future__ import annotations

import io
import json

import httpx
import pytest

from backend.memory.models import ContractReview, SessionLocal

pytestmark = pytest.mark.usefixtures("_prepare_db")


async def _make_client() -> httpx.AsyncClient:
    from backend.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login(client: httpx.AsyncClient, user_no: str, password: str = "law123456") -> dict:
    r = await client.post("/api/v1/auth/login", json={"user_no": user_no, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


async def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestFileParse:
    async def test_parse_txt_utf8(self):
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("劳动合同.txt", "第一条 试用期：6 个月。".encode("utf-8"), "text/plain")},
                headers=await _headers(token),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["suffix"] == ".txt"
            assert "试用期" in body["text"]
            assert body["chars"] > 0

    async def test_parse_txt_gbk(self):
        """Windows 记事本常见的 GBK 编码也要能读。"""
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("案情.txt", "员工被违法辞退，要求赔偿".encode("gb18030"), "text/plain")},
                headers=await _headers(token),
            )
            assert r.status_code == 200
            assert "违法辞退" in r.json()["text"]

    async def test_parse_docx(self):
        from docx import Document

        doc = Document()
        doc.add_paragraph("劳动合同")
        doc.add_paragraph("第二条 试用期：试用期为 6 个月。")
        table = doc.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "甲方"
        table.rows[0].cells[1].text = "某某科技有限公司"
        buf = io.BytesIO()
        doc.save(buf)

        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("合同.docx", buf.getvalue(),
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                headers=await _headers(token),
            )
            assert r.status_code == 200, r.text
            text = r.json()["text"]
            assert "试用期为 6 个月" in text
            assert "某某科技有限公司" in text  # 表格内容也抽出来了

    async def test_parse_pdf(self):
        import fitz

        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "第七条 竞业限制：离职后5年内不得入职竞争企业。", fontname="china-s")
        pdf_bytes = pdf.tobytes()

        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("合同.pdf", pdf_bytes, "application/pdf")},
                headers=await _headers(token),
            )
            assert r.status_code == 200, r.text
            assert "竞业限制" in r.json()["text"]

    async def test_rejects_bad_suffix(self):
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("病毒.exe", b"MZ...", "application/octet-stream")},
                headers=await _headers(token),
            )
            assert r.status_code == 415

    async def test_rejects_empty_and_scan_pdf(self):
        import fitz

        empty_pdf = fitz.open()
        empty_pdf.new_page()  # 无文本（模拟扫描件）
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("空.txt", b"", "text/plain")},
                headers=await _headers(token),
            )
            assert r.status_code == 422
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("扫描件.pdf", empty_pdf.tobytes(), "application/pdf")},
                headers=await _headers(token),
            )
            assert r.status_code == 422

    async def test_requires_auth(self):
        async with await _make_client() as client:
            r = await client.post(
                "/api/v1/files/parse",
                files={"file": ("a.txt", b"hello", "text/plain")},
            )
            assert r.status_code == 401


class TestExportDocx:
    @staticmethod
    async def _seed_review(tenant_id: str, user_id: str, **kw) -> str:
        review_id = kw.pop("review_id", "rev_export_test")
        async with SessionLocal() as session:
            session.add(ContractReview(
                review_id=review_id, thread_id="th_export", tenant_id=tenant_id, user_id=user_id,
                contract_title="劳动合同（李某）", contract_text="第一条…",
                risks_json=json.dumps([
                    {"risk_name": "试用期超法定上限", "level": "high",
                     "reason": "1 年期合同约定 6 个月试用期", "basis": "《劳动合同法》第十九条",
                     "suggestion": "试用期调整至不超过 2 个月"},
                ], ensure_ascii=False),
                risk_level="high", review_opinion="经审查，该合同试用期条款违法…",
                status="pending_review",
                **kw,
            ))
            await session.commit()
        return review_id

    async def test_export_by_owner(self):
        from docx import Document

        async with await _make_client() as client:
            me = (await _login(client, "L1001"))
            review_id = await self._seed_review("tenant_firm", me["user"]["user_id"])
            r = await client.get(f"/api/v1/reviews/{review_id}/export", headers=await _headers(me["token"]))
            assert r.status_code == 200, r.text
            assert "wordprocessingml" in r.headers["content-type"]
            doc = Document(io.BytesIO(r.content))
            all_text = "\n".join(p.text for p in doc.paragraphs) + \
                "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
            assert "合同审查意见书" in all_text
            assert "试用期超法定上限" in all_text
            assert "第十九条" in all_text
            assert "合伙人批注" in all_text

    async def test_export_by_partner(self):
        async with await _make_client() as client:
            lawyer = await _login(client, "L1001")
            review_id = await self._seed_review(
                "tenant_firm", lawyer["user"]["user_id"], review_id="rev_export_p")
            partner = await _login(client, "P2001")
            r = await client.get(f"/api/v1/reviews/{review_id}/export", headers=await _headers(partner["token"]))
            assert r.status_code == 200

    async def test_export_forbidden_for_other_lawyer(self):
        async with await _make_client() as client:
            lawyer = await _login(client, "L1001")
            review_id = await self._seed_review(
                "tenant_firm", lawyer["user"]["user_id"], review_id="rev_export_x")
            r = await client.post("/api/v1/auth/register", json={
                "user_no": "T9002", "username": "别的律师", "password": "test123456", "role": "lawyer",
            })
            assert r.status_code == 200, r.text
            # 阶段2 注册审核流：新账号默认 pending，登录会被 403 拦截。
            # 本测试只关心「非 owner 不能导出」，审核链路有 test_api/test_security2 覆盖，
            # 这里直接 DB 放行，避免把两个关注点绑在一起。
            from sqlalchemy import update

            from backend.memory.models import User

            async with SessionLocal() as s:
                await s.execute(update(User).where(User.user_no == "T9002").values(status="approved"))
                await s.commit()
            other = (await _login(client, "T9002", "test123456"))
            r = await client.get(f"/api/v1/reviews/{review_id}/export", headers=await _headers(other["token"]))
            assert r.status_code == 403

    async def test_export_404(self):
        async with await _make_client() as client:
            token = (await _login(client, "L1001"))["token"]
            r = await client.get("/api/v1/reviews/rev_not_exist/export", headers=await _headers(token))
            assert r.status_code == 404
