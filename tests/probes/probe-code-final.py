"""最终验证：mono 主导行 + 区段整体缩进 >= 正文列 + 0.4 字号。"""
import sys
sys.path.insert(0, r"D:/agent")
import pymupdf, collections, re

MONO_HINTS = ("menlo", "mono", "consol", "courier", "code", "hack", "fira",
              "jetbrains", "cascadia", "sarasa")
FILE_HINT = re.compile(r"\^\^=\s*File:/s*(/S+?)/s*/^/^=")
LANG = {"py": "python", "js": "javascript", "ts": "typescript", "sh": "bash",
        "md": "markdown", "yml": "yaml", "yaml": "yaml", "json": "json",
        "java": "java", "go": "go", "rs": "rust", "c": "c", "cpp": "cpp",
        "txt": "text", "toml": "toml", "html": "html", "css": "css"}


def main_font(line):
    w = collections.Counter()
    for s in line.get("spans", []):
        w[s["font"]] += len(s.get("text", ""))
    return w.most_common(1)[0][0] if w else ""


def is_mono(name):
    return any(h in name.lower() for h in MONO_HINTS)


for path, label, pages in [
    (r"C:/Users/13526/Desktop/Hello-Agents.pdf", "Hello-Agents", (19, 20)),
    (r"C:/Users/13526/Desktop/Hello-Algo.pdf", "Hello-Algo", (36, 37)),
]:
    doc = pymupdf.open(path)
    body_x = 72.0 if "Agents" in label else 71.0
    print("#" * 74)
    print(f"# {label}")
    for pg in pages:
        page = doc[pg]
        rows = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0: continue
            for line in block.get("lines", []):
                t = "".join(s.get("text", "") for s in line.get("spans", []))
                if not t.strip(): continue
                size = max((s["size"] for s in line["spans"]), default=10.0)
                rows.append((line["bbox"][1], line["bbox"][0], t, is_mono(main_font(line)), size))
        rows.sort()

        out, i = [], 0
        while i < len(rows):
            grp = []
            while i < len(rows) and rows[i][3]:
                grp.append(rows[i]); i += 1
            if not grp:
                out.append(("p", rows[i][2].strip())); i += 1; continue
            base = min(r[1] for r in grp)
            size = grp[0][4]
            if base < body_x + 0.4 * size:      # 顶格 → 是行内 code，不是代码块
                for r in grp: out.append(("p", r[2].strip()))
                continue
            cw = size * 0.6
            lines = []
            for r in grp:
                ind = max(0, round((r[1] - base) / cw))
                prev = lines[-1] if lines else None
                if ind == 0 and prev is not None and prev.rstrip()[-1:] in ',(="\'':
                    lines[-1] = prev.rstrip() + r[2].strip()
                else:
                    lines.append(" " * ind + r[2].rstrip())
            lang = ""
            for prev in reversed([b for k, b in out[-3:] if k == "p"]):
                m = FILE_HINT.search(prev)
                if m:
                    ext = m.group(1).rsplit(".", 1)[-1].lower()
                    lang = LANG.get(ext, ext); break
            out.append(("code", "\n".join(lines), lang))
        print(f"\n<!-- 第 {pg+1} 页 -->")
        for item in out:
            if item[0] == "code":
                print(f"\n```{item[2]}\n{item[1]}\n```\n")
            else:
                print(item[1])
    doc.close()
