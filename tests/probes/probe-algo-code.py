# -*- coding: utf-8 -*-
"""在 Hello-Algo（LaTeX 排版）上验证同一套规则，防止它只对一本书成立。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf, collections

MONO = ("jetbrains", "menlo", "mono", "consol", "courier", "fira", "code")


def main_font(line):
    w = collections.Counter()
    for s in line.get("spans", []):
        w[s["font"]] += len(s.get("text", ""))
    return w.most_common(1)[0][0] if w else ""


def unclosed(s):
    s = s.replace('"""', "").replace("'''", "")
    if s.count("(") != s.count(")") or s.count("[") != s.count("]") or s.count("{") != s.count("}"):
        return True
    return s.count('"') % 2 == 1


doc = pymupdf.open(r"C:/Users/13526/Desktop/Hello-Algo.pdf")
# 找等宽行最多的页
best = []
for i in range(20, 120):
    n = sum(1 for b in doc[i].get_text("dict")["blocks"] if b.get("type") == 0
            for line in b.get("lines", [])
            if any(m in main_font(line).lower() for m in MONO))
    best.append((n, i))
best.sort(reverse=True)
print("等宽行最多的页:", [(n, p+1) for n, p in best[:4]])

for _, pg in best[:2]:
    page = doc[pg]
    rows = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0: continue
        for line in b.get("lines", []):
            t = "".join(s.get("text", "") for s in line.get("spans", []))
            if not t.strip(): continue
            size = max((s["size"] for s in line["spans"]), default=10.0)
            code = any(m in main_font(line).lower() for m in MONO) and line["bbox"][0] >= 71.0 + 0.4*size
            rows.append((line["bbox"][1], line["bbox"][0], t, code, size))
    rows.sort()
    out, i = [], 0
    while i < len(rows):
        if not rows[i][3]:
            out.append(("p", rows[i][2].strip())); i += 1; continue
        grp = []
        while i < len(rows) and rows[i][3]:
            grp.append(rows[i]); i += 1
        base = min(r[1] for r in grp); cw = grp[0][4]*0.6
        lines, last_x = [], None
        for r in grp:
            ind = max(0, round((r[1]-base)/cw)); prev = lines[-1] if lines else None
            if prev is not None and unclosed(prev) and last_x is not None and r[1] <= last_x + 0.5*cw:
                tail = prev.rstrip()
                sep = "" if tail[-1:] in "([{" else " "
                lines[-1] = tail + sep + r[2].strip()
            else:
                lines.append(" "*ind + r[2].rstrip())
            last_x = r[1]
        out.append(("code", "\n".join(lines)))
    print("\n" + "="*74 + f"\n第 {pg+1} 页")
    for k, body in out:
        print(f"```\n{body}\n```" if k == "code" else body)
        print()
doc.close()
