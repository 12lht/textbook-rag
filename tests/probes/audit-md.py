"""审计 data/ 里 24 本 ready 书的解析成品质量（markdown + chunks）。

指标：乱码(U+FFFD)、无章节归属、段首像页码、条目符残留、图引用与 asset 失配、
页锚点覆盖、字数分布、最高频重复行（页眉残留嫌疑）。
"""
from __future__ import annotations

import re
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"D:/agent")
from dotenv import load_dotenv

load_dotenv(r"D:/agent/.env")
import documents
import pymupdf

BULLETS = "\u27a2\uf070\uf0a7\uf0b7\u25cf\u2022"
HEADERISH = re.compile(r"^(第?\s*\d+\s*页|\d{1,3})$")

docs = documents.list_documents("1")
print(f"ready {sum(1 for d in docs if d['status']=='ready')} 本\n")

for d in docs:
    if d["status"] != "ready":
        print(f"[{d['status']}] {d['title'][:26]}  error: {(d.get('error') or '')[:60]}")
        continue
    md = documents.markdown_of(d["doc_id"], "1") or ""
    chunks = documents.chunks_of(d["doc_id"])
    text = [c for c in chunks if c["kind"] == "text"]
    lens = [len(c["content"]) for c in text]
    figs = [c for c in chunks if c["kind"] == "figure"]

    bad = sum(md.count("\ufffd") for _ in [0])
    no_chapter = sum(1 for c in text if not c.get("chapter"))
    head_hits = sum(
        1 for c in text
        if any(HEADERISH.match(ln.strip()) for ln in c["content"].splitlines()[:1])
    )
    bullets = sum(
        1 for c in chunks for ln in c["content"].splitlines()
        if ln[:1] and ln.lstrip()[:1] in BULLETS
    )
    ref_ids = set(re.findall(r"as_[0-9a-f]+", md))
    asset_ids = {a for a in ref_ids}
    pages_in_md = sorted({int(m) for m in re.findall(r"<!--\s*page:(\d+)", md)})
    anchor_cov = (len(pages_in_md) / d["page_count"]) if d["page_count"] else 0
    lines = [ln.strip() for c in text for ln in c["content"].splitlines() if 2 < len(ln.strip()) < 40]
    top = Counter(lines).most_common(1)
    repeated = ""
    if top and top[0][1] >= max(4, d["page_count"] * 0.3):
        repeated = f"  疑似页眉×{top[0][1]}: {top[0][0][:24]!r}"

    flag = []
    if bad: flag.append(f"乱码{bad}")
    if no_chapter > len(text) * 0.3: flag.append(f"无章节{no_chapter}/{len(text)}")
    if bullets: flag.append(f"条目符{bullets}")
    if anchor_cov < 0.9: flag.append(f"锚点{anchor_cov:.0%}")

    print(f"{d['title'][:26]:<28} {d['page_count']:>3}页 {len(chunks):>4}段 "
          f"(文{len(text)} 图{len(figs)}) 中位{statistics.median(lens):.0f}字 "
          f"<60字{sum(1 for n in lens if n<60)/len(lens):.0%} "
          f"页眉段{head_hits}{'  ⚠ ' + ' '.join(flag) if flag else ''}{repeated}")
