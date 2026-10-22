"""比「跨 block 间隙 2.0 → 0.35」前后，Markdown 里到底有哪些行变了。"""
import difflib
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:/agent")
sys.path.insert(0, str(ROOT))
import pymupdf  # noqa: E402
import documents  # noqa: E402

PDF = r"C:/Users/13526/Desktop/Hello-Agents.pdf"
START, END = 40, 69


def render() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="diff-gap-"))
    documents.DB_PATH = tmp / "d.sqlite3"
    documents.UPLOAD_DIR = tmp / "u"
    documents.ASSET_DIR = tmp / "a"
    src = pymupdf.open(PDF)
    sub = pymupdf.open()
    sub.insert_pdf(src, from_page=START, to_page=END)
    data = sub.tobytes()
    src.close()
    sub.close()
    record = documents.create_upload("x", "a.pdf", data)
    documents.parse_document(str(record["doc_id"]))
    return documents.markdown_of(str(record["doc_id"]), "x") or ""


current = render()

original = documents._split_by_gap


def loose(band, tight_cross_block=False):
    band.sort(key=lambda entry: (entry[0], entry[1]))
    groups = [[band[0]]] if band else []
    for entry in band[1:]:
        previous = groups[-1][-1]
        gap = max(entry[0] - previous[2], previous[0] - entry[2])
        if gap < 2.0 * max(entry[5], 1.0):
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


documents._split_by_gap = loose
before = render()

lines = list(
    difflib.unified_diff(before.split("\n"), current.split("\n"), lineterm="", n=0)
)
print(f"差异行数 {sum(1 for l in lines if l.startswith(('+', '-')) and not l.startswith(('+++', '---')))}")
for line in lines[:60]:
    print(line[:180])
