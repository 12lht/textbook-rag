# -*- coding: utf-8 -*-
"""量一下真实行距：段内行距 vs 段间距，看阈值 0.75 倍字号对不对。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf, statistics
import documents

for path, label, pages in [
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents", (49, 50, 51)),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo", (34, 35, 36)),
]:
    doc = pymupdf.open(path)
    body_left = documents._pdf_body_left(doc)
    print("=" * 70)
    print(f"{label}  body_left={body_left}")
    gaps = []
    for pg in pages:
        lines = documents._pdf_page_lines(doc[pg], set())
        for a, b in zip(lines, lines[1:]):
            size = max(a[5], 1.0)
            gap = b[1] - a[3]
            gaps.append((gap / size, a[5], a[4][:26], b[4][:26], b[0] - a[0]))
    ratios = sorted(g[0] for g in gaps)
    print(f"  行距/字号 分布: min={ratios[0]:.2f} p25={ratios[len(ratios)//4]:.2f} "
          f"中位={statistics.median(ratios):.2f} p75={ratios[3*len(ratios)//4]:.2f} max={ratios[-1]:.2f}")
    print("  最小的 8 个（最像段内）:")
    for g in sorted(gaps, key=lambda x: abs(x[0]))[:8]:
        print(f"    {g[0]:+.2f}x  字号{g[1]:.1f}  dx={g[4]:+.1f} | {g[2]} → {g[3]}")
    doc.close()
