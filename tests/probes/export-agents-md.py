"""把 Hello-Agents 整本解析成一份可以直接打开的 Markdown。

产物目录：

    out/Hello-Agents/
        Hello-Agents.md      ← 带页码/章节锚点的全文，图和它同目录
        images/as_xxxx.png   ← 161 张插图

md 里的图引用从 `as_xxx.png` 改成 `images/as_xxx.png`，这样用 Typora、
VSCode 预览、Obsidian 打开都能直接看到图，不用额外配置。

用临时库解析，不碰 data/ 里的生产数据。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402

PDF = Path("C:/Users/13526/Desktop/Hello-Agents.pdf")
OUT_DIR = ROOT / "out" / "Hello-Agents"

TMP = Path(tempfile.mkdtemp(prefix="export-md-"))
documents.DB_PATH = TMP / "documents.sqlite3"
documents.UPLOAD_DIR = TMP / "uploads"
documents.ASSET_DIR = TMP / "assets"

data = PDF.read_bytes()
print(f"读入 {len(data)/1024/1024:.1f}MB", flush=True)
record = documents.create_upload("export", PDF.name, data)
doc_id = str(record["doc_id"])

started = time.perf_counter()
parsed = documents.parse_document(doc_id)
print(f"解析 {time.perf_counter()-started:.0f}s：{parsed['status']}，"
      f"{parsed['page_count']} 页 / {parsed['chunk_count']} 段 / {parsed['asset_count']} 图", flush=True)
if parsed["status"] != "ready":
    print("解析没成功：", parsed.get("error"), flush=True)
    sys.exit(1)

markdown = documents.markdown_of(doc_id, "export") or ""

# 图引用指向子目录，这样 md 和图片可以一起搬走
markdown = markdown.replace("](as_", "](images/as_")

if OUT_DIR.exists():
    shutil.rmtree(OUT_DIR)
images_dir = OUT_DIR / "images"
images_dir.mkdir(parents=True)

source_assets = documents.ASSET_DIR / doc_id
count = 0
for path in sorted(source_assets.glob("*.png")):
    shutil.copyfile(path, images_dir / path.name)
    count += 1

target = OUT_DIR / "Hello-Agents.md"
target.write_text(markdown, encoding="utf-8")

print(f"写入 {target}", flush=True)
print(f"  正文 {len(markdown)/1024:.0f}KB，图片 {count} 张 "
      f"（{sum(p.stat().st_size for p in images_dir.glob('*.png'))/1024/1024:.1f}MB）", flush=True)

head = markdown[:600].splitlines()
print("\n开头几行：", flush=True)
for line in head[:14]:
    print("  " + line, flush=True)

shutil.rmtree(TMP, ignore_errors=True)
