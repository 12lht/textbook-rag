# -*- coding: utf-8 -*-
"""逐页量：文本量、矢量图形占比、位图数量。

用来判断「哪一页其实是一张图，应该整页截图而不是输出零散标签」。
排除整页背景色块（面积 > 90% 页面），否则每页都是 100%。
"""
import sys

import pymupdf

PDF = r"C:\Users\13526\Desktop\第1章 移动通信概述202603101500.pdf"


def main() -> None:
    doc = pymupdf.open(PDF)
    print(f"{'页':>3} {'字符':>5} {'矢量块':>5} {'有效占比':>7} {'位图':>4}  首行")
    for index, page in enumerate(doc):
        area = page.rect.width * page.rect.height
        drawings = page.get_drawings()
        used = 0.0
        count = 0
        for drawing in drawings:
            rect = drawing["rect"]
            ratio = (rect.width * rect.height) / area
            if ratio > 0.9:          # 整页背景底色
                continue
            if rect.width < 8 or rect.height < 8:
                continue
            used += rect.width * rect.height
            count += 1
        text = page.get_text("text").replace("\n", "")
        first = text.strip().split("　")[0][:26]
        cover = min(used / area, 1.0)
        flag = " ← 大图" if cover > 0.35 and len(text) < 260 else ""
        print(f"{index + 1:>3} {len(text):>5} {count:>5} {cover:>6.0%} "
              f"{len(page.get_images(full=True)):>4}  {first}{flag}")
    doc.close()


if __name__ == "__main__":
    main()
