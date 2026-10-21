# -*- coding: utf-8 -*-
"""解析改动后的实际产出：段落长度分布、代码块数量、样例片段。"""
import sys, statistics, tempfile, re
sys.path.insert(0, r"D:/agent")
from pathlib import Path
import pymupdf
import documents

CASES = [
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents", 40, 69),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo", 30, 59),
    (r"C:/Users/13526/Desktop/第1章 移动通信概述202603101500.pdf", "移动通信", 0, 31),
]

for path, label, start, end in CASES:
    src = pymupdf.open(path)
    subset = pymupdf.open()
    subset.insert_pdf(src, from_page=start, to_page=min(end, src.page_count - 1))
    data = subset.tobytes()
    with tempfile.TemporaryDirectory() as tmp:
        parsed = documents._parse_pdf(data, Path(tmp))
        md = documents.render_markdown(parsed.elements)
    kinds = {}
    for e in parsed.elements:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    fences = md.count("```") // 2
    code_chars = sum(len(e.content) for e in parsed.elements if e.kind == "code")
    text_lens = [len(e.content) for e in parsed.elements if e.kind == "text"]
    print("=" * 72)
    print(f"{label}  页 {start+1}-{end+1}")
    print(f"  元素: {kinds}   (共 {len(parsed.elements)})")
    print(f"  代码围栏: {fences} 个，代码总字符 {code_chars}")
    if text_lens:
        print(f"  正文段: {len(text_lens)} 段，中位 {statistics.median(text_lens):.0f} 字，"
              f"<30字 {sum(1 for n in text_lens if n < 30)/len(text_lens):.0%}")
    print(f"  warning: {parsed.warning}")
    code_samples = [e.content for e in parsed.elements if e.kind == "code"][:1]
    if code_samples:
        print("  --- 首个代码块（前 12 行）---")
        for ln in code_samples[0].splitlines()[:12]:
            print("   |" + ln)
    src.close(); subset.close()
