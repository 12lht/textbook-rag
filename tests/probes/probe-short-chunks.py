# -*- coding: utf-8 -*-
"""短正文 chunk 到底是什么？"""
import sys, tempfile
sys.path.insert(0, r"D:/agent")
from pathlib import Path
import pymupdf
import documents

src = pymupdf.open(r"C:/Users/13526/Desktop/Hello-Algo.pdf")
subset = pymupdf.open()
subset.insert_pdf(src, from_page=30, to_page=59)
with tempfile.TemporaryDirectory() as tmp:
    parsed = documents._parse_pdf(subset.tobytes(), Path(tmp))
md = documents.render_markdown(parsed.elements)
chunks = documents.cut_chunks(md, "probe")
text = [c for c in chunks if c["kind"] == "text"]
short = [c for c in text if len(c["content"]) < 80]
print(f"正文 {len(text)} 条，其中 <80 字 {len(short)} 条")
print("-" * 70)
for c in short:
    print(f"  [{len(c['content']):3d}] p{c['page_start']} | {c['content'][:88]!r}")
src.close(); subset.close()
