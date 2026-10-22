"""并排对照：同一页，按 block 聚合 vs 按几何合并，看谁更接近原文段落。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf
import documents

SENT_END = "。！？；.!?;"


def merge_by_geometry(lines):
    paras, cur = [], None
    for x0, y0, x1, y1, text, size, _b in lines:
        if cur is None:
            cur = [x0, y0, x1, y1, text, size]; continue
        cx0, _cy0, cx1, cy1, ctext, csize = cur
        gap = y0 - cy1
        if (abs(size - csize) < 0.6 and -0.3 * csize <= gap <= 0.75 * csize
                and ctext.rstrip()[-1:] not in SENT_END
                and x0 - cx0 <= 1.5 * csize):
            cur[4] = ctext.rstrip() + text.lstrip()
            cur[2], cur[3] = max(cx1, x1), max(cy1, y1)
        else:
            paras.append(cur); cur = [x0, y0, x1, y1, text, size]
    if cur: paras.append(cur)
    return paras


doc = pymupdf.open(r"C:/Users/13526/Desktop/Hello-Agents.pdf")
for pg in (19, 20, 21):
    lines = documents._pdf_page_lines(doc[pg], set())
    if len(lines) < 15: continue
    blocks = {}
    for ln in lines: blocks.setdefault(ln[6], []).append(ln)
    by_block = ["".join(x[4].strip() for x in g) for g in blocks.values()]
    by_geom = [p[4] for p in merge_by_geometry(lines)]
    print("=" * 76)
    print(f"第 {pg+1} 页   行 {len(lines)} / block 段 {len(by_block)} / 几何段 {len(by_geom)}")
    print("\n--- 按 block（现状）---")
    for t in by_block: print(f"  [{len(t):4d}] {t[:100]}")
    print("\n--- 按几何（候选）---")
    for t in by_geom: print(f"  [{len(t):4d}] {t[:100]}")
doc.close()
