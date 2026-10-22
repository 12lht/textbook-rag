"""看 PPT 页的 block 结构与图形覆盖面积。

两个问题：
  1. 多余换行 —— 同一水平带上有多个独立文本框（左右栏），被按 x 交错输出
  2. 图片少   —— 无题注的大幅矢量图，我们只输出零散标签
"""
import sys

import pymupdf

sys.path.insert(0, r"D:\agent")

PDF = r"C:\Users\13526\Desktop\第1章 移动通信概述202603101500.pdf"


def block_report(page_no: int) -> None:
    doc = pymupdf.open(PDF)
    page = doc[page_no - 1]
    print(f"\n===== 第 {page_no} 页  rect={page.rect}")
    for index, block in enumerate(page.get_text("dict")["blocks"]):
        if block.get("type") != 0:
            continue
        x0, y0, x1, y1 = block["bbox"]
        lines = block.get("lines", [])
        texts = ["".join(s["text"] for s in line["spans"]) for line in lines]
        print(f"  block{index}: bbox=({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}) "
              f"{len(lines)} 行  首行={texts[0][:28]!r}")
        if len(lines) > 1 and len(texts) <= 12:
            for line in lines:
                lx0, ly0, lx1, ly1 = line["bbox"]
                t = "".join(s["text"] for s in line["spans"])
                print(f"      line y={ly0:.0f} x=({lx0:.0f}..{lx1:.0f}) {t[:40]!r}")
    drawings = page.get_drawings()
    images = page.get_images(full=True)
    print(f"  矢量图形 {len(drawings)} 条 / 位图 {len(images)} 张 / 文本字符 "
          f"{len(page.get_text('text').replace(chr(10), ''))}")
    if drawings:
        xs0 = min(d["rect"].x0 for d in drawings)
        ys0 = min(d["rect"].y0 for d in drawings)
        xs1 = max(d["rect"].x1 for d in drawings)
        ys1 = max(d["rect"].y1 for d in drawings)
        area = (xs1 - xs0) * (ys1 - ys0)
        print(f"  图形包围盒=({xs0:.0f},{ys0:.0f},{xs1:.0f},{ys1:.0f}) 占页面 {area / (page.rect.width * page.rect.height):.0%}")
    doc.close()


if __name__ == "__main__":
    for number in (3, 18):
        block_report(number)
