# -*- coding: utf-8 -*-
"""代码块能不能靠字体/颜色识别？看 Hello-Agents 第 20 页逐行的字体名。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf, collections

doc = pymupdf.open(r"C:/Users/13526/Desktop/Hello-Agents.pdf")
page = doc[19]
data = page.get_text("dict")
print("逐行：字体 / 字号 / 颜色 / 左边缘 / 文本")
for block in data["blocks"]:
    if block.get("type") != 0: continue
    for line in block["lines"]:
        spans = line.get("spans", [])
        if not spans: continue
        text = "".join(s.get("text","") for s in spans)
        if not text.strip(): continue
        fonts = {s["font"] for s in spans}
        size = max(s["size"] for s in spans)
        color = {s.get("color") for s in spans}
        print(f"  {sorted(fonts)} {size:4.1f} {sorted(color)} x={line['bbox'][0]:6.1f} | {text[:60]}")
doc.close()
