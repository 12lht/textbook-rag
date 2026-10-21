# -*- coding: utf-8 -*-
"""扫描所有 PDF 里的「小字号 + 基线偏移」片段，评估上标判据的误伤面。

判据候选：
  ratio = span.size / 前一个 span.size        <= 0.85
  dy    = 行基线(取字符数最多的 span 的 origin y) - span.origin.y   （正=在上方）
  dy_n  = dy / 行主字号                         >= 0.25
  gap   = span.x0 - 前一个 span.x1              <= 0.15 * 行主字号
"""
import collections
import sys

import pymupdf

CASES = [
    (r"C:\Users\13526\Desktop\Hello-Algo.pdf", "Hello-Algo"),
    (r"C:\Users\13526\Desktop\Hello-Agents.pdf", "Hello-Agents"),
    (r"C:\Users\13526\Desktop\移动通信.pdf", "移动通信"),
]
RATIO = 0.85
DY_N = 0.25
GAP_N = 0.15


def dominant_size(spans):
    weight = collections.Counter()
    for span in spans:
        weight[round(span["size"], 2)] += len(span["text"])
    return weight.most_common(1)[0][0] if weight else 0.0


def scan(path, name, limit_pages=0):
    doc = pymupdf.open(path)
    total = doc.page_count if not limit_pages else min(limit_pages, doc.page_count)
    sup, sub, rejected = [], [], []
    for index in range(total):
        page = doc[index]
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                spans = [s for s in line["spans"] if s["text"].strip()]
                if len(spans) < 2:
                    continue
                base_size = dominant_size(line["spans"])
                base_y = collections.Counter(
                    round(s["origin"][1], 1) for s in line["spans"] for _ in s["text"]
                ).most_common(1)[0][0]
                for pos in range(1, len(spans)):
                    span = spans[pos]
                    prev = spans[pos - 1]
                    if span["size"] > prev["size"] * RATIO:
                        continue
                    dy = base_y - span["origin"][1]
                    gap = span["bbox"][0] - prev["bbox"][2]
                    if abs(dy) < DY_N * base_size or gap > GAP_N * base_size:
                        rejected.append((index + 1, span["text"], round(dy / base_size, 2),
                                         round(gap, 1), round(span["size"], 2),
                                         round(base_size, 2)))
                        continue
                    context = "".join(s["text"] for s in spans)
                    item = (index + 1, span["text"], round(dy / base_size, 2),
                            round(gap, 1), round(span["size"], 2), round(base_size, 2),
                            context[:70])
                    (sup if dy > 0 else sub).append(item)
    doc.close()
    print(f"\n### {name}  页数={total}")
    print(f"  上标候选 {len(sup)} / 下标候选 {len(sub)} / 被拒 {len(rejected)}")
    print("  --- 上标候选（前 25）")
    for item in sup[:25]:
        print(f"    p{item[0]:<4} {item[1]!r:<9} dyN={item[2]:<6} gap={item[3]:<5} "
              f"size={item[4]}/{item[5]}  {item[6]!r}")
    print("  --- 下标候选（前 15）")
    for item in sub[:15]:
        print(f"    p{item[0]:<4} {item[1]!r:<9} dyN={item[2]:<6} gap={item[3]:<5} "
              f"size={item[4]}/{item[5]}  {item[6]!r}")
    print("  --- 被拒（前 15）")
    for item in rejected[:15]:
        print(f"    p{item[0]:<4} {item[1]!r:<9} dyN={item[2]:<6} gap={item[3]:<5} "
              f"size={item[4]}/{item[5]}")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    for path, name in CASES:
        scan(path, name, limit)
