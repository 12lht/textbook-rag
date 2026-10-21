# -*- coding: utf-8 -*-
"""模拟『按几何重建段落』，不碰 documents.py。
对比：按 block_index 聚合（现状） vs 按行距+字号+标点合并（候选）。
"""
import sys
sys.path.insert(0, r"D:/agent")
import statistics
import pymupdf
import documents

SENT_END = "。！？；.!?;"


def merge_by_geometry(lines, body_size):
    """lines: (x0,y0,x1,y1,text,size,block) 已按 y 排序。返回段落列表。"""
    paras = []
    cur = None
    for x0, y0, x1, y1, text, size, _block in lines:
        if cur is None:
            cur = [x0, y0, x1, y1, text, size]
            continue
        cx0, _cy0, cx1, cy1, ctext, csize = cur
        gap = y0 - cy1
        same_size = abs(size - csize) < 0.6
        # 行内间距 vs 段间距：行距一般 <=0.75 倍字号，段间距 >=1 倍
        tight = -0.3 * csize <= gap <= 0.75 * csize
        # 左边缘：中文首行缩进约 2 字，续行顶格；所以下一行比当前**更靠右**超过 1.5 字 → 新段
        indent_break = x0 - cx0 > 1.5 * csize
        open_tail = ctext.rstrip()[-1:] not in SENT_END
        if same_size and tight and open_tail and not indent_break:
            joiner = " " if ctext.rstrip()[-1:].isascii() and ctext.rstrip()[-1:] else ""
            cur[4] = ctext.rstrip() + joiner + text.lstrip()
            cur[2] = max(cx1, x1)
            cur[3] = max(cy1, y1)
        else:
            paras.append(cur)
            cur = [x0, y0, x1, y1, text, size]
    if cur:
        paras.append(cur)
    return paras


PAGES = [(r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents(Skia)", [49, 50, 51]),
         (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo(pypdf)", [30, 31, 32]),
         (r"C:/Users/13526/Desktop/第1章 移动通信概述202603101500.pdf", "移动通信(PPT)", [5, 6, 7])]

for path, label, page_idx in PAGES:
    doc = pymupdf.open(path)
    body = documents._pdf_body_size(doc)
    print("=" * 74)
    print(f"{label}   正文字号 {body}")
    old_lens, new_lens = [], []
    sample = []
    for i in page_idx:
        if i >= doc.page_count:
            continue
        lines = documents._pdf_page_lines(doc[i], set())
        if not lines:
            continue
        # 现状：按 block 聚合
        blocks = {}
        for ln in lines:
            blocks.setdefault(ln[6], []).append(ln)
        for group in blocks.values():
            old_lens.append(sum(len(x[4]) for x in group))
        # 候选：按几何
        for para in merge_by_geometry(lines, body):
            new_lens.append(len(para[4]))
        if not sample:
            sample = merge_by_geometry(lines, body)[:3]
    print(f"  按 block  : {len(old_lens)} 段，中位 {statistics.median(old_lens):.0f} 字")
    print(f"  按几何    : {len(new_lens)} 段，中位 {statistics.median(new_lens):.0f} 字")
    for p in sample:
        print(f"    · [{len(p[4]):4d}字] {p[4][:90]}")
    doc.close()
