"""SQLite 连接、建表与迁移。

整个应用只在这里碰 sqlite3 的连接细节，业务函数拿到的是 `_connect()` 这个上下文管理器。
搬出来之后 documents.py 只剩「收文件、切段、读写记录」——出数据库问题时只看这一个文件。
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

_log = logging.getLogger("agent.documents")

# 这个文件在 documents/ 里，data/ 在仓库根，所以往上两级。
# 用 with_name('data') 会得到 documents/data —— 数据目录会凭空多出一个空壳。
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "documents.sqlite3"
# 上传原件和抽取的插图也在这里定义，**只此一份**。
# 之前它们散在 core 里，测试靠 `documents.UPLOAD_DIR = 临时目录` 重定向；
# 一旦 core 从别处 import 一份副本，赋值就只改了副本，测试会静默写进真库。
UPLOAD_DIR = DATA_DIR / "uploads"
ASSET_DIR = DATA_DIR / "assets"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    filename     TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- pdf / pptx / docx
    status       TEXT NOT NULL,
    error        TEXT,                   -- 失败原因；ready 时为空
    warning      TEXT,                   -- 解析完成但有已知瑕疵（如双栏），可为空
    page_count   INTEGER NOT NULL DEFAULT 0,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    asset_count  INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    toc          TEXT,                   -- 目录 JSON，章节归属靠它
    markdown     TEXT,                   -- 解析产物：带页码/章节锚点的 Markdown
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
-- 同一个人的同一份文件只收一次；换个人传同一本书是另一行（各自的库互不可见）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_dedup ON documents(user_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id, created_at);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,        -- {doc_id}-{seq:04d}，全局唯一，可直接当引用锚点
    doc_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,           -- text / figure / formula
    content     TEXT NOT NULL,           -- 原文段落；图/公式是题注或编号说明
    page_start  INTEGER NOT NULL DEFAULT 0,
    page_end    INTEGER NOT NULL DEFAULT 0,
    chapter     TEXT,
    asset_id    TEXT,
    char_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, seq);

CREATE TABLE IF NOT EXISTS assets (
    asset_id   TEXT PRIMARY KEY,
    doc_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- figure / formula
    label      TEXT,                     -- 「图 3-4」，来自题注，不是模型编的
    caption    TEXT,
    page       INTEGER NOT NULL DEFAULT 0,
    file_path  TEXT NOT NULL,
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assets_doc ON assets(doc_id);

-- 分类：把一个学科的书归在一起（比如「移动通信」下面挂课件 + 教材）。
-- 和 documents 是「一对多」：一本书最多属于一个分类，`documents.category_id`
-- 为空就是「未分类」。**删分类不删书**，只把书退回未分类——书是贵重资产，
-- 删分类这个动作本身不该带走它。
CREATE TABLE IF NOT EXISTS categories (
    category_id TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
-- 同一个人的分类不许重名（换个人可以有同名分类，各自的库互不可见）
CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name ON categories(user_id, name);
CREATE INDEX IF NOT EXISTS idx_categories_user ON categories(user_id, created_at);

-- 语料版本：段落内容一变就换一个随机值。检索侧靠它判断内存索引是否过期——
-- 只看「段数 + updated_at」抓不住「同数量重切」和「同一秒内两次重切」（时间戳只到秒）。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_init_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """五张表都在才算就绪。加新表必须同步加进来：建表只在「不就绪」时跑，只判一部分的话
    老库永远进不了建表分支（同样的坑踩过三次）。
    """
    expected = {"documents", "chunks", "assets", "categories", "meta"}
    found = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('documents', 'chunks', 'assets', 'categories', 'meta')"
        )
    }
    return found == expected


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """拿一个连接。每个调用一个新连接：SQLite 连接不是线程安全的，而本模块在线程池里跑
    （线程是复用的），共享连接迟早撞上「另一个线程正在用」。连接本身很便宜。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if not _schema_ready(conn):
            with _init_lock:
                conn.executescript(_SCHEMA)
                conn.commit()
        _migrate(conn)
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """给老库补新列。`CREATE TABLE IF NOT EXISTS` 只管「没有这张表」，已存在的库不会自己
    长出 `markdown` 列，所以每次连接都 `PRAGMA table_info` 查一次（微秒级）。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "markdown" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN markdown TEXT")
    if "warning" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN warning TEXT")
    if "category_id" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN category_id TEXT")
    # 语料版本初始化：空串会被误读成「从来没有写过」，给一个初值再说
    if conn.execute("SELECT 1 FROM meta WHERE key = 'chunks'").fetchone() is None:
        _bump_corpus(conn)
    conn.commit()


def _bump_corpus(conn: sqlite3.Connection) -> None:
    """段落内容变了就换一个语料版本号。**每个写 chunks 的地方都要调**：
    检索侧的内存索引、向量新鲜度都靠它判断（详见 `retrieval._signature`）。

    为什么不能只靠 `updated_at`：它只精确到秒，同一秒里连做两次重切看起来一模一样；
    而「段数不变、内容换了」正是补图注、包公式这些操作的标准形态。
    """
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('chunks', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (secrets.token_hex(8),),
    )


def corpus_version() -> str:
    """当前语料版本。读不到（老库刚建表）就返回空串。"""
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'chunks'").fetchone()
    return str(row[0]) if row else ""


def _row_to_doc(row: sqlite3.Row) -> dict[str, object]:
    return {
        "doc_id": row["doc_id"],
        "title": row["title"],
        "filename": row["filename"],
        "kind": row["kind"],
        "status": row["status"],
        "error": row["error"],
        "warning": row["warning"],
        "page_count": row["page_count"],
        "chunk_count": row["chunk_count"],
        "asset_count": row["asset_count"],
        "size_bytes": row["size_bytes"],
        "category_id": row["category_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
