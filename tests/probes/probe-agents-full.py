"""整本 Hello-Agents（720 页 / 97.9MB）跑一遍：耗时、段数、图数、章节数。

只回答一个问题：**这本书现在能不能真的用起来**。
30 页的子集能跑不代表 720 页能跑——内存、耗时、图的数量都可能出问题。

存储放临时目录，不碰生产库。
"""
from __future__ import annotations

import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="agents-full-"))
documents.DB_PATH = TMP / "documents.sqlite3"
documents.UPLOAD_DIR = TMP / "uploads"
documents.ASSET_DIR = TMP / "assets"

PDF = Path("C:/Users/13526/Desktop/Hello-Agents.pdf")
data = PDF.read_bytes()
print(f"文件 {len(data)/1024/1024:.1f}MB，上限 {documents.MAX_FILE_BYTES//1024//1024}MB", flush=True)

started = time.perf_counter()
record = documents.create_upload("probe", PDF.name, data)
doc_id = str(record["doc_id"])
print(f"收下用时 {time.perf_counter()-started:.1f}s，状态 {record['status']}", flush=True)

started = time.perf_counter()
parsed = documents.parse_document(doc_id)
elapsed = time.perf_counter() - started
print(f"解析用时 {elapsed:.0f}s", flush=True)
print(f"状态 {parsed['status']}  error={parsed.get('error')}  warning={parsed.get('warning')}", flush=True)
print(f"页数 {parsed['page_count']}  段落 {parsed['chunk_count']}  图 {parsed['asset_count']}", flush=True)

chunks = documents.chunks_of(doc_id)
kinds = Counter(c["kind"] for c in chunks)
print(f"段落构成 {dict(kinds)}", flush=True)

texts = sorted(len(str(c["content"])) for c in chunks if c["kind"] == "text")
if texts:
    print(f"正文长度 中位 {texts[len(texts)//2]} / P95 {texts[int(len(texts)*0.95)]} / 最大 {texts[-1]}", flush=True)

markdown = documents.markdown_of(doc_id, "probe") or ""
print(f"Markdown {len(markdown)/1024:.0f}KB，页码锚点 {markdown.count('<!-- page:')}，"
      f"章节锚点 {markdown.count('<!-- chapter:')}", flush=True)

asset_dir = documents.ASSET_DIR / doc_id
images = sorted(asset_dir.glob("*.png")) if asset_dir.is_dir() else []
total = sum(p.stat().st_size for p in images)
print(f"图片文件 {len(images)} 个，共 {total/1024/1024:.1f}MB", flush=True)

some = images[len(images)//2] if images else None
if some:
    from pymupdf import Pixmap
    pix = Pixmap(str(some))
    print(f"抽查一张：{some.name} {pix.width}x{pix.height}", flush=True)

import shutil  # noqa: E402

shutil.rmtree(TMP, ignore_errors=True)
print("已清理临时目录", flush=True)
