"""诊断：几何规则为什么该合没合。逐对打印拆分的三个判据实际取值。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf
import documents

SENT_END = "。！？；.!?;"
doc = pymupdf.open(r"C:/Users/13526/Desktop/Hello-Agents.pdf")
body = documents._pdf_body_size(doc)
print(f"正文字号 {body}")
page = doc[50]
lines = documents._pdf_page_lines(page, set())
print(f"第 51 页 行数 {len(lines)}\n")
for i in range(len(lines) - 1):
    a, b = lines[i], lines[i + 1]
    gap = b[1] - a[3]
    same = abs(b[5] - a[5]) < 0.6
    tight = -0.3 * a[5] <= gap <= 0.75 * a[5]
    indent = b[0] - a[0] > 1.5 * a[5]
    open_tail = a[4].rstrip()[-1:] not in SENT_END
    ok = same and tight and open_tail and not indent
    if not ok:
        why = []
        if not same: why.append(f"字号 {a[5]:.1f}→{b[5]:.1f}")
        if not tight: why.append(f"gap {gap:.1f} (允许 {-0.3*a[5]:.1f}~{0.75*a[5]:.1f})")
        if not open_tail: why.append("上句已收尾")
        if indent: why.append(f"缩进 {b[0]-a[0]:.1f}>{1.5*a[5]:.1f}")
        print(f"  ✂ {' + '.join(why)}")
        print(f"      A[{a[5]:.1f}] {a[4][:46]}")
        print(f"      B[{b[5]:.1f}] {b[4][:46]}")
doc.close()
