"""Markdown 里的图引用能不能真的渲染出来（`?render=1`）。

验的是这一件事：**存进去的 `![图 3-4](as_77.png)` 换成带签名的 URL 之后，
不带任何令牌的 GET 请求也能取到那张 PNG**。这是「Markdown 引用图片」能成立的
全部前提——`<img>` 不会替我们带 Authorization 头。

要真实解析一份 PDF（MinerU，约 40 秒）并注册账号，所以打 `requires_sample`
+ `requires_network`，且解析结果用 module 级 fixture 复用。
原 `tests/probes/probe-asset-render.py`，2026-11-03 迁到 pytest。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pymupdf
import pytest
from fastapi.testclient import TestClient

import agent_core
import api
import documents
import users
from documents import db

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DIR = REPO_ROOT / "data"
REAL_DB = REAL_DIR / "documents.sqlite3"

ALICE = {"account": "probe_render_a", "password": "correct-horse-1"}
BOB = {"account": "probe_render_b", "password": "battery-staple-2"}

ASSET_REF = re.compile(r"!\[.*?\]\((as_[0-9a-f]+)\.png\)")
RENDER_REF = re.compile(r"!\[.*?\]\((/api/assets/as_[0-9a-f]+\?u=[^&]+&t=[^)]+)\)")

pytestmark = [pytest.mark.requires_sample, pytest.mark.requires_network]


def real_snapshot() -> tuple[int | None, list[str]]:
    assets = REAL_DIR / "assets"
    listing = sorted(str(p.name) for p in assets.rglob("*")) if assets.is_dir() else []
    return (REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else None, listing)


async def fake_stream_agent(*args, **kwargs):
    """上传解析不该走到对话流；替换掉以免真调模型。"""
    if False:
        yield {}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """把存储重定向到临时目录，并裁出一份 10 页样张。

    module 级：真解析一次要 40 秒，每个用例重来不可接受。
    """
    tmp = tmp_path_factory.mktemp("asset-render")
    saved = {
        "users": users.DB_PATH,
        "db": db.DB_PATH,
        "upload": db.UPLOAD_DIR,
        "asset": db.ASSET_DIR,
    }
    (tmp / "uploads").mkdir()
    (tmp / "assets").mkdir()
    users.DB_PATH = tmp / "users.sqlite3"
    db.DB_PATH = tmp / "documents.sqlite3"
    db.UPLOAD_DIR = tmp / "uploads"
    db.ASSET_DIR = tmp / "assets"

    before = real_snapshot()
    try:
        yield SimpleNamespace(tmp=tmp, before=before)
    finally:
        users.DB_PATH = saved["users"]
        db.DB_PATH = saved["db"]
        db.UPLOAD_DIR = saved["upload"]
        db.ASSET_DIR = saved["asset"]


@pytest.fixture(scope="module")
def session(env, sample_pdf):
    """注册两个账号（一个持邀请码）、传一本、等解析完，返回可复用的句柄。"""
    source_path = sample_pdf("Hello-Algo")
    src = pymupdf.open(source_path)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=34, to_page=43)  # 第 35-44 页
    sample = env.tmp / "sample.pdf"
    out.save(sample)
    out.close()
    src.close()

    original = agent_core.stream_agent
    agent_core.stream_agent = fake_stream_agent
    client = TestClient(api.app)
    try:
        with client:
            # 邀请制上线后，第一个账号是主人，第二个必须持码注册
            ra = client.post("/api/auth/register", json=ALICE)
            assert ra.status_code == 200, f"注册主人 A 失败：{ra.status_code} {ra.text[:80]}"
            auth_a = {"Authorization": f"Bearer {ra.json()['token']}"}

            seed = client.post("/api/invites", json={"note": "给 B"}, headers=auth_a)
            assert seed.status_code == 200, f"发邀请码失败：{seed.text[:80]}"

            rb = client.post("/api/auth/register", json={**BOB, "invite": seed.json()["code"]})
            assert rb.status_code in (200, 201), f"B 持码注册失败：{rb.status_code} {rb.text[:80]}"
            auth_b = {"Authorization": f"Bearer {rb.json()['token']}"}

            upload = client.post(
                "/api/documents",
                files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
                headers=auth_a,
            )
            assert upload.status_code == 200, f"上传失败：{upload.status_code} {upload.text[:120]}"
            doc_id = upload.json()["doc_id"]

            listing = client.get("/api/documents", headers=auth_a).json()
            assert listing[0]["status"] == "ready", (
                f"解析没完成：{listing[0]['status']} {listing[0].get('error') or ''}"
            )
            assert listing[0]["asset_count"] > 0, "一张图都没抽到"

            yield SimpleNamespace(
                client=client,
                auth_a=auth_a,
                auth_b=auth_b,
                doc_id=doc_id,
                user_a=ra.json()["user"]["user_id"],
                asset_count=listing[0]["asset_count"],
            )
    finally:
        agent_core.stream_agent = original


def test_raw_markdown_keeps_relative_refs(session):
    """原文模式：图引用必须是相对文件名，存进去什么样拿出来什么样。"""
    raw = session.client.get(f"/api/documents/{session.doc_id}/markdown", headers=session.auth_a)
    assert raw.status_code == 200, raw.status_code
    raw_text = raw.text

    refs = ASSET_REF.findall(raw_text)
    assert refs, "原文里没有相对引用"
    assert "/api/assets/" not in raw_text, "原文里混进了签名 URL"
    assert "<!-- page:" in raw_text and "<!-- chapter:" in raw_text, "锚点丢了"
    assert raw_text == documents.markdown_of(session.doc_id, session.user_a), "取出来和存的不一致"


def test_render_mode_signs_urls_without_touching_content(session):
    """渲染模式：只有图引用被换成 URL，其余内容一字不动。"""
    raw_text = session.client.get(
        f"/api/documents/{session.doc_id}/markdown", headers=session.auth_a
    ).text
    rendered = session.client.get(
        f"/api/documents/{session.doc_id}/markdown?render=1", headers=session.auth_a
    )
    assert rendered.status_code == 200, rendered.status_code
    render_text = rendered.text

    assert len(RENDER_REF.findall(render_text)) == len(ASSET_REF.findall(raw_text)), (
        "换出来的 URL 数和原文图引用数不一致"
    )
    assert not ASSET_REF.search(render_text), "还有漏掉的相对引用"
    assert "<!-- page:" in render_text, "渲染模式把锚点弄丢了"

    def strip(text: str) -> str:
        return re.sub(r"!\[.*?\]\([^)]*\)", "IMG", text)

    assert strip(raw_text) == strip(render_text), "除图引用外内容被改了"


def test_image_is_fetchable_without_any_token(session):
    """整件事的前提：`<img>` 不带 Authorization 也能取到图。"""
    url_refs = RENDER_REF.findall(
        session.client.get(
            f"/api/documents/{session.doc_id}/markdown?render=1", headers=session.auth_a
        ).text
    )
    route = urlparse(url_refs[0])
    image = session.client.get(f"{route.path}?{route.query}")  # 故意不带 headers

    assert image.status_code == 200, image.status_code
    assert image.headers.get("content-type", "").startswith("image/png"), image.headers.get("content-type")
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n", f"{len(image.content)} bytes"
    assert len(image.content) > 2000, f"只有 {len(image.content)} 字节，多半是占位图"


def test_signature_is_not_a_master_key(session):
    """改用户、改签名、过期、不存在的图——一律不给看。"""
    url_refs = RENDER_REF.findall(
        session.client.get(
            f"/api/documents/{session.doc_id}/markdown?render=1", headers=session.auth_a
        ).text
    )
    route = urlparse(url_refs[0])
    path, query = route.path, route.query

    assert session.client.get(f"{path}?{re.sub(r'u=[^&]+', 'u=someone-else', query)}").status_code == 403
    assert session.client.get(f"{path}?{re.sub(r't=[^&]+', 't=9999999999.deadbeef', query)}").status_code == 403
    assert session.client.get(f"{path}?{re.sub(r't=[^&]+', 't=1000000000.abc', query)}").status_code == 403
    assert session.client.get(path).status_code == 403, "无签名参数也能取"

    missing = path.rsplit("/", 1)[0] + "/as_00000000"
    assert session.client.get(missing).status_code in (403, 404), "不存在的图泄漏了存在性"


def test_cross_user_access_is_denied(session):
    assert session.client.get(
        f"/api/documents/{session.doc_id}/markdown", headers=session.auth_b
    ).status_code == 404, "B 能读到 A 的书"
    assert session.client.get(
        f"/api/documents/{session.doc_id}/markdown?render=1", headers=session.auth_b
    ).status_code == 404
    assert session.client.get(
        f"/api/documents/{session.doc_id}/markdown?render=1"
    ).status_code == 401, "游客能读"


def test_legacy_markdown_still_renders(session):
    """老数据里的相对引用也要能出 URL，不必重新解析。"""
    conn = sqlite3.connect(db.DB_PATH)
    try:
        original = conn.execute(
            "SELECT markdown FROM documents WHERE doc_id = ?", (session.doc_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE documents SET markdown = ? WHERE doc_id = ?",
            ("![图 1-1 旧书](as_deadbeef.png)\n", session.doc_id),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        old = session.client.get(
            f"/api/documents/{session.doc_id}/markdown?render=1", headers=session.auth_a
        )
        assert old.status_code == 200, old.status_code
        assert "/api/assets/as_deadbeef?" in old.text, old.text.strip()[:80]
    finally:
        conn = sqlite3.connect(db.DB_PATH)
        try:
            conn.execute(
                "UPDATE documents SET markdown = ? WHERE doc_id = ?", (original, session.doc_id)
            )
            conn.commit()
        finally:
            conn.close()


def test_real_data_dir_is_untouched(env):
    """整个过程只碰临时目录。通过时不打详情——快照是上千个文件名，会冲掉别的日志。"""
    after = real_snapshot()
    assert env.before == after, "真实 data/ 被改了"
