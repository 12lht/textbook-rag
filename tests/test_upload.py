"""上传闸门、去重、老库迁移、删除。

守四条：
1. **上限**：超了要明确报错，不是静默截断；
2. **去重**：同内容不同名返回同一条（重复解析会白烧额度、还会让引用失配）；
3. **老库迁移**：没有 `warning` 列的老库打开后自动补列，老行不丢；
4. **删除**：真删行 + 真删目录，删完 `get_document` 返回 None。

原先这里是 `probe-preflight.py`，守的是「扫描版必须 failed」「双栏要挂 warning」——
那两条判据属于已删掉的本地解析预检；MinerU 的 vlm 自带 OCR 和版面模型，两条都不再成立。

原 `tests/probes/probe-upload.py`，2026-11-03 迁到 pytest。
"""
from __future__ import annotations

import sqlite3

import pytest

import documents

# 老库的建表语句：**故意没有 warning 列**，用来验证自动补列。
LEGACY_SCHEMA = """
    CREATE TABLE documents (
        doc_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
        filename TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
        page_count INTEGER NOT NULL DEFAULT 0, chunk_count INTEGER NOT NULL DEFAULT 0,
        asset_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL,
        file_path TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, toc TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
"""


@pytest.fixture
def pdf(sample_pdf):
    return sample_pdf("Hello-Algo").read_bytes()


@pytest.fixture
def uploaded(tmp_store, pdf):
    """先传一条正常大小的，供去重和删除两组用例复用。"""
    return documents.create_upload("probe", "Hello-Algo.pdf", pdf)


@pytest.mark.requires_sample
def test_accepts_normal_size(uploaded, tmp_store):
    assert uploaded["status"] == documents.STATUS_PENDING, uploaded["status"]
    assert (tmp_store / "uploads" / str(uploaded["doc_id"])).is_dir(), "原文件没落盘"


@pytest.mark.requires_sample
def test_rejects_over_limit(pdf, monkeypatch):
    """超上限要抛错，不是静默截断。"""
    monkeypatch.setattr(documents.core, "MAX_FILE_BYTES", 1024)
    with pytest.raises(documents.DocumentError) as exc:
        documents.create_upload("probe", "太大的.pdf", pdf)
    assert "上限" in str(exc.value), str(exc.value)[:60]


@pytest.mark.requires_sample
def test_duplicate_returns_same_record(uploaded, pdf):
    duplicate = documents.create_upload("probe", "换个名字.pdf", pdf)
    assert duplicate["doc_id"] == uploaded["doc_id"], "同内容不同名没指向同一条"
    assert duplicate.get("duplicated") is True, duplicate.get("duplicated")
    assert len(documents.list_documents("probe")) == 1, "多出了一行"


def test_legacy_db_gains_warning_column(tmp_path, monkeypatch):
    """没有 warning 列的老库，打开后要自己长出这一列，且老行不丢。"""
    legacy = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO documents VALUES ('d-old','probe','老书','old.pdf','pdf','ready',NULL,"
        "10,5,0,'hash','/tmp/x',1,'[]','2026-01-01','2026-01-01')"
    )
    conn.commit()
    conn.close()

    from documents import db

    monkeypatch.setattr(db, "DB_PATH", legacy)

    rows = documents.list_documents("probe")
    assert len(rows) == 1, f"老行丢了，只剩 {len(rows)} 条"
    assert rows[0]["chunk_count"] == 5, rows[0]["chunk_count"]
    assert rows[0].get("warning") is None, rows[0].get("warning")

    columns = {row[1] for row in sqlite3.connect(legacy).execute("PRAGMA table_info(documents)")}
    assert "warning" in columns, f"迁移后列还是：{sorted(columns)}"


@pytest.mark.requires_sample
def test_delete_removes_row_and_directory(uploaded, tmp_store):
    doc_id = str(uploaded["doc_id"])
    assert documents.get_document(doc_id, "probe") is not None, "删之前就查不到"
    assert documents.delete_document(doc_id, "probe") is True
    assert documents.get_document(doc_id, "probe") is None, "删完还能查到"
    assert not (tmp_store / "uploads" / doc_id).exists(), "目录没删"
    assert documents.delete_document(doc_id, "probe") is False, "重复删除不幂等"
