# -*- coding: utf-8 -*-
"""查 PDF 的『产地』：Producer / Creator / 页面尺寸 / 每页字数。
用来判断样本属于哪一类排版工具链，而不是靠猜。"""
import sys, statistics
import pymupdf

PATHS = [
    (r"C:/Users/13526/Desktop/第1章 移动通信概述202603101500.pdf", "移动通信课件"),
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents"),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo"),
]

for path, label in PATHS:
    print("=" * 70)
    print(label)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        print("  打不开:", exc)
        continue
    meta = doc.metadata or {}
    for key in ("producer", "creator", "format"):
        print(f"  {key:9s}: {meta.get(key)!r}")
    n = doc.page_count
    print(f"  页数     : {n}")

    # 页面尺寸
    sizes = {}
    for page in doc:
        k = (round(page.rect.width), round(page.rect.height))
        sizes[k] = sizes.get(k, 0) + 1
    print(f"  页面尺寸 : {dict(sorted(sizes.items(), key=lambda x:-x[1])[:3])}")

    # 每页字数（抽样 12 页）
    step = max(1, n // 12)
    words = []
    for i in range(0, n, step):
        words.append(len(doc[i].get_text("text").replace("\n", "").strip()))
    print(f"  每页字数 : 中位 {statistics.median(words):.0f}  最大 {max(words)}")
    print(f"  横版比例 : {sizes and max(sizes, key=sizes.get)[0] / max(sizes, key=sizes.get)[1]:.2f}")

    # 书签
    toc = doc.get_toc()
    print(f"  书签     : {len(toc)} 条")
    for item in toc[:3]:
        print(f"     {item}")

    # 字体名（判断是否内嵌标准字体 / 子集化）
    fonts = set()
    for i in range(min(3, n)):
        for f in doc[i].get_fonts():
            fonts.add(f[3])
    print(f"  字体样本 : {sorted(fonts)[:5]}")

    # 图像 / 矢量
    imgs = draw = 0
    for i in range(min(5, n)):
        imgs += len(doc[i].get_images())
        draw += len(doc[i].get_drawings())
    print(f"  前5页    : 位图 {imgs} 个 / 矢量 {draw} 条")
    doc.close()
