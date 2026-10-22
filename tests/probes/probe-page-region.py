"""打印某页某个 y 区间的所有 line，用来看上下标在 PDF 里长什么样。"""
import sys

import pymupdf

path, page_no, y0, y1 = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
doc = pymupdf.open(path)
page = doc[page_no - 1]
print(f"page {page_no} rect={page.rect}  images={len(page.get_images(full=True))}")
for block in page.get_text("dict")["blocks"]:
    if block.get("type") != 0:
        print(f"  [image block] bbox={block['bbox']}")
        continue
    for line in block["lines"]:
        x0, ly0, x1, ly1 = line["bbox"]
        if ly1 < y0 or ly0 > y1:
            continue
        text = "".join(s["text"] for s in line["spans"])
        print(f"line y=({ly0:.1f}..{ly1:.1f}) x=({x0:.1f}..{x1:.1f}) {text!r}")
        for span in line["spans"]:
            sx0, sy0, sx1, sy1 = span["bbox"]
            print(
                f"    size={span['size']:6.2f} oy={span['origin'][1]:7.2f} "
                f"x=({sx0:7.2f}..{sx1:7.2f}) font={span['font'][:24]:24s} {span['text']!r}"
            )
doc.close()
