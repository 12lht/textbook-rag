"""被误判成代码的正文行长什么样？"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf
import documents

doc = pymupdf.open(r"C:/Users/13526/Desktop/Hello-Agents.pdf")
body_left = documents._pdf_body_left(doc)
print(f"body_left={body_left}")
hits = 0
for pg in range(40, 70):
    lines = documents._pdf_page_lines(doc[pg], set())
    for ln in lines:
        is_code = ln[7] and ln[0] >= body_left + 0.4 * max(ln[5], 1.0)
        if is_code and any(ch in ln[4] for ch in "。？！，") and len(ln[4]) > 40:
            print(f"  页{pg+1} x0={ln[0]:.1f} 字号{ln[5]:.1f} | {ln[4][:70]}")
            hits += 1
            if hits > 8: break
    if hits > 8: break
doc.close()
