"""导出一段可直接打开的 Markdown（含图片），用来肉眼验收解析质量。"""
from __future__ import annotations
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT))
import pymupdf  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import documents
from documents import db as documents_db  # noqa: E402
from _sample import sample_pdf  # noqa: E402

CASES = [
    (str(sample_pdf("Hello-Algo")), "Hello-Algo", 30, 59),
    (str(sample_pdf("Hello-Agents")), "Hello-Agents", 40, 69),
    (str(sample_pdf("第1章 移动通信概述")), "移动通信", 0, 31),
]

TMP = Path(tempfile.mkdtemp(prefix="export-sample-"))
documents_db.DB_PATH = TMP / "documents.sqlite3"
documents_db.UPLOAD_DIR = TMP / "uploads"
documents_db.ASSET_DIR = TMP / "assets"

for pdf_path, name, start, end in CASES:
    src = pymupdf.open(pdf_path)
    subset = pymupdf.open()
    subset.insert_pdf(src, from_page=start, to_page=min(end, src.page_count - 1))
    data = subset.tobytes()
    src.close()
    subset.close()

    record = documents.create_upload("export", f"{name}-p{start+1}-{end+1}.pdf", data)
    doc_id = str(record["doc_id"])
    t0 = time.perf_counter()
    parsed = documents.parse_document(doc_id)
    markdown = documents.markdown_of(doc_id, "export") or ""
    markdown = markdown.replace("](as_", "](images/as_")

    out = ROOT / "out" / name
    if out.exists():
        shutil.rmtree(out)
    (out / "images").mkdir(parents=True)
    count = 0
    for path in (documents_db.ASSET_DIR / doc_id).glob("*.png"):
        shutil.copyfile(path, out / "images" / path.name)
        count += 1
    target = out / f"{name}-p{start+1}-{end+1}.md"
    target.write_text(markdown, encoding="utf-8")
    kinds: dict[str, int] = {}
    for chunk in documents.chunks_of(doc_id):
        kinds[chunk["kind"]] = kinds.get(chunk["kind"], 0) + 1
    print(f"{name}: {parsed['status']} / {parsed['page_count']} 页 / "
          f"{parsed['chunk_count']} 段 {kinds} / {count} 图 / "
          f"{len(markdown)/1024:.0f}KB / {time.perf_counter()-t0:.0f}s", flush=True)
    print(f"  → {target}", flush=True)

shutil.rmtree(TMP, ignore_errors=True)
