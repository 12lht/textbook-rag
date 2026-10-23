"""探针：Markdown 里的图引用能不能真的渲染出来（`?render=1`）。

验的是这一件事：**存进去的 `![图 3-4](as_77.png)` 换成带签名的 URL 之后，
不带任何令牌的 GET 请求也能取到那张 PNG**。这是「Markdown 引用图片」能成立的
全部前提——`<img>` 不会替我们带 Authorization 头。

存储重定向到临时目录，不碰 data/ 下的真库与真图。
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PDF = ROOT / "textbook/Hello-Algo.pdf"

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {label}  {detail}")
    else:
        FAIL += 1
        print(f"  ✗ {label}  {detail}")


def header(text: str) -> None:
    print(f"\n== {text} ==")


REAL_DIR = ROOT / "data"
REAL_DB = REAL_DIR / "documents.sqlite3"


def real_snapshot() -> tuple[int | None, list[str]]:
    assets = REAL_DIR / "assets"
    listing = sorted(str(p.name) for p in assets.rglob("*")) if assets.is_dir() else []
    return (REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else None, listing)


before = real_snapshot()

import pymupdf  # noqa: E402

import documents  # noqa: E402
import users  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="asset-render-"))
users.DB_PATH = TMP / "users.sqlite3"
documents.DB_PATH = TMP / "documents.sqlite3"
documents.UPLOAD_DIR = TMP / "uploads"
documents.ASSET_DIR = TMP / "assets"

import agent_core  # noqa: E402


async def fake_stream_agent(*args, **kwargs):
    if False:
        yield {}


agent_core.stream_agent = fake_stream_agent

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

client = TestClient(api.app)

ALICE = {"account": "probe_render_a", "password": "correct-horse-1"}
BOB = {"account": "probe_render_b", "password": "battery-staple-2"}

ASSET_REF = re.compile(r"!\[.*?\]\((as_[0-9a-f]+)\.png\)")
RENDER_REF = re.compile(r"!\[.*?\]\((/api/assets/as_[0-9a-f]+\?u=[^&]+&t=[^)]+)\)")

print("临时目录:", TMP)
check("样本存在", PDF.is_file(), str(PDF))

src = pymupdf.open(PDF)
out = pymupdf.open()
out.insert_pdf(src, from_page=34, to_page=43)  # 第 35-44 页
sample = TMP / "sample.pdf"
out.save(sample)
out.close()
src.close()

with client:
    header("0. 准备：两个互不相干的账号")
    ra = client.post("/api/auth/register", json=ALICE)
    rb = client.post("/api/auth/register", json=BOB)
    check("注册 A / B", ra.status_code == 200 and rb.status_code == 200,
          f"{ra.status_code}/{rb.status_code}")
    ta, tb = ra.json()["token"], rb.json()["token"]
    auth_a = {"Authorization": f"Bearer {ta}"}
    auth_b = {"Authorization": f"Bearer {tb}"}

    header("1. 上传并解析（TestClient 会同步跑完 BackgroundTasks）")
    upload = client.post(
        "/api/documents",
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
        headers=auth_a,
    )
    check("上传 → 200", upload.status_code == 200, str(upload.status_code))
    record = upload.json()
    doc_id = record["doc_id"]
    listing = client.get("/api/documents", headers=auth_a).json()
    status = listing[0]["status"]
    check("解析完成", status == "ready", f"{status} {listing[0].get('error') or ''}")
    asset_total = listing[0]["asset_count"]
    check("抽到了图", asset_total > 0, f"{asset_total} 张")

    header("2. 原文模式：图引用必须是相对文件名（存进去什么样拿出来什么样）")
    raw = client.get(f"/api/documents/{doc_id}/markdown", headers=auth_a)
    check("原文 → 200", raw.status_code == 200, str(raw.status_code))
    raw_text = raw.text
    raw_refs = ASSET_REF.findall(raw_text)
    check("原文里有相对引用", len(raw_refs) > 0, f"{len(raw_refs)} 处，如 {raw_refs[:2]}")
    check("原文里没有签名 URL", "/api/assets/" not in raw_text)
    check("原文里锚点还在", "<!-- page:" in raw_text and "<!-- chapter:" in raw_text)
    check(
        "原文与存储逐字相同",
        raw_text == documents.markdown_of(doc_id, ra.json()["user"]["user_id"]),
    )

    header("3. 渲染模式：图引用换成带签名的 URL，其余内容一字不动")
    rendered = client.get(f"/api/documents/{doc_id}/markdown?render=1", headers=auth_a)
    check("render → 200", rendered.status_code == 200, str(rendered.status_code))
    render_text = rendered.text
    url_refs = RENDER_REF.findall(render_text)
    check("渲染里的图都换成了 URL", len(url_refs) == len(raw_refs),
          f"{len(url_refs)} / {len(raw_refs)}")
    check("渲染里没有漏掉的相对引用", not ASSET_REF.search(render_text))
    restored = RENDER_REF.sub(lambda m: "![" + "换回来" + "](" + "x.png)", render_text)
    check(
        "除图引用外内容未变",
        re.sub(r"!\[.*?\]\([^)]*\)", "IMG", raw_text) == re.sub(r"!\[.*?\]\([^)]*\)", "IMG", restored),
    )
    check("渲染模式也保留锚点", "<!-- page:" in render_text)

    header("4. 图真的能取到：**不带任何令牌**的 GET")
    route = urlparse(url_refs[0])
    image = client.get(f"{route.path}?{route.query}")  # 没有 headers
    check("无令牌取图 → 200", image.status_code == 200, str(image.status_code))
    check("是 PNG", image.headers.get("content-type", "").startswith("image/png"),
          image.headers.get("content-type", ""))
    check("真的是图（PNG magic）", image.content[:8] == b"\x89PNG\r\n\x1a\n", f"{len(image.content)} bytes")
    check("不是 1x1 占位", len(image.content) > 2000, f"{len(image.content)/1024:.0f}KB")

    header("5. 签名不是万能钥匙")
    path, query = route.path, route.query
    tampered_u = re.sub(r"u=[^&]+", "u=someone-else", query)
    check("改 u → 403", client.get(f"{path}?{tampered_u}").status_code == 403)
    tampered_t = re.sub(r"t=[^&]+", "t=9999999999.deadbeef", query)
    check("改签名 → 403", client.get(f"{path}?{tampered_t}").status_code == 403)
    expired = re.sub(r"t=[^&]+", "t=1000000000.abc", query)
    check("过期 → 403", client.get(f"{path}?{expired}").status_code == 403)
    check("无签名参数 → 403", client.get(path).status_code == 403)
    check("不存在的图 → 403/404（不给探测面）",
          client.get(path.rsplit("/", 1)[0] + "/as_00000000").status_code in (403, 404))

    header("6. 越权：B 拿不到 A 的书")
    check("B 取 A 的 markdown → 404",
          client.get(f"/api/documents/{doc_id}/markdown", headers=auth_b).status_code == 404)
    check("B 取 A 的 render → 404",
          client.get(f"/api/documents/{doc_id}/markdown?render=1", headers=auth_b).status_code == 404)
    check("游客取 render → 401",
          client.get(f"/api/documents/{doc_id}/markdown?render=1").status_code == 401)
    check("A 的签名 URL 不带身份也能取（签名即凭证）",
          client.get(f"{path}?{query}").status_code == 200)

    header("7. 老数据兼容：已入库的书不用重新解析")
    import sqlite3

    conn = sqlite3.connect(documents.DB_PATH)
    conn.execute("UPDATE documents SET markdown = ? WHERE doc_id = ?",
                 ("![图 1-1 旧书](as_deadbeef.png)\n", doc_id))
    conn.commit()
    conn.close()
    old = client.get(f"/api/documents/{doc_id}/markdown?render=1", headers=auth_a)
    check("旧格式也能出 URL", old.status_code == 200 and "/api/assets/as_deadbeef?" in old.text,
          old.text.strip()[:80])

after = real_snapshot()
check("真实 data/ 未被改动", before == after, f"{before} -> {after}")

shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'全部通过' if FAIL == 0 else f'{FAIL} 项失败'}（{PASS} 通过）")
sys.exit(1 if FAIL else 0)
