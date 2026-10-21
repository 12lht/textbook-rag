"""定位 O(n^2) 这类上下标的真实 bbox / 字号。

用法: python probe-superscript.py <pdf> <关键词> [页码(1-based, 可选)]
不给页码时全本搜索前 5 处。
"""
import sys

import fitz


def dump_line(page_no: int, line: dict) -> None:
    x0, y0, x1, y1 = line["bbox"]
    print(f"  line bbox=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f})")
    for span in line["spans"]:
        sx0, sy0, sx1, sy1 = span["bbox"]
        print(
            f"    span size={span['size']:.2f} font={span['font'][:28]:28s} "
            f"bbox=({sx0:.1f},{sy0:.1f},{sx1:.1f},{sy1:.1f}) "
            f"origin={span['origin'][1]:.1f} text={span['text']!r}"
        )


def main() -> None:
    path = sys.argv[1]
    needle = sys.argv[2]
    only = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    doc = fitz.open(path)
    hits = 0
    for index, page in enumerate(doc):
        page_no = index + 1
        if only and page_no != only:
            continue
        rects = page.search_for(needle)
        if not rects:
            continue
        print(f"== page {page_no}  hits={len(rects)}")
        for rect in rects:
            print(f"  search rect=({rect.x0:.1f},{rect.y0:.1f},{rect.x1:.1f},{rect.y1:.1f})")
        data = page.get_text("dict")
        for block in data["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                text = "".join(span["text"] for span in line["spans"])
                if needle in text:
                    print(f"  -- matched line: {text!r}")
                    dump_line(page_no, line)
        hits += 1
        if not only and hits >= 5:
            break
    doc.close()


if __name__ == "__main__":
    main()
