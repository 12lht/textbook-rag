"""全本跑一遍 _line_text，把所有还原出的上标列出来人工过一遍。"""
import collections
import sys

import pymupdf

sys.path.insert(0, r"D:\agent")
import documents  # noqa: E402

CASES = [
    (r"C:\Users\13526\Desktop\Hello-Algo.pdf", "Hello-Algo"),
    (r"C:\Users\13526\Desktop\Hello-Agents.pdf", "Hello-Agents"),
    (r"C:\Users\13526\Desktop\第1章 移动通信概述202603101500.pdf", "移动通信"),
]

for path, name in CASES:
    doc = pymupdf.open(path)
    rows = []
    for index in range(doc.page_count):
        page = doc[index]
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                raw = "".join(s["text"] for s in line["spans"])
                text = documents._line_text(line)
                if text.count("^") > raw.count("^"):
                    added = text.count("^") - raw.count("^")
                    rows.append((index + 1, added, text[:100]))
    doc.close()
    print(f"\n### {name}  还原上标 {sum(r[1] for r in rows)} 处，涉及 {len(rows)} 行")
    for page_no, added, text in rows[:60]:
        print(f"  p{page_no:<4} x{added}  {text!r}")
    if len(rows) > 60:
        print(f"  ... 省略 {len(rows) - 60} 行")
