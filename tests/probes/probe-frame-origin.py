"""钉死因果链：Skia 渲染出来的矢量矩形里，有多少被判定成排版框。
如果 Hello-Agents 的框远多于 Hello-Algo，就说明『排版框是网页打印的产物』。"""
import pymupdf
import documents

PATHS = [
    (r"C:/Users/13526/Desktop/第1章 移动通信概述202603101500.pdf", "移动通信课件(PPT)"),
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents(Skia)"),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo(pypdf)"),
]

for path, label in PATHS:
    doc = pymupdf.open(path)
    n = doc.page_count
    step = max(1, n // 8)
    total = frames = 0
    samples = []
    for i in range(0, n, step):
        page = doc[i]
        text_rects = [b[:4] for b in page.get_text("blocks") if b[6] == 0]
        text_rects = [pymupdf.Rect(r) for r in text_rects]
        for path_item in page.get_drawings():
            rect = path_item["rect"]
            total += 1
            if documents._is_layout_frame(rect, page.rect, text_rects):
                frames += 1
                if len(samples) < 2:
                    samples.append(tuple(round(v) for v in rect))
    print("=" * 70)
    print(f"{label}  抽样 {len(range(0, n, step))} 页")
    print(f"  矢量矩形总数 : {total}")
    print(f"  判定为排版框 : {frames}" + (f"  ({frames/total:.0%})" if total else ""))
    if samples:
        print(f"  框样本       : {samples}  (页面 {round(doc[0].rect.width)}x{round(doc[0].rect.height)})")
    doc.close()
