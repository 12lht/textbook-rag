"""探针：上传闸门 + 库迁移 + 删除。

原先这里是 `probe-preflight.py`，守的是「扫描版必须 failed」「双栏要挂 warning」——
那两条判据属于已删掉的本地解析预检。MinerU 的 vlm 自带 OCR 和版面模型，
扫描版和双栏都能解析，所以这两条不再成立，剩下的闸门挪到这里：

1. **上限**：超了要明确报错，不是静默截断；
2. **去重**：同内容不同名返回同一条（重复解析会白烧额度、还会让引用失配）；
3. **老库迁移**：没有 `warning` 列的老库打开后自动补列，老行不丢；
4. **删除**：真删行 + 真删目录，删完 `get_document` 返回 None。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sample import sample_pdf  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="upload-"))
documents.DB_PATH = TMP / "documents.sqlite3"
documents.UPLOAD_DIR = TMP / "uploads"
documents.ASSET_DIR = TMP / "assets"

PDF = sample_pdf("Hello-Algo").read_bytes()  # textbook/ 已移出仓库，样本从 data/uploads 取
print(f"样本：Hello-Algo.pdf {len(PDF) / 1024 / 1024:.1f}MB，"
      f"上限 {documents.MAX_FILE_BYTES // 1024 // 1024}MB\n")

print("== 1. 上限 ==")
record = documents.create_upload("probe", "Hello-Algo.pdf", PDF)
check("正常大小能收", record["status"] == documents.STATUS_PENDING, str(record["status"]))
doc_id = str(record["doc_id"])
check("原文件落盘了", (documents.UPLOAD_DIR / doc_id).is_dir())

limit = documents.MAX_FILE_BYTES
documents.MAX_FILE_BYTES = 1024  # 只为触发闸门，不改真常量
try:
    documents.create_upload("probe", "太大的.pdf", PDF)
    check("超上限被拒", False, "居然收下了")
except documents.DocumentError as exc:
    check("超上限被拒", "上限" in str(exc), str(exc)[:60])
finally:
    documents.MAX_FILE_BYTES = limit

print("\n== 2. 去重 ==")
duplicate = documents.create_upload("probe", "换个名字.pdf", PDF)
check("返回同一条", duplicate["doc_id"] == doc_id)
check("带 duplicated 标记", duplicate.get("duplicated") is True, str(duplicate.get("duplicated")))
check("没有多出一行", len(documents.list_documents("probe")) == 1,
      str(len(documents.list_documents("probe"))))

print("\n== 3. 老库迁移：没有 warning 列的表能自己长出来 ==")
legacy = TMP / "legacy.sqlite3"
conn = sqlite3.connect(legacy)
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

documents.DB_PATH = legacy
old_rows = documents.list_documents("probe")
check("老行还在", len(old_rows) == 1, str(len(old_rows)))
check("老行字段没丢", old_rows[0]["chunk_count"] == 5, str(old_rows[0]["chunk_count"]))
check("老行 warning 为 None", old_rows[0].get("warning") is None, str(old_rows[0].get("warning")))
columns = {row[1] for row in sqlite3.connect(legacy).execute("PRAGMA table_info(documents)")}
check("表里已经有了 warning 列", "warning" in columns)
documents.DB_PATH = TMP / "documents.sqlite3"

print("\n== 4. 删除：行和目录一起走 ==")
check("删之前查得到", documents.get_document(doc_id, "probe") is not None)
check("删除返回 True", documents.delete_document(doc_id, "probe") is True)
check("删之后查不到", documents.get_document(doc_id, "probe") is None)
check("目录也没了", not (documents.UPLOAD_DIR / doc_id).exists())
check("再删返回 False（幂等）", documents.delete_document(doc_id, "probe") is False)

print(f"\n{'全部通过' if not failures else '失败：' + '、'.join(failures)}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failures else 0)
