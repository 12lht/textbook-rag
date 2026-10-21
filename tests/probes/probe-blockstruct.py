# -*- coding: utf-8 -*-
"""block 结构指纹：一行是不是一个 block，决定了段落会不会被切碎。
对比 Skia(网页打印) 与 pypdf(LaTeX) 两类产物的差异。"""
import statistics
import pymupdf

PATHS = [
    (r"C:/Users/13526/Desktop/第1章 移动通信概述202603101500.pdf", "移动通信课件(PPT)"),
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents(Skia/网页打印)"),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo(pypdf/LaTeX)"),
]

for path, label in PATHS:
    doc = pymupdf.open(path)
    n = doc.page_count
    step = max(1, n // 8)
    line_counts, sizes = [], []
    multi = 0          # 一个 block 里有多行的页数
    for i in range(0, n, step):
        blocks = [b for b in doc[i].get_text("dict")["blocks"] if b.get("type") == 0]
        if not blocks:
            continue
        per = []
        for b in blocks:
            lines = b.get("lines", [])
            per.append(len(lines))
            sizes.append(len(lines))
        line_counts.append(len(blocks))
        if any(c > 1 for c in per):
            multi += 1
    print("=" * 70)
    print(f"{label}  抽样 {len(line_counts)} 页")
    print(f"  每页 block 数 中位 : {statistics.median(line_counts):.0f}")
    if sizes:
        print(f"  单 block 行数 中位 : {statistics.median(sizes):.0f}  最大 {max(sizes)}")
        one = sum(1 for s in sizes if s == 1)
        print(f"  『一行一 block』占比: {one}/{len(sizes)} = {one/len(sizes):.0%}")
    print(f"  含多行 block 的页数: {multi}/{len(line_counts)}")
    doc.close()
