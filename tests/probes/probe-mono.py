# -*- coding: utf-8 -*-
"""等宽字体的分布：它是识别代码块的可靠信号吗？三类样本都查。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf, collections

MONO_HINTS = ("menlo", "mono", "consol", "courier", "code", "hack", "fira", "source",
              "cascadia", "jetbrains", "dejavu sans mono", "sarasa", "noto sans mono")

PATHS = [
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents(Skia)"),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo(pypdf)"),
    (r"C:/Users/13526/Desktop/第1章 移动通信概述202603101500.pdf", "移动通信(PPT)"),
]

for path, label in PATHS:
    doc = pymupdf.open(path)
    n = doc.page_count
    step = max(1, n // 10)
    font_counter = collections.Counter()
    mono_lines = total_lines = 0
    for i in range(0, n, step):
        for block in doc[i].get_text("dict")["blocks"]:
            if block.get("type") != 0: continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans: continue
                text = "".join(s.get("text", "") for s in spans)
                if not text.strip(): continue
                total_lines += 1
                # 取该行字符数最多的字体当主字体
                weights = collections.Counter()
                for s in spans:
                    weights[s["font"]] += len(s.get("text", ""))
                main = weights.most_common(1)[0][0]
                font_counter[main] += 1
                if any(h in main.lower() for h in MONO_HINTS):
                    mono_lines += 1
    print("=" * 70)
    print(f"{label}   抽样 {len(range(0,n,step))} 页 / {total_lines} 行")
    print(f"  等宽字体行: {mono_lines} ({mono_lines/total_lines:.1%})" if total_lines else "")
    for name, cnt in font_counter.most_common(4):
        print(f"    {cnt:6d}  {name[:60]}")
    doc.close()
