"""探针：解析前预检 + warning 列 + 上传上限。

守四件事：

1. **扫描版不许静默成功**：纯图 PDF 必须 `failed`，且原因说得清是「扫描版」；
2. **双栏要给瑕疵提示**：状态仍是 ready（能检索），但 `warning` 非空；
3. **正常书不许出现 warning**：否则提示会变成噪音，人人忽略；
4. **老库能迁移**：没有 `warning` 列的老库打开后自动补列，老行不丢。

顺带验 150MB 上限对 Hello-Agents（97.9MB）放行。
"""
from __future__ import annotations

import shutil
import sqlite3
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


TMP = Path(tempfile.mkdtemp(prefix="preflight-"))
documents.DB_PATH = TMP / "documents.sqlite3"
documents.UPLOAD_DIR = TMP / "uploads"
documents.ASSET_DIR = TMP / "assets"

ALGO = Path("C:/Users/13526/Desktop/Hello-Algo.pdf")
SCANNED = ROOT / ".workbuddy/tmp/scanned-3p.pdf"
TWOCOL = ROOT / ".workbuddy/tmp/twocol.pdf"


def upload_and_parse(name: str, data: bytes) -> dict[str, object]:
    record = documents.create_upload("probe", name, data)
    return documents.parse_document(str(record["doc_id"]))


print("== 1. 扫描版：必须 failed，且原因说人话 ==")
scanned = upload_and_parse("scanned.pdf", SCANNED.read_bytes())
check("状态是 failed", scanned["status"] == "failed", str(scanned["status"]))
error = str(scanned.get("error") or "")
check("原因里点明扫描版", "扫描版" in error, error[:80])
check("原因里点明不做 OCR", "OCR" in error, error[:80])
check("没有产出 segments", scanned["chunk_count"] == 0, str(scanned["chunk_count"]))
check("warning 为空（错误不该伪装成瑕疵）", not scanned.get("warning"))

print("\n== 2. 双栏：能解析，但要挂 warning ==")
twocol = upload_and_parse("twocol.pdf", TWOCOL.read_bytes())
check("状态是 ready", twocol["status"] == "ready", str(twocol["status"]))
check("有 warning", bool(twocol.get("warning")), str(twocol.get("warning"))[:60])
check("warning 提到双栏", "双栏" in str(twocol.get("warning") or ""))
check("仍然产出了段落", int(twocol["chunk_count"]) > 0, str(twocol["chunk_count"]))

print("\n== 3. 正常单栏书：不许有 warning ==")
src = pymupdf.open(ALGO)
out = pymupdf.open()
out.insert_pdf(src, from_page=30, to_page=59)
sample = TMP / "algo.pdf"
out.save(sample)
out.close()
src.close()
normal = upload_and_parse("algo.pdf", sample.read_bytes())
check("状态是 ready", normal["status"] == "ready", str(normal["status"]))
check("没有 warning", not normal.get("warning"), str(normal.get("warning")))
check("段落数正常", int(normal["chunk_count"]) > 30, str(normal["chunk_count"]))

print("\n== 4. 老库迁移：没有 warning 列的表能自己长出来 ==")
legacy_path = TMP / "legacy.sqlite3"
conn = sqlite3.connect(legacy_path)
conn.executescript(
    """
    CREATE TABLE documents (
        doc_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
        filename TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
        page_count INTEGER NOT NULL DEFAULT 0, chunk_count INTEGER NOT NULL DEFAULT 0,
        asset_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL,
        file_path TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, toc TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    """
)
conn.execute(
    "INSERT INTO documents VALUES ('d-old','probe','老书','old.pdf','pdf','ready',NULL,"
    "10,5,0,'hash','/tmp/x',1,'[]','2026-01-01','2026-01-01')"
)
conn.commit()
conn.close()

documents.DB_PATH = legacy_path
old_rows = documents.list_documents("probe")
check("老行还在", len(old_rows) == 1, str(len(old_rows)))
check("老行 warning 为 None", old_rows[0].get("warning") is None, str(old_rows[0].get("warning")))
check("老行字段没丢", old_rows[0]["chunk_count"] == 5, str(old_rows[0]["chunk_count"]))
columns = {row[1] for row in sqlite3.connect(legacy_path).execute("PRAGMA table_info(documents)")}
check("表里已经有了 warning 列", "warning" in columns)
documents.DB_PATH = TMP / "documents.sqlite3"

print("\n== 5. 上限：97.9MB 的 Hello-Agents 现在能收 ==")
agents = Path("C:/Users/13526/Desktop/Hello-Agents.pdf")
size_mb = agents.stat().st_size / 1024 / 1024
print(f"  文件大小 {size_mb:.1f}MB，上限 {documents.MAX_FILE_BYTES // 1024 // 1024}MB")
try:
    record = documents.create_upload("probe", "Hello-Agents.pdf", agents.read_bytes())
    check("不再被拒", record["status"] == "pending", str(record["status"]))
    documents.delete_document(str(record["doc_id"]), "probe")
except documents.DocumentError as exc:
    check("不再被拒", False, str(exc))

print(f"\n{'全部通过' if not failures else '失败：' + '、'.join(failures)}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failures else 0)
