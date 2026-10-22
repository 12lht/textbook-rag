"""入库质量审计：三本基线书重跑解析 + 切分，输出可量化的脏点。

只用临时目录，不碰 data/documents.sqlite3。
"""
from __future__ import annotations

import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"D:/agent")
import pymupdf

import documents

CASES = [
    (r"D:/agent/.workbuddy/tmp/marker-compare/pdf/algo.pdf", "Hello-Algo", False),
    (r"D:/agent/.workbuddy/tmp/marker-compare/pdf/agents.pdf", "Hello-Agents", False),
    (r"D:/agent/.workbuddy/tmp/marker-compare/pdf/mobile.pdf", "移动通信", True),
]

BULLETS = "\u27a2\uf070\uf0a7\uf0b7\u25cf\u2022"
HEADERISH = re.compile(r"^(第?\s*\d+\s*页|\d{1,3})$")


def audit(path: str, label: str, per_page: bool) -> None:
    data = Path(path).read_bytes()
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="audit-"))
    (tmp / "assets").mkdir(parents=True, exist_ok=True)
    parsed = documents._parse_pdf(data, tmp / "assets")
    md = documents.render_markdown(parsed.elements)
    chunks = documents.cut_chunks(md, "doc", section_per_page=per_page)
    dt = time.time() - t0

    text = [c for c in chunks if c["kind"] == "text"]
    lens = [len(c["content"]) for c in text]
    figs = [c for c in chunks if c["kind"] == "figure"]
    forms = [c for c in chunks if c["kind"] == "formula"]
    tables = [c for c in chunks if c["kind"] == "table"]

    bullet_lines = sum(
        1 for c in chunks for ln in c["content"].splitlines() if ln[:1] and ln.lstrip()[:1] in BULLETS
    )
    head_hits = [
        c for c in text
        if any(HEADERISH.match(ln.strip()) for ln in c["content"].splitlines()[:1])
    ]
    no_chapter = sum(1 for c in text if not c["chapter"])

    print("=" * 78)
    print(f"{label}   {len(chunks)} chunk ({dt:.1f}s)  pages={parsed.page_count}")
    if parsed.warning:
        print(f"  warning: {parsed.warning}")
    print(
        f"  text {len(text)}  figure {len(figs)}  formula {len(forms)}  table {len(tables)}"
    )
    if lens:
        print(
            f"  字数 中位 {statistics.median(lens):.0f}  均值 {sum(lens)/len(lens):.0f}  "
            f"最大 {max(lens)}  <60字 {sum(1 for n in lens if n < 60)/len(lens):.0%}"
        )
    print(f"  无章节归属 {no_chapter}/{len(text)}   段首像页码/页眉 {len(head_hits)}")
    print(f"  行首条目符残留 {bullet_lines} 行")
    if text:
        print(f"  首条 p{text[0]['page_start']}: {text[0]['content'][:90]!r}")
        print(f"  末条 p{text[-1]['page_end']}: {text[-1]['content'][:90]!r}")
    (tmp / "markdown.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    for path, label, per_page in CASES:
        if only and only not in label:
            continue
        audit(path, label, per_page)
