"""把「解析 → Markdown → 渲染」的结果导出成一张自包含的 HTML 快照。

用途：**不用登录、不用起服务**也能一眼看到解析产物渲染出来是什么样——
图在原来的位置、表格是真表格、代码块没有散架。

图片以 data URI 内嵌，所以这个 .html 单独拷给别人也能看。
全部写在临时目录里，不碰 data/。
"""
from __future__ import annotations

import base64
import html
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

import documents  # noqa: E402

PDF = Path("C:/Users/13526/Desktop/Hello-Algo.pdf")
OUT = ROOT / ".workbuddy/tmp/render-preview.html"
PAGES = (30, 60)  # 第 31-60 页：正文、图、表格都有（58/59 页是表 2-1）

TMP = Path(tempfile.mkdtemp(prefix="render-snapshot-"))
documents.DB_PATH = TMP / "documents.sqlite3"
documents.UPLOAD_DIR = TMP / "uploads"
documents.ASSET_DIR = TMP / "assets"

src = pymupdf.open(PDF)
out = pymupdf.open()
out.insert_pdf(src, from_page=PAGES[0], to_page=PAGES[1] - 1)
sample = TMP / "sample.pdf"
out.save(sample)
out.close()
src.close()

record = documents.create_upload("u1", "Hello-Algo.pdf", sample.read_bytes())
doc_id = str(record["doc_id"])
documents.parse_document(doc_id)
markdown = documents.markdown_of(doc_id, "u1") or ""
doc = documents.list_documents("u1")[0]

def embed(match: "re.Match[str]") -> str:
    alt, name = match.group(1), match.group(2)
    path = documents.ASSET_DIR / doc_id / name
    if not path.is_file():
        return match.group(0)
    data = base64.b64encode(path.read_bytes()).decode()
    return f"![{alt}](data:image/png;base64,{data})"


import re  # noqa: E402

markdown_with_images = re.sub(r"!\[(.*?)\]\((as_[0-9a-f]+\.png)\)", embed, markdown)
chunks = documents.chunks_of(doc_id)

page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>解析产物渲染快照 · Hello-Algo 第 {PAGES[0] + 1}-{PAGES[1]} 页</title>
<script src="https://cdn.jsdelivr.net/npm/marked@15.0.7/marked.min.js"></script>
<style>
body{{margin:0;background:#1b1b1b;color:#d8d8d8;font:15px/1.8 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
header{{position:sticky;top:0;background:#1b1b1b;border-bottom:1px solid #333;padding:12px 32px;font-size:12px;color:#8a8a8a}}
main{{padding:26px 32px 80px;max-width:960px}}
h1,h2,h3,h4{{line-height:1.4;margin:1.6em 0 .6em}} h1{{font-size:22px}} h2{{font-size:19px}} h3{{font-size:16px}}
p{{margin:.8em 0}}
img{{max-width:100%;height:auto;display:block;margin:1em auto;border-radius:6px;background:#fff}}
table{{border-collapse:collapse;margin:1em 0;font-size:13px;display:block;overflow-x:auto}}
th,td{{border:1px solid #3a3a3a;padding:6px 10px;text-align:left}} th{{background:#242424}}
code{{background:#242424;padding:1px 5px;border-radius:4px;font:13px ui-monospace,Menlo,monospace}}
pre{{background:#151515;border-radius:8px;padding:14px 18px;overflow-x:auto}}
pre code{{background:none;padding:0}}
blockquote{{margin:1em 0;padding:.2em 0 .2em 14px;border-left:3px solid #444;color:#a8a8a8}}
</style></head>
<body>
<header>{html.escape("Hello-Algo.pdf")} 第 {PAGES[0] + 1}-{PAGES[1]} 页 ·
{len(chunks)} 段 · {doc["asset_count"]} 张图 ·
本页是解析产物（Markdown）的渲染结果，不是原 PDF</header>
<main id="view"></main>
<script>
const md = {markdown_with_images!r};
const view = document.getElementById('view');
if (!window.marked) {{  // 离线时别留白页，最起码把 Markdown 原文给出来
  const pre = document.createElement('pre'); pre.textContent = md; view.appendChild(pre);
}} else {{
  view.innerHTML = marked.parse(md, {{breaks: true, gfm: true}});
}}
</script>
</body></html>
"""

OUT.write_text(page, encoding="utf-8")
print(f"写入 {OUT}  ({OUT.stat().st_size/1024:.0f}KB)")
print(f"图片 {documents.list_documents('u1')[0]['asset_count']} 张，段落 {len(chunks)} 条")
shutil.rmtree(TMP, ignore_errors=True)
