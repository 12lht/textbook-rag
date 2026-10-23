"""回归探针：排版框不许再把插图吞掉。


Hello-Agents（720 页）解析出来 **0 张图**。根因是一条三步链：

1. 这本书每页有一个**排版框** `[72,30,540,760]`（页面 612x792，矢量矩形）；
2. 它被当成「图形」收进列表，`_merge_clusters` 再把它和真插图合并成整页大簇；
3. `_nearest_graphic_above` 要求 `图注顶 − 簇底 ∈ [0, 340]`，整页簇的底边在
   题注下方（760 > 540），gap 为负 → 排除 → 一本书一张图都配不上。

修法：`_is_layout_frame` 在收集阶段就剔掉「大且框住半数文字块」的矩形。

这个探针守三件事：排版框别再被当图、Hello-Agents 的图别丢、Hello-Algo 别回归。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

import documents  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


_TMP = Path(tempfile.mkdtemp(prefix="frame-filter-"))
documents.DB_PATH = _TMP / "documents.sqlite3"
documents.UPLOAD_DIR = _TMP / "uploads"
documents.ASSET_DIR = _TMP / "assets"

AGENTS = ROOT / "textbook/Hello-Agents.pdf"
ALGO = ROOT / "textbook/Hello-Algo.pdf"


def count_figures(pdf_path: Path, first: int, last: int) -> tuple[int, int]:
    """返回 (配上的图数, 认出的题注数)。"""
    src = pymupdf.open(pdf_path)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=first - 1, to_page=last - 1)
    sample = _TMP / f"{pdf_path.stem}-{first}-{last}.pdf"
    out.save(sample)
    out.close()
    src.close()

    record = documents.create_upload("probe", pdf_path.name, sample.read_bytes())
    documents.parse_document(str(record["doc_id"]))
    chunks = documents.chunks_of(str(record["doc_id"]))
    return sum(1 for c in chunks if c["kind"] == "figure"), str(record["doc_id"])


print("== 1. 排版框确实存在（否则这个探针在测空气） ==")
pdf = pymupdf.open(AGENTS)
page = pdf[44]
raw_frames = [
    d["rect"] for d in page.get_drawings()
    if d.get("rect") and d["rect"].height > page.rect.height * 0.6
]
check("页面里有整页级别的矢量矩形", bool(raw_frames),
      f"{[round(x) for x in raw_frames[0]] if raw_frames else '无'}")

clusters = documents._pdf_graphic_rects(page)
still_frame = [r for r in clusters if r.height > page.rect.height * 0.6]
check("但它没被当成图形簇", not still_frame,
      f"残留 {[round(x) for x in still_frame[0]]}" if still_frame else "已排除")
pdf.close()

print("\n== 2. 两本书各自抽到多少图 ==")
agents_figures, _ = count_figures(AGENTS, 41, 70)
algo_figures, _ = count_figures(ALGO, 31, 60)
print(f"  Hello-Agents 41-70 页：{agents_figures} 张")
print(f"  Hello-Algo   31-60 页：{algo_figures} 张")

check("Hello-Agents 的图抽得出来（修复前是 0）", agents_figures >= 10, str(agents_figures))
check("Hello-Algo 没有回归", algo_figures >= 18, str(algo_figures))

print(f"\n{'全部通过' if not failures else '失败：' + '、'.join(failures)}")
sys.exit(1 if failures else 0)
