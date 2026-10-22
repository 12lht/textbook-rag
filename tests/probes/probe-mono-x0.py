"""代码块的左边缘和正文左边缘差多少？两本书都要看。
如果代码块稳定整体缩进，就用『x0 是否超出正文列』当区域判据。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf, collections

MONO_HINTS = ("menlo", "mono", "consol", "courier", "code", "hack", "fira",
              "jetbrains", "cascadia", "sarasa")


def main_font(line):
    w = collections.Counter()
    for s in line.get("spans", []):
        w[s["font"]] += len(s.get("text", ""))
    return w.most_common(1)[0][0] if w else ""


for path, label, pages in [
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents", range(18, 40)),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo", range(20, 60)),
]:
    doc = pymupdf.open(path)
    mono_x0, text_x0 = collections.Counter(), collections.Counter()
    for i in pages:
        if i >= doc.page_count: break
        for block in doc[i].get_text("dict")["blocks"]:
            if block.get("type") != 0: continue
            for line in block.get("lines", []):
                t = "".join(s.get("text", "") for s in line.get("spans", []))
                if not t.strip(): continue
                f = main_font(line).lower()
                key = round(line["bbox"][0])
                if any(h in f for h in MONO_HINTS):
                    mono_x0[key] += 1
                else:
                    text_x0[key] += 1
    print("=" * 70)
    print(f"{label}   页 {list(pages)[0]+1}~{min(list(pages)[-1]+1, doc.page_count)}")
    print(f"  正文行左边缘 top3 : {text_x0.most_common(3)}")
    print(f"  代码行左边缘 top3 : {mono_x0.most_common(3)}")
    if mono_x0 and text_x0:
        body = text_x0.most_common(1)[0][0]
        nearer = sum(c for x, c in mono_x0.items() if x >= body + 4)
        print(f"  代码行中 x0 >= 正文列+4 的占比: {nearer}/{sum(mono_x0.values())} = {nearer/sum(mono_x0.values()):.0%}")
    doc.close()
