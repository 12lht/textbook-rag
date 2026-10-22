"""教材库：把 PDF / PPTX / DOCX 变成「可检索的段落」和「可引用的图」。

应用层模块（同 `memories.py` / `conversations.py`）：只读写 `data/` 下的文件与 SQLite，
不 import 任何 agent 相关的东西，所以命令行、探针、离线批处理都能直接用。

只做三件事：收文件（算指纹去重、落盘、写 pending）、解析（切段落、抽图、配对图注，
推到 ready / failed）、存取（列表、删除、按用户取段落）。检索、排序、引用编号在
`retrieval.py`——守住这条边界，否则删书、换切分策略、做离线评测都要穿过检索逻辑。

收文件和解析分开：300 页 PDF 解析要几十秒，`create_upload()` 只落盘 + 写 pending，
毫秒级返回，重活由应用层丢线程池。代价是可能出现「写着 pending 但没人解析」的僵尸行，
`reconcile_pending()` 在启动时把超时未完成的置为 failed。

切分的目标不是切得均匀，是切出来能引用：不跨章合并、允许跨页合并、图注和截图合成
一条 `kind='figure'` 的 chunk（图注单独成 chunk 没有意义）。页码记 page_start /
page_end 两个，跨页 chunk 只记一个必然错。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

_log = logging.getLogger("agent.documents")

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "documents.sqlite3"
UPLOAD_DIR = DATA_DIR / "uploads"
ASSET_DIR = DATA_DIR / "assets"

MAX_FILE_BYTES = 250 * 1024 * 1024

SUPPORTED_SUFFIXES = {".pdf", ".pptx", ".docx"}

STATUS_PENDING = "pending"
STATUS_PARSING = "parsing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

STALE_PARSING_MINUTES = 30

TARGET_CHARS = 250
MAX_CHARS = 900
MIN_CHARS = 80

_CAPTION_RE = re.compile(
    r"^\s*(图|表|Figure|Fig\.?|Table)\s*[（(]?\s*"
    r"([0-9]+(?:\s*[-－.．–—‑]\s*[0-9]+)*|[A-Z]\s*[-－.．–—‑]\s*[0-9]+)"
    r"\s*[)）]?\s*[:：.．、]?\s*(.*)$",
    re.IGNORECASE,
)
_CAPTION_VERBS = (
    "所示", "中", "为", "是", "表示", "展示", "给出", "描述", "说明", "显示",
    "列出", "对比", "描绘", "列举", "呈现", "可见", "反映了", "是一种",
)
_FORMULA_NO_RE = re.compile(r"^\s*[（(]\s*([0-9]+(?:\s*[-－.．–—‑]\s*[0-9]+)+)\s*[)）]\s*$")
_HEADING_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百]+\s*[章节篇部]"
    r"|[0-9]+(\.[0-9]+){1,2}\s*[、.．]?\s*\S"
    r"|Chapter\s+[0-9]+)",
    re.IGNORECASE,
)
_PAGE_NO_RE = re.compile(r"^\s*[-—–]?\s*[0-9]{1,4}\s*[-—–]?\s*$|^\s*第\s*[0-9]{1,4}\s*页\s*$|^\s*[0-9]{1,4}\s*/\s*[0-9]{1,4}\s*$")


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
"""

_init_lock = threading.Lock()



def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """四张表都在才算就绪。加新表必须同步加进来：建表只在「不就绪」时跑，只判一部分的话
    老库永远进不了建表分支（同样的坑踩过三次）。
    """
    expected = {"documents", "chunks", "assets", "categories"}
    found = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('documents', 'chunks', 'assets', 'categories')"
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
    conn.commit()


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


class DocumentError(Exception):
    """可预期的失败：格式不支持、文件损坏、依赖缺失、超限。

    和「代码 bug」分开，因为这些错**要原样告诉用户**（写进 `documents.error`
    并在列表里显示），而不是吞掉或 500。
    """



def create_upload(user_id: str, filename: str, data: bytes) -> dict[str, object]:
    """收下一个文件，落盘并写一行 `pending`。返回这条记录（含是否重复）。

    去重按内容指纹，不按文件名。重复上传不覆盖原记录——已经解析好了，重解析一遍纯属浪费，
    还会让这期间的引用（chunk_id）全部失配；直接返回老记录并带 `duplicated=True`。
    """
    name = os.path.basename(str(filename or "").strip()) or "未命名"
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocumentError(f"不支持的文件类型：{suffix or '未知'}。目前支持 PDF、PPTX、DOCX。")
    if not data:
        raise DocumentError("文件是空的。")
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(f"文件太大（{len(data) / 1024 / 1024:.1f}MB），上限 {MAX_FILE_BYTES // 1024 // 1024}MB。")

    content_hash = hashlib.sha256(data).hexdigest()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? AND content_hash = ?",
            (user_id, content_hash),
        ).fetchone()
        if existing is not None:
            return {**_row_to_doc(existing), "duplicated": True}

        doc_id = _new_doc_id(conn)
        doc_dir = UPLOAD_DIR / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        file_path = doc_dir / f"source{suffix}"
        file_path.write_bytes(data)

        now = _now()
        conn.execute(
            "INSERT INTO documents (doc_id, user_id, title, filename, kind, status,"
            " page_count, chunk_count, asset_count, content_hash, file_path,"
            " size_bytes, toc, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,0,0,0,?,?,?,NULL,?,?)",
            (
                doc_id,
                user_id,
                Path(name).stem[:120] or "未命名教材",
                name,
                suffix.lstrip("."),
                STATUS_PENDING,
                content_hash,
                str(file_path),
                len(data),
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return {**_row_to_doc(row), "duplicated": False}


def _new_doc_id(conn: sqlite3.Connection) -> str:
    """8 位十六进制短 id。它同时是目录名和 chunk_id 前缀，短一点好读。

    碰撞概率极低（4 字节随机），但仍然显式查重——一旦撞了，两本书的目录会合并，
    这种错很难从现象反推回来。
    """
    for _ in range(20):
        candidate = secrets.token_hex(4)
        if conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (candidate,)).fetchone() is None:
            return candidate
    raise DocumentError("生成教材编号失败，请重试。")



@dataclass
class _Element:
    """切分前的中间形态：一段正文、一张图、或一个公式编号。

    `content` 对图/公式来说是**题注文本**——它不是正文，但必须留着：
    「图 3-4 抽样过程」这句话本身就是检索命中的抓手。
    """

    kind: str  # text / figure / formula
    content: str
    page: int
    y: float = 0.0
    asset_id: str | None = None
    label: str | None = None
    chapter: str | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class _Parsed:
    page_count: int
    elements: list[_Element] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    toc: list[list] = field(default_factory=list)
    warning: str | None = None
    section_per_page: bool = False


def parse_document(doc_id: str) -> dict[str, object]:
    """把一本 pending 的书解析完，返回最终记录。

    阻塞（CPU + 磁盘），调用方必须丢线程池。失败时写 `documents.error` 并置 failed，不抛异常
    （调用它的后台任务没人接）。产出 `documents.markdown` + 从它切出的 chunks——**切分只读
    Markdown**，所以调切分参数不用重新解析整本 PDF。
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if row is None:
        raise KeyError(doc_id)
    if row["status"] == STATUS_READY:
        return _row_to_doc(row)

    _set_status(doc_id, STATUS_PARSING)
    try:
        data = Path(row["file_path"]).read_bytes()
        asset_dir = ASSET_DIR / doc_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        kind = row["kind"]
        if kind == "pdf":
            parsed = _parse_pdf(data, asset_dir)
        elif kind == "pptx":
            parsed = _parse_pptx(data, asset_dir)
        elif kind == "docx":
            parsed = _parse_docx(data, asset_dir)
        else:  # pragma: no cover - 入库时已经卡过扩展名
            raise DocumentError(f"未知格式：{kind}")

        markdown = render_markdown(parsed.elements)
        chunks = cut_chunks(
            markdown, doc_id, section_per_page=(kind == "pptx" or parsed.section_per_page)
        )
        _store(doc_id, parsed, markdown, chunks)
    except DocumentError as exc:
        _fail(doc_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - 解析器内部的任何意外都要落成 failed
        _log.exception("解析失败 doc=%s", doc_id)
        _fail(doc_id, f"{type(exc).__name__}: {exc}")

    with _connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return _row_to_doc(row)


def rechunk(doc_id: str) -> dict[str, object]:
    """只重切、不重解析，用于调完切分参数后批量重跑。这是 Markdown 中间态带来的能力：
    解析要几十秒还要原文件，重切只要读一列文本 + 一遍正则。不碰 `assets`（图是解析产物，
    重切只改「怎么分段」）。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT markdown, status, kind FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        raise KeyError(doc_id)
    if row["status"] != STATUS_READY or not row["markdown"]:
        raise DocumentError("这本书还没有解析完成，先解析再重切。")

    chunks = cut_chunks(
        row["markdown"], doc_id, section_per_page=(row["kind"] == "pptx")
    )
    with _connect() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO chunks (chunk_id, doc_id, seq, kind, content, page_start,"
            " page_end, chapter, asset_id, char_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    c["chunk_id"],
                    doc_id,
                    c["seq"],
                    c["kind"],
                    c["content"],
                    c["page_start"],
                    c["page_end"],
                    c.get("chapter"),
                    c.get("asset_id"),
                    len(c["content"]),
                )
                for c in chunks
            ],
        )
        conn.execute(
            "UPDATE documents SET chunk_count = ?, updated_at = ? WHERE doc_id = ?",
            (len(chunks), _now(), doc_id),
        )
        conn.commit()
    return {"doc_id": doc_id, "chunk_count": len(chunks)}


def _set_status(doc_id: str, status: str, error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE documents SET status = ?, error = ?, updated_at = ? WHERE doc_id = ?",
            (status, error, _now(), doc_id),
        )
        conn.commit()


def _fail(doc_id: str, error: str) -> None:
    _set_status(doc_id, STATUS_FAILED, error[:500])


def _store(doc_id: str, parsed: _Parsed, markdown: str, chunks: list[dict]) -> None:
    """一次性写入章节、资产、段落。**先删后写**，这样重新解析不会留下旧段落。

    整件事在一个事务里：中途崩了要么全旧要么全新，不会出现「段落是新的、
    图还是上一版」这种半截状态——那种状态下引用会指向不存在的图。
    """
    now = _now()
    with _connect() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM assets WHERE doc_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO assets (asset_id, doc_id, kind, label, caption, page,"
            " file_path, width, height) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    a["asset_id"],
                    doc_id,
                    a["kind"],
                    a.get("label"),
                    a.get("caption"),
                    a.get("page", 0),
                    a["file_path"],
                    a.get("width", 0),
                    a.get("height", 0),
                )
                for a in parsed.assets
            ],
        )
        conn.executemany(
            "INSERT INTO chunks (chunk_id, doc_id, seq, kind, content, page_start,"
            " page_end, chapter, asset_id, char_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    c["chunk_id"],
                    doc_id,
                    c["seq"],
                    c["kind"],
                    c["content"],
                    c["page_start"],
                    c["page_end"],
                    c.get("chapter"),
                    c.get("asset_id"),
                    len(c["content"]),
                )
                for c in chunks
            ],
        )
        conn.execute(
            "UPDATE documents SET status = ?, error = NULL, warning = ?, page_count = ?,"
            " chunk_count = ?, asset_count = ?, toc = ?, markdown = ?, updated_at = ?"
            " WHERE doc_id = ?",
            (
                STATUS_READY,
                parsed.warning,
                parsed.page_count,
                len(chunks),
                len(parsed.assets),
                json.dumps(parsed.toc, ensure_ascii=False),
                markdown,
                now,
                doc_id,
            ),
        )
        conn.commit()


def reconcile_pending() -> int:
    """启动时把「解析中」但明显已经死掉的行置为失败，返回处理条数。
    判据：`status='parsing'` 且 `updated_at` 超时。刻意不动 `pending`——它可能只是排着队。
    """
    cutoff = (datetime.now() - timedelta(minutes=STALE_PARSING_MINUTES)).isoformat(timespec="seconds")
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE documents SET status = ?, error = ?, updated_at = ?"
            " WHERE status = ? AND updated_at < ?",
            (STATUS_FAILED, "解析中断（进程重启或超时），请重新上传。", _now(), STATUS_PARSING, cutoff),
        )
        conn.commit()
        return cursor.rowcount



def list_documents(user_id: str) -> list[dict[str, object]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_doc(row) for row in rows]



MAX_CATEGORY_NAME = 40


class CategoryExists(ValueError):
    """同一个人已经有同名分类。和 `AccountTaken` 一样用异常，让上层能区分
    「你填的东西不行」（→ 400）和系统故障。"""


def _clean_category_name(name: object) -> str:
    """折叠空白 + 截断。清洗后为空直接抛——空名字的分类在界面上没法点。"""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ValueError("分类名不能为空")
    if len(cleaned) > MAX_CATEGORY_NAME:
        raise ValueError(f"分类名不能超过 {MAX_CATEGORY_NAME} 个字")
    return cleaned


def list_categories(user_id: str) -> list[dict[str, object]]:
    """这个人的全部分类，带上每类有几本书，按创建先后排。
    用 `LEFT JOIN` 而不是先查分类再逐个查书数（N+1）；`d.user_id = c.user_id` 不能少，
    否则别人的书会让这个人的计数串过来。
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT c.category_id, c.name, c.created_at,
                   COUNT(d.doc_id) AS doc_count
              FROM categories c
              LEFT JOIN documents d
                     ON d.category_id = c.category_id AND d.user_id = c.user_id
             WHERE c.user_id = ?
             GROUP BY c.category_id
             ORDER BY c.created_at ASC, c.rowid ASC
            """,
            (str(user_id),),
        ).fetchall()
    return [
        {
            "category_id": row["category_id"],
            "name": row["name"],
            "doc_count": row["doc_count"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def create_category(user_id: str, name: object) -> dict[str, object]:
    """新建分类。重名抛 `CategoryExists`。

    靠唯一索引判重而不是「先查再插」：先查再插中间有窗口，两次并发新建会双双通过。
    """
    cleaned = _clean_category_name(name)
    category_id = f"c-{secrets.token_hex(4)}"
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO categories (category_id, user_id, name, created_at)"
                " VALUES (?, ?, ?, ?)",
                (category_id, str(user_id), cleaned, _now()),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise CategoryExists("已经有同名的分类了") from exc
    return {"category_id": category_id, "name": cleaned, "doc_count": 0}


def rename_category(category_id: str, user_id: str, name: object) -> str:
    """改分类名。返回 `"ok"` / `"missing"`（没这类）/ `"clash"`（改成重名了）。

    三个结果分开，是因为对用户的意义不同：404 该刷新，409 该换个名字。
    混成一句「改失败」会让人反复重试同一个名字。
    """
    cleaned = _clean_category_name(name)
    try:
        with _connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM categories WHERE category_id = ? AND user_id = ?",
                (category_id, str(user_id)),
            ).fetchone()
            if exists is None:
                return "missing"
            conn.execute(
                "UPDATE categories SET name = ? WHERE category_id = ? AND user_id = ?",
                (cleaned, category_id, str(user_id)),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return "clash"
    return "ok"


def delete_category(category_id: str, user_id: str) -> bool:
    """删分类，类下的书退回未分类、不跟着删，返回它此前是否存在。
    书是几十 MB 解析出来的贵重资产，分类只是标签——「删掉一个标签顺手删掉 8 本书」
    点错了就没法挽回。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM categories WHERE category_id = ? AND user_id = ?",
            (category_id, str(user_id)),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE documents SET category_id = NULL WHERE category_id = ? AND user_id = ?",
            (category_id, str(user_id)),
        )
        conn.execute(
            "DELETE FROM categories WHERE category_id = ? AND user_id = ?",
            (category_id, str(user_id)),
        )
        conn.commit()
    return True


def assign_category(doc_id: str, user_id: str, category_id: str | None) -> str:
    """把一本书挪到某个分类（`None` = 挪回未分类）。返回 ok / missing / no_category。
    书和分类两个 id 都要按 `user_id` 验一遍：只验书的话，别人可以把他的书挂到我的分类 id 上。
    """
    target = None if category_id is None else str(category_id)
    with _connect() as conn:
        doc = conn.execute(
            "SELECT 1 FROM documents WHERE doc_id = ? AND user_id = ?",
            (doc_id, str(user_id)),
        ).fetchone()
        if doc is None:
            return "missing"
        if target is not None:
            owned = conn.execute(
                "SELECT 1 FROM categories WHERE category_id = ? AND user_id = ?",
                (target, str(user_id)),
            ).fetchone()
            if owned is None:
                return "no_category"
        conn.execute(
            "UPDATE documents SET category_id = ? WHERE doc_id = ? AND user_id = ?",
            (target, doc_id, str(user_id)),
        )
        conn.commit()
    return "ok"


def get_document(doc_id: str, user_id: str) -> dict[str, object] | None:
    """取一本书的元数据。**越权一律返回 None**，由上层转 404。

    查询条件里带 `user_id` 而不是查完再比对：这样「别人的书」和「不存在的书」
    走的是同一条代码路径，不可能出现「漏了比对」的分支。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE doc_id = ? AND user_id = ?", (doc_id, user_id)
        ).fetchone()
    return _row_to_doc(row) if row else None


def delete_document(doc_id: str, user_id: str) -> bool:
    """删书：数据库行 + 原文件目录 + 图目录一起删，返回此前是否存在。
    先删库再删文件：反过来会留下「ready 但文件不存在」的悬空记录，之后每次检索都要处理。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id = ? AND user_id = ?", (doc_id, user_id)
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM assets WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        conn.commit()

    for directory in (UPLOAD_DIR / doc_id, ASSET_DIR / doc_id):
        if directory.is_dir():
            for path in sorted(directory.rglob("*"), reverse=True):
                try:
                    path.unlink() if path.is_file() else path.rmdir()
                except OSError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass
    return True


def chunks_of(doc_id: str) -> list[dict[str, object]]:
    """一本书的全部段落，按书中顺序。检索层拿它建索引。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, seq, kind, content, page_start, page_end, chapter, asset_id"
            " FROM chunks WHERE doc_id = ? ORDER BY seq",
            (doc_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def markdown_of(doc_id: str, user_id: str) -> str | None:
    """取一本书的 Markdown 全文。用于前端预览、导出、调试解析质量。

    单独一个函数而不是塞进 `get_document` 的返回值：一本书的正文有几百 KB，
    列表接口每次都带上它会把响应撑爆。**按需取**。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT markdown FROM documents WHERE doc_id = ? AND user_id = ?",
            (doc_id, user_id),
        ).fetchone()
    return row["markdown"] if row else None


def chunks_of_user(user_id: str) -> list[dict[str, object]]:
    """这个人的所有可用教材的全部段落，带上 doc_id / 书名。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT c.chunk_id, c.doc_id, c.seq, c.kind, c.content, c.page_start,"
            " c.page_end, c.chapter, c.asset_id, d.title AS doc_title"
            " FROM chunks c JOIN documents d ON d.doc_id = c.doc_id"
            " WHERE d.user_id = ? AND d.status = ? ORDER BY d.created_at, c.seq",
            (user_id, STATUS_READY),
        ).fetchall()
    return [dict(row) for row in rows]


def asset_of(asset_id: str, user_id: str) -> dict[str, object] | None:
    """取一张图。**三级越权检查**：asset → doc → user，一次 SQL 走完。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT a.asset_id, a.doc_id, a.kind, a.label, a.caption, a.page, a.file_path,"
            " a.width, a.height FROM assets a JOIN documents d ON d.doc_id = a.doc_id"
            " WHERE a.asset_id = ? AND d.user_id = ?",
            (asset_id, user_id),
        ).fetchone()
    return dict(row) if row else None


#     ![图 3-4 抽样过程的示意](as_77.png)

_PAGE_ANCHOR_RE = re.compile(r"^<!--\s*page:(\d+)\s*-->\s*$")
_CHAPTER_ANCHOR_RE = re.compile(r"^<!--\s*chapter:(.+?)\s*-->\s*$")
_IMAGE_LINE_RE = re.compile(r"^!\[(.*?)\]\(([^)]+)\)\s*$")

ASSET_REF_RE = re.compile(r"(!\[.*?\]\()(as_[0-9a-f]{4,})\.png(\))")

_BULLET_GLYPHS = "\uf000-\uf8ff➢➤►▶▸◆◇▪▫●○•·❖"

_BULLET_LINE_RE = re.compile("^[ \t\u200b]*[" + _BULLET_GLYPHS + "][ \t\u3000]*")


def normalize_bullets(text: str) -> str:
    """把行首条目符换成 Markdown 的 `- `。
    私用区那几个是方块、箭头占字符还干扰分词；统一之后正文列表和幻灯片要点在存储里是同一种
    形态，下游（检索 / 引用 / 导出）只认一种。
    """
    if not any(ch in text for ch in "➢➤►▶▸◆◇▪▫●○•·❖") and not any(
        "\uf000" <= ch <= "\uf8ff" for ch in text
    ):
        return text  # 绝大多数段落走这条快路，不做逐行正则
    return "\n".join(
        "- " + line[m.end() :] if (m := _BULLET_LINE_RE.match(line)) else line
        for line in text.splitlines()
    )



def render_markdown(elements: list[_Element]) -> str:
    """元素流 → 带锚点的 Markdown，解析的唯一出口。
    页码锚点每页打一次、章节锚点只在变化时打一次——每个元素都带会让文本一半是注释。
    """
    parts: list[str] = []
    last_page: int | None = None
    last_chapter: str | None = None
    for element in elements:
        if element.chapter and element.chapter != last_chapter:
            parts.append(f"<!-- chapter:{element.chapter} -->")
            last_chapter = element.chapter
        if element.page and element.page != last_page:
            parts.append(f"<!-- page:{element.page} -->")
            last_page = element.page
        if element.kind in {"figure", "formula"} and element.asset_id:
            parts.append(f"![{element.content}]({element.asset_id}.png)")
            continue
        if element.kind == "code":
            parts.append(f"```{element.label or ''}\n{element.content.rstrip()}\n```")
            continue
        body = normalize_bullets(element.content.strip())
        if body:
            parts.append(body)
    return "\n\n".join(parts)


def markdown_elements(markdown: str) -> list[_Element]:
    """Markdown → 元素流，`render_markdown` 的逆过程。
    刻意不做任何智能判断（不猜标题、不合并段落），「怎么切」全留给 `cut_chunks`，两步各自可测。
    """
    elements: list[_Element] = []
    page = 0
    chapter: str | None = None
    buffer: list[str] = []
    buffer_kind = "text"

    def flush() -> None:
        nonlocal buffer, buffer_kind
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                elements.append(
                    _Element(kind=buffer_kind, content=text, page=page, chapter=chapter)
                )
        buffer = []
        buffer_kind = "text"

    in_code = False
    code_lang = ""
    for raw in markdown.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("```"):
            if in_code:
                text = "\n".join(buffer)
                if text.strip():
                    elements.append(
                        _Element(
                            kind="code",
                            content=text,
                            page=page,
                            chapter=chapter,
                            label=code_lang or None,
                        )
                    )
                buffer = []
                code_lang = ""
                in_code = False
            else:
                flush()
                code_lang = stripped[3:].strip()
                in_code = True
            continue
        if in_code:
            buffer.append(raw.rstrip())
            continue

        line = raw.rstrip()
        anchor = _PAGE_ANCHOR_RE.match(line)
        if anchor:
            flush()
            page = int(anchor.group(1))
            continue
        anchor = _CHAPTER_ANCHOR_RE.match(line)
        if anchor:
            flush()
            chapter = anchor.group(1)
            continue
        if not line.strip():
            flush()
            continue

        image = _IMAGE_LINE_RE.match(line)
        if image:
            flush()
            alt, path = image.group(1), image.group(2)
            asset_id = path.rsplit(".", 1)[0]
            elements.append(
                _Element(
                    kind="formula" if alt.startswith("式") else "figure",
                    content=alt,
                    page=page,
                    chapter=chapter,
                    asset_id=asset_id,
                )
            )
            continue

        if line.startswith("|"):
            if buffer_kind != "table":
                flush()
                buffer_kind = "table"
            buffer.append(line)
            continue

        if buffer_kind == "table":
            flush()
        buffer.append(line)
    if in_code and buffer:
        text = "\n".join(buffer)
        if text.strip():
            elements.append(
                _Element(
                    kind="code",
                    content=text,
                    page=page,
                    chapter=chapter,
                    label=code_lang or None,
                )
            )
        buffer = []
    flush()
    return elements



def cut_chunks(markdown: str, doc_id: str, *, section_per_page: bool = False) -> list[dict]:
    """Markdown → 段落列表，只读文本、不碰解析器。
    `section_per_page`（每页必须断开，只有 PPT 用）是切分策略而不是元素属性——挂在元素上
    过一遍 `render_markdown` 就丢了，PPT 的三页又会被合并成一条。
    """
    return _cut_chunks(markdown_elements(markdown), doc_id, section_per_page=section_per_page)


def _cut_chunks(
    elements: list[_Element], doc_id: str, *, section_per_page: bool = False
) -> list[dict]:
    """元素流 → 段落列表。单遍扫描，三条规则按顺序生效：

    1. 图/公式独占一条（自带 asset 引用，混进正文会让引用位置失真）；
    2. 长度够了（60% TARGET）之后章变了才断——不能章一变就断，否则短小节被切成一堆
       没有上下文的碎块；
    3. 到 TARGET 且刚好在句末就断，到 MAX 无条件断（尽量在句号处断，别给半句话）。

    表格是原子块：不能被 _split_long 切开（markdown 表格拦腰截断就是乱码），放不下就 flush。
    """
    chunks: list[dict] = []
    buffer: list[_Element] = []
    buffer_len = 0
    current_chapter: str | None = None
    current_page = 0
    chapter_cut = int(TARGET_CHARS * 0.6)

    def flush() -> None:
        nonlocal buffer, buffer_len
        if not buffer:
            return
        text = _join_elements(buffer)
        if text.strip():
            kind = "table" if all(item.kind == "table" for item in buffer) else "text"
            chunks.append(_make_chunk(doc_id, chunks, buffer, text, kind=kind))
        buffer = []
        buffer_len = 0

    for element in elements:
        if element.kind in {"figure", "formula"}:
            flush()
            chunks.append(
                _make_chunk(doc_id, chunks, [element], element.content, kind=element.kind)
            )
            continue

        if element.kind == "table":
            flush()
            chunks.append(
                _make_chunk(doc_id, chunks, [element], element.content, kind="table")
            )
            continue

        if element.kind == "code":
            flush()
            chunks.append(
                _make_chunk(doc_id, chunks, [element], element.content, kind="code")
            )
            continue

        if section_per_page and element.page != current_page and buffer:
            flush()
            current_page = element.page
        current_page = element.page

        if element.chapter != current_chapter:
            if buffer_len >= chapter_cut:
                flush()
            current_chapter = element.chapter

        for piece in _split_long(element.content):
            piece_element = _Element(
                kind="text",
                content=piece,
                page=element.page,
                chapter=element.chapter,
            )
            buffer.append(piece_element)
            buffer_len += len(piece)
            if buffer_len >= TARGET_CHARS and piece.rstrip().endswith(tuple(_SENTENCE_END)):
                flush()
            elif buffer_len >= MAX_CHARS:
                flush()
    flush()
    return _merge_short(chunks)


def _merge_short(chunks: list[dict]) -> list[dict]:
    """把过短的正文片段并进前一条，然后重排序号。
    短片段（图前最后一句、章尾几行）单独成条时检索能命中但答不出东西，还白占引用编号。
    只合并正文：图的 content 是题注，并进正文会让 asset 引用出现在一段无关的文字里；跨章不合并。
    """
    merged: list[dict] = []
    for chunk in chunks:
        if (
            merged
            and chunk["kind"] == "text"
            and merged[-1]["kind"] == "text"
            and len(chunk["content"]) < MIN_CHARS
            and len(merged[-1]["content"]) + len(chunk["content"]) <= MAX_CHARS
            and merged[-1]["chapter"] == chunk["chapter"]
        ):
            previous = merged[-1]
            previous["content"] = f"{previous['content']}\n\n{chunk['content']}"
            previous["page_end"] = max(previous["page_end"], chunk["page_end"])
            continue
        merged.append(chunk)

    for index, chunk in enumerate(merged):
        chunk["seq"] = index
        chunk["chunk_id"] = chunk["chunk_id"].rsplit("-", 1)[0] + f"-{index:04d}"
    return merged


def _make_chunk(
    doc_id: str,
    existing: list[dict],
    elements: list[_Element],
    text: str,
    *,
    kind: str = "text",
) -> dict:
    seq = len(existing)
    return {
        "chunk_id": f"{doc_id}-{seq:04d}",
        "seq": seq,
        "kind": kind,
        "content": text.strip(),
        "page_start": min((e.page for e in elements), default=0),
        "page_end": max((e.page for e in elements), default=0),
        "chapter": next((e.chapter for e in elements if e.chapter), None),
        "asset_id": next((e.asset_id for e in elements if e.asset_id), None),
    }


def _join_elements(elements: list[_Element]) -> str:
    """把同一条 chunk 里的段落拼起来。段间空一行——它是 markdown 的段落分隔，
    模型读起来也更容易分清哪里开始是新的一段。"""
    parts = [e.content.strip() for e in elements if e.content.strip()]
    return "\n\n".join(parts)


def _split_long(text: str) -> list[str]:
    """超过 MAX_CHARS 的段落按句切开。切点优先句末标点，其次逗号，最后硬切。"""
    if len(text) <= MAX_CHARS:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > MAX_CHARS:
        window = rest[:MAX_CHARS]
        cut = max(window.rfind(ch) for ch in _SENTENCE_END)
        if cut < MAX_CHARS // 2:
            cut = max(window.rfind(ch) for ch in "，,、")
        if cut < MAX_CHARS // 2:
            cut = MAX_CHARS
        pieces.append(rest[: cut + 1])
        rest = rest[cut + 1 :]
    if rest.strip():
        pieces.append(rest)
    return pieces



def _require(module: str, hint: str):
    """延迟 import 第三方解析库。缺了给出**能照做的**提示，而不是 ModuleNotFoundError。"""
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise DocumentError(f"缺少解析依赖 {module}，请先安装：{hint}") from exc


def _looks_two_column(blocks: list, width: float) -> bool:
    """这一页是不是双栏：看文字块左边缘有没有明显分界缝，不看「有没有两列文字」。

    门槛缺一不可：块数 ≥ 6（太少时居中图注就能造出间隙）、间隙 > 页宽 15%、
    左边贴页边距且右边落在右半页（排除「居中大标题 + 正文」）。
    """
    if len(blocks) < 6:
        return False
    xs = sorted(float(block[0]) for block in blocks)
    gap, split = 0.0, None
    for left, right in zip(xs, xs[1:]):
        if right - left > gap:
            gap, split = right - left, (left, right)
    if split is None or gap < width * 0.15:
        return False
    left_count = sum(1 for x in xs if x <= split[0])
    if left_count < 2 or len(xs) - left_count < 2:
        return False
    return split[0] < width * 0.25 and split[1] > width * 0.4


def _pdf_preflight(pdf, pymupdf) -> dict[str, object]:
    """解析前体检，返回 text_ratio（有文字层的页占比）和 columns（1 或 2）。

    必须先探再解析：这两个条件不成立时解析器不报错，会安安静静产出一份读不通的文本、
    状态还是 ready（3 页纯图 PDF 得到「已就绪 · 0 段」）。宁可拒绝，也别给一份假成品。
    """
    pages = int(pdf.page_count)
    if pages <= 0:
        return {"text_ratio": 0.0, "columns": 1, "probed": 0}

    start = min(2, pages - 1)
    span = pages - start
    wanted = min(5, span)
    indexes = sorted({
        start + round(i * (span - 1) / max(wanted - 1, 1)) for i in range(wanted)
    })
    with_text = 0
    two_column = 0
    for index in indexes:
        page = pdf[index]
        blocks = [block for block in page.get_text("blocks") if block[4].strip()]
        if blocks:
            with_text += 1
        if _looks_two_column(blocks, page.rect.width):
            two_column += 1

    slides = False
    if pages >= 3:
        rect = pdf[0].rect
        if rect.height > 0 and rect.width / rect.height > 1.1:
            chars = sorted(
                len(pdf[index].get_text("text").strip()) for index in indexes
            )
            slides = chars[len(chars) // 2] < 500

    return {
        "text_ratio": with_text / len(indexes),
        "columns": 2 if two_column * 2 >= len(indexes) else 1,
        "probed": len(indexes),
        "slides": slides,
    }


def _parse_pdf(data: bytes, asset_dir: Path) -> _Parsed:
    pymupdf = _require("pymupdf", "pip install pymupdf")
    pdf = pymupdf.open(stream=data, filetype="pdf")
    try:
        page_count = pdf.page_count
        toc = pdf.get_toc(simple=True) or []
        junk = _pdf_junk_lines(pdf)
        parsed = _Parsed(page_count=page_count, toc=toc)

        preflight = _pdf_preflight(pdf, pymupdf)
        if float(preflight["text_ratio"]) < 0.2:
            raise DocumentError(
                f"抽样的 {preflight['probed']} 页里只有 "
                f"{float(preflight['text_ratio']):.0%} 有文字层，基本可以确定是扫描版"
                "（整页都是图片）。当前版本不做 OCR，请换一本带文字层的 PDF。"
            )
        if preflight["slides"]:
            parsed.section_per_page = True
        if int(preflight["columns"]) == 2:
            parsed.warning = (
                "检测到双栏排版，当前按单栏解析，跨栏的段落顺序可能错乱；"
                "正文仍可检索，但引用时请核对原文"
            )

        has_toc = any(len(entry) >= 3 and isinstance(entry[2], int) for entry in toc)
        body_size = _pdf_body_size(pdf)
        heading_size = 0.0 if has_toc else body_size
        body_left = _pdf_body_left(pdf)
        chapter: str | None = None
        template_images = (
            frozenset(_pdf_template_images(pdf)) if parsed.section_per_page else frozenset()
        )

        for page_index in range(page_count):
            page = pdf[page_index]
            page_no = page_index + 1
            lines = _pdf_page_lines(page, junk, tight_cross_block=parsed.section_per_page)
            if has_toc:
                from_toc = _chapter_at(toc, page_no)
                if from_toc:
                    chapter = from_toc

            elements, chapter = _pdf_page_elements(
                parsed,
                page,
                page_no,
                lines,
                chapter,
                asset_dir,
                body_size=body_size,
                body_left=body_left,
                heading_size=heading_size,
                skip_images=template_images,
            )
            parsed.elements.extend(elements)
        parsed.toc = [
            [int(entry[0]), str(entry[1]), int(entry[2])]
            for entry in toc
            if len(entry) >= 3 and isinstance(entry[2], int)
        ]
        return parsed
    finally:
        pdf.close()


def _pdf_template_images(pdf, ratio: float = 0.3) -> set[int]:
    """反复出现在大多数页上的位图：母版背景、校徽、装饰条。
    当成插图抽出来等于每页凭空多一张没有信息的图。真正的内容图只出现在一两页，
    按「出现页数占全本的比例」就能分开，不需要认图。
    """
    counts: Counter[int] = Counter()
    for page_index in range(pdf.page_count):
        try:
            xrefs = {int(image[0]) for image in pdf[page_index].get_images(full=True)}
        except Exception:  # noqa: BLE001 - 畸形 PDF 的图字典可能是空的
            continue
        counts.update(xrefs)
    if pdf.page_count < 3:
        return set()
    limit = max(2.0, pdf.page_count * ratio)
    return {xref for xref, pages in counts.items() if pages >= limit}


def _pdf_body_size(pdf, sample_pages: int = 40) -> float:
    """正文字号：抽前若干页，取出现次数最多的字号。

    只抽前 40 页是为了快——全本扫一遍在 300 页的书上要多花一秒，
    而字号在一本书里是全局一致的，抽样足够。
    """
    counter: Counter[float] = Counter()
    for page_index in range(min(sample_pages, pdf.page_count)):
        page = pdf[page_index]
        for line in _pdf_raw_lines(page):
            text = line[4].strip()
            if len(text) >= 8:  # 太短的行（标题、页码）不参与统计
                counter[round(line[5], 1)] += 1
    if not counter:
        return 0.0
    return counter.most_common(1)[0][0]


def _pdf_body_left(pdf, sample_pages: int = 40) -> float:
    """正文左边缘：抽若干页取出现次数最多的行左边缘（一本书的正文列全局一致）。
    用来分开代码块和行内代码：代码块整体比正文右移一个内边距，行内代码就贴在正文列上。
    """
    counter: Counter[float] = Counter()
    for page_index in range(min(sample_pages, pdf.page_count)):
        for line in _pdf_raw_lines(pdf[page_index]):
            if len(line[4].strip()) >= 8:
                counter[round(line[0], 0)] += 1
    if not counter:
        return 0.0
    return counter.most_common(1)[0][0]


_MONO_HINTS = (
    "menlo", "mono", "consol", "courier", "code", "hack", "fira",
    "jetbrains", "cascadia", "sarasa", "inconsolata", "sourcecodepro",
)


def _mono_share(line: dict) -> float:
    """一行里等宽字体占的字符比例。
    不能按「主字体」判断：两本书的正文恰好相反（一个 Type3 碎字体、一个 NotoSansSC 整块覆
    中文），按比例算才不会两个方向都踩。
    """
    mono = total = 0
    for span in line.get("spans", []):
        count = len(span.get("text", ""))
        total += count
        if any(hint in span.get("font", "").lower() for hint in _MONO_HINTS):
            mono += count
    return (mono / total) if total else 0.0


_OPEN_BRACKETS = "([{"
_BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))


def _unbalanced(code: str) -> bool:
    text = code.replace('"""', "").replace("'''", "")
    if any(text.count(opening) != text.count(closing) for opening, closing in _BRACKET_PAIRS):
        return True
    return text.count('"') % 2 == 1


_HEADING_STRONG_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百]+\s*[章篇部]"
    r"|\d+\s*\.\s*\d+"
    r"|Chapter\s+\d+)",
    re.IGNORECASE,
)
_PANEL_LABEL_RE = re.compile(
    r"^\s*(Tip|Note|Question|Warning|Abstract|Summary|Example|Attention)\b", re.IGNORECASE
)


def _looks_heading(text: str, size: float, body_size: float) -> bool:
    """没有目录时判断一行是不是章节标题。

    只认编号形式（「第 3 章」「2.3.1」）。靠字号猜会造出大量假标题（提示框标签、表注、
    公式行、编号列表项），危害是它后面的正文全归到它名下，引用时「出自哪一节」整段错。
    无编号的必须字号明显更大且极短才收（兜住「尾递归」这类二级标题）。
    """
    if body_size <= 0 or size < body_size * 1.12:
        return False
    stripped = text.strip()
    if not (2 <= len(stripped) <= 40):
        return False
    if "，" in stripped or "。" in stripped:
        return False
    if stripped.endswith(("。", "！", "？", "，", "、", "；", "：", ":", ")", "）")):
        return False
    if _PANEL_LABEL_RE.match(stripped):
        return False
    if _match_caption(stripped) is not None:
        return False
    if re.search(r"[=<>≤≥]", stripped) and not re.search(r"[\u4e00-\u9fff]{2,}", stripped):
        return False
    if re.match(r"^[‧·•●○▪◆\-—*=+]", stripped):
        return False
    if _PAGE_NO_RE.match(stripped):
        return False
    if _HEADING_STRONG_RE.match(stripped):
        return True
    if re.match(r"^\d+\s*[.、)]", stripped):
        return False
    return size >= body_size * 1.3 and len(stripped) <= 16


def _norm_line(text: str) -> str:
    """页眉页脚比对用的归一化：数字变 `#`、去空白。

    这样「第 3 章 抽样定理 12」和「第 3 章 抽样定理 13」会被认成同一行，
    而不同页的页码 `12` / `13` 也归一化成同一个 `#`——两者都正是要删的。
    """
    return re.sub(r"\s+", "", re.sub(r"[0-9０-９]+", "#", text))


def _pdf_junk_lines(pdf) -> set[str]:
    """找出页眉页脚的文本（归一化后），供逐页过滤。

    判据是「在同侧边缘反复出现」。单页出现一次的边缘文本不算——很多教材首页
    顶部是书名，只有一页，删了会丢信息。所以阈值是 `max(3, 25% 页数)`。
    """
    counter: Counter[str] = Counter()
    pages = pdf.page_count
    for page_index in range(pages):
        page = pdf[page_index]
        height = page.rect.height
        for line in _pdf_raw_lines(page):
            y0, y1 = line[1], line[3]
            if y1 <= height * 0.09 or y0 >= height * 0.91:
                counter[_norm_line(line[4])] += 1
    threshold = max(3, int(pages * 0.25))
    return {key for key, count in counter.items() if count >= threshold}


def _sanitize_surrogates(text: str) -> str:
    """修复 PDF ToUnicode CMap 缺页产生的孤立 UTF-16 代理对。
    数学字母（U+1D400-U+1D7FF）在坏映射下被拆成高低两个孤立代理码点，str 里合法、
    encode 成 UTF-8 就 UnicodeEncodeError，整本书解析失败。能配对的重新合成，配不上的丢弃。
    """
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        o = ord(text[i])
        if 0xD800 <= o <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(text[i + 1]) <= 0xDFFF:
            out.append(chr(0x10000 + ((o - 0xD800) << 10) + (ord(text[i + 1]) - 0xDC00)))
            i += 2
            continue
        if not (0xD800 <= o <= 0xDFFF):
            out.append(text[i])
        i += 1
    return "".join(out)


def _sanitize_page_dict(data: dict) -> dict:
    """对 `get_text("dict")` 的结果就地清洗所有 span 文本。"""
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if "text" in span:
                    span["text"] = _sanitize_surrogates(span["text"])
    return data


def _pdf_raw_lines(page) -> list[tuple[float, float, float, float, str, float]]:
    """一页的所有文本行：`(x0, y0, x1, y1, text, max_font_size)`。"""
    result = []
    data = _sanitize_page_dict(page.get_text("dict"))
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            text = text.replace("\u200b", "").replace("\ufeff", "")
            if not text.strip():
                continue
            bbox = line.get("bbox", (0, 0, 0, 0))
            size = max((float(span.get("size", 0)) for span in line.get("spans", [])), default=0.0)
            result.append((bbox[0], bbox[1], bbox[2], bbox[3], text, size))
    return result


def _dominant_size(line: dict) -> float:
    """一行的主字号：按字符数取最多的那个 span 的字号。
    不能用 `max()`：正文夹一个公式字符就把整行字号抬上去，而段落合并靠字号相同判「同一段」，
    一抬高就和相邻行对不上，一句话被切成两段。
    """
    counter: Counter[float] = Counter()
    for span in line.get("spans", []):
        counter[round(float(span.get("size", 0)), 1)] += len(span.get("text", ""))
    return counter.most_common(1)[0][0] if counter else 0.0


_SCRIPT_TEXT_RE = re.compile(r"[0-9A-Za-z+\-−=/.]{1,4}")

_SCRIPT_BASE_RE = re.compile(r"[0-9A-Za-z\u0370-\u03ff\U0001D400-\U0001D7FF)]")


def _is_superscript(span: dict, previous: dict, baseline: float, base_size: float) -> bool:
    """这个 span 是不是前一个字符的上标。三条几何特征同时成立：

    - 字号比前一个 span 小：比值 ≤ 0.85（和前一个 span 比，不能和行主字号比）；
    - 基线比本行主基线高：≥ 0.25 × 主字号；
    - 与前一个 span 水平紧贴：间隙 ≤ 0.15 × 主字号。
    """
    text = span.get("text", "").strip()
    if not text or not _SCRIPT_TEXT_RE.fullmatch(text):
        return False
    if base_size <= 0:
        return False
    size = float(span.get("size", 0.0))
    previous_size = float(previous.get("size", 0.0))
    if previous_size <= 0 or size > previous_size * 0.85:
        return False
    rise = baseline - float(span.get("origin", (0.0, 0.0))[1])
    if rise < 0.25 * base_size:
        return False
    gap = float(span.get("bbox", (0, 0, 0, 0))[0]) - float(
        previous.get("bbox", (0, 0, 0, 0))[2]
    )
    if gap > 0.15 * base_size:
        return False
    previous_text = previous.get("text", "").rstrip()
    return bool(previous_text) and bool(_SCRIPT_BASE_RE.fullmatch(previous_text[-1]))


def _line_text(line: dict) -> str:
    """一行的文本：span 按顺序拼接，上标还原成 `^x`。
    `O(n2)` 和 `O(n²)` 提取出来都是 `O(n2)`，读的人、检索、模型都分不出。单字符不加括号，
    多字符用 `^(2i/d)` 括起来，避免 `10000^2i/d` 这种「谁是上标」的歧义。
    """
    spans = line.get("spans", [])
    if not spans:
        return ""
    base_size = _dominant_size(line)
    baseline_counter: Counter[float] = Counter()
    for span in spans:
        baseline_counter[round(float(span.get("origin", (0.0, 0.0))[1]), 1)] += len(
            span.get("text", "")
        )
    baseline = baseline_counter.most_common(1)[0][0] if baseline_counter else 0.0

    parts: list[str] = []
    previous = None
    for span in spans:
        text = span.get("text", "")
        if text.strip() and previous is not None and _is_superscript(
            span, previous, baseline, base_size
        ):
            parts.append("^" + (text.strip() if len(text.strip()) == 1 else f"({text.strip()})"))
        else:
            parts.append(text)
        if text.strip():
            previous = span
    return "".join(parts)


def _pdf_page_lines(
    page, junk: set[str], tight_cross_block: bool = False
) -> list[tuple[float, float, float, float, str, float, int, bool]]:
    """过滤页眉页脚和纯页码，给每行带上 block 序号与「是否等宽字体」。
    同一基线上的多段要先合并成一行：行尾注释在 PDF 里是两个独立 line 对象（y 相同、x 不同），
    不合并会按 x 算出几十个空格的缩进。
    """
    height = page.rect.height
    data = _sanitize_page_dict(page.get_text("dict"))
    lines: list[list] = []
    for block_index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = _line_text(line)
            text = text.replace("\u200b", "").replace("\ufeff", "")
            if not text.strip():
                continue
            bbox = line.get("bbox", (0, 0, 0, 0))
            y0, y1 = bbox[1], bbox[3]
            at_edge = y1 <= height * 0.09 or y0 >= height * 0.91
            if at_edge and (_norm_line(text) in junk or _PAGE_NO_RE.match(text)):
                continue
            size = _dominant_size(line)
            lines.append(
                [bbox[0], y0, bbox[2], y1, text, size, block_index, _mono_share(line)]
            )

    lines.sort(key=lambda item: (round(item[1], 1), item[0]))

    bands: list[list[list]] = []
    for item in lines:
        if bands and _same_band(bands[-1], item):
            bands[-1].append(item)
        else:
            bands.append([item])

    out: list[tuple] = []
    for band in bands:
        for visual_row in _split_by_gap(band, tight_cross_block):
            out.append(_merge_row(visual_row))
    return out


def _same_band(band: list[list], item: list) -> bool:
    """是否落在同一个「行带」里（只比 y，不比 x）。

    0.7 倍字号是从上下标量出来的：变量比正文大一号、下标再往下沉，最远到
    5.6pt（字号 9.8 时约 0.57 倍）；而相邻正文行的行距在 17pt 以上。
    """
    top = min(entry[1] for entry in band)
    size = max((entry[5] for entry in band), default=0.0)
    return item[1] - top < 0.7 * max(size, 1.0)


def _split_by_gap(band: list[list], tight_cross_block: bool = False) -> list[list[list]]:
    """行带内按 x 切成视觉行：间隙超过阈值就断开（默认 2 倍字号）。

    2 倍够用：上下标留的空位约 1.4 倍要接上，并排元素的间隙是几倍到几十倍。
    tight_cross_block 只给幻灯片用，阈值收到 0.35 倍——图内标注和正文挤在同一基线，
    2 倍会把它吞进句子；正文书不能收，Word 的行内公式两侧各留 0.5~1 倍，收了句子就断。
    """
    band.sort(key=lambda entry: (entry[0], entry[1]))
    groups: list[list[list]] = [[band[0]]] if band else []
    for entry in band[1:]:
        previous = groups[-1][-1]
        size = max(entry[5], 1.0)
        gap = max(entry[0] - previous[2], previous[0] - entry[2])
        limit = 0.35 if tight_cross_block and entry[6] != previous[6] else 2.0
        if gap < limit * size:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def _merge_row(row: list[list]) -> tuple:
    """一个视觉行内的片段 → 一行（按 x 拼接）。"""
    row.sort(key=lambda entry: (entry[0], entry[1]))
    parts: list[str] = []
    previous_end: float | None = None
    for entry in row:
        if previous_end is not None:
            gap = entry[0] - previous_end
            if gap > 0.6 * max(entry[5], 1.0) and parts and not parts[-1].endswith(
                (" ", "\u3000")
            ):
                parts.append(" ")
        parts.append(entry[4])
        previous_end = entry[2] if previous_end is None else max(previous_end, entry[2])

    weights: Counter[float] = Counter()
    mono_total = 0
    char_total = 0
    for entry in row:
        count = len(entry[4])
        weights[entry[5]] += count
        mono_total += entry[7] * count
        char_total += count
    size = weights.most_common(1)[0][0] if weights else 0.0
    mono = (mono_total / char_total) if char_total else 0.0

    first = row[0]
    return (
        min(entry[0] for entry in row),
        min(entry[1] for entry in row),
        max(entry[2] for entry in row),
        max(entry[3] for entry in row),
        "".join(parts),
        size,
        first[6],
        mono,
    )


def _pdf_page_elements(
    parsed: _Parsed,
    page,
    page_no: int,
    lines: list[tuple[float, float, float, float, str, float, int, bool]],
    chapter: str | None,
    asset_dir: Path,
    *,
    body_size: float = 0.0,
    body_left: float = 0.0,
    heading_size: float = 0.0,
    skip_images: frozenset = frozenset(),
) -> tuple[list[_Element], str | None]:
    """一页 → 元素流（正文段落 + 表格 + 图 + 公式编号），按版面从上到下。
    图和公式要插回版面上的位置，不能统一堆到页尾——回答里「如图 3-4」的位置就该是那张图的
    位置。返回 (元素, 本页最后的章节名)：章节是跨页连续的状态，不能按页各算各的。
    """
    elements: list[_Element] = []

    table_rects: list[tuple[float, float, float, float]] = []
    for markdown_table, bbox in _pdf_tables(page):
        table_rects.append(bbox)
        elements.append(
            _Element(
                kind="table",
                content=markdown_table,
                page=page_no,
                y=bbox[1],
                chapter=chapter,
            )
        )

    figure_rects = _pdf_graphic_rects(page)
    consumed: set[tuple[float, float, str]] = set()
    for line in lines:
        x0, y0, x1, y1, text, _size, _block, _mono = line
        caption = _match_caption(text)
        if caption is None:
            continue
        label, caption_text = caption
        if label.startswith("表"):
            continue
        rect = (
            _slide_figure_rect(figure_rects, (x0, y0, x1, y1), page)
            if parsed.section_per_page
            else _nearest_graphic_above(figure_rects, (x0, y0, x1, y1))
        )
        if rect is None:
            continue
        asset = _save_clip(page, rect, asset_dir, parsed, label, caption_text, page_no, "figure")
        if asset is None:
            continue
        consumed.add((x0, y0, text))
        elements.append(
            _Element(
                kind="figure",
                content=_figure_chunk_text(label, caption_text),
                page=page_no,
                y=rect.y0,
                asset_id=asset["asset_id"],
                label=label,
                chapter=chapter,
                bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
            )
        )
        figure_rects = _drop_inside(figure_rects, rect)

    figure_boxes = [item.bbox for item in elements if item.kind == "figure" and item.bbox]
    body_lines = [
        line
        for line in lines
        if (line[0], line[1], line[4]) not in consumed
        and not (table_rects and _center_in(line[:4], table_rects))
        and not (
            parsed.section_per_page
            and figure_boxes
            and _is_slide_label(line, figure_boxes)
        )
    ]

    def is_code(line) -> bool:
        if body_left <= 0 and body_size <= 0:
            return False
        return _is_code_line(line, body_left, body_size)

    ordered: list[_Element] = []
    rejected: list = []
    cursor = 0
    while cursor < len(body_lines):
        if not is_code(body_lines[cursor]):
            cursor += 1
            continue
        block: list = []
        while cursor < len(body_lines) and is_code(body_lines[cursor]):
            block.append(body_lines[cursor])
            cursor += 1
        if not _looks_like_code(block, body_size):
            rejected.extend(block)
            continue
        code = _code_block_text(block)
        if code:
            ordered.append(
                _Element(
                    kind="code",
                    content=code,
                    page=page_no,
                    y=min(item[1] for item in block),
                    chapter=chapter,
                    label=_code_language(code) or None,
                )
            )

    prose_lines = sorted(
        [line for line in body_lines if not is_code(line)] + rejected,
        key=lambda item: (round(item[1], 1), item[0]),
    )
    if parsed.section_per_page:
        paragraphs = _ppt_paragraphs(prose_lines)
    else:
        paragraphs = _pdf_paragraphs(prose_lines)

    for group in paragraphs:
        text = _join_lines([item[4] for item in group])
        if not text.strip():
            continue
        y0 = min(item[1] for item in group)
        size = max(item[5] for item in group)
        if _looks_heading(text, size, heading_size):
            chapter = text.strip()[:80]
        ordered.append(
            _Element(kind="text", content=text, page=page_no, y=y0, chapter=chapter)
        )
    elements.extend(ordered)

    if (
        parsed.section_per_page
        and not any(item.kind == "figure" for item in elements)
        and sum(len(line[4]) for line in body_lines) < _PPT_FIGURE_MAX_CHARS
        and (
            _pdf_graphic_cover(page) >= _PPT_FIGURE_COVER
            or (
                sum(len(line[4]) for line in body_lines) < _PPT_FIGURE_MIN_CHARS
                and _pdf_shape_count(page) >= _PPT_FIGURE_MIN_SHAPES
            )
        )
    ):
        whole = page.rect  # pymupdf 的 Rect，整页范围
        label = f"第 {page_no} 页 整页图示"
        asset = _save_clip(page, whole, asset_dir, parsed, label, "", page_no, "figure")
        if asset is not None:
            elements.append(
                _Element(
                    kind="figure",
                    content=_figure_chunk_text(label, ""),
                    page=page_no,
                    y=whole.y0,
                    asset_id=asset["asset_id"],
                    label=label,
                    chapter=chapter,
                    bbox=(whole.x0, whole.y0, whole.x1, whole.y1),
                )
            )

    if (
        parsed.section_per_page
        and skip_images is not None
        and not any(item.kind == "figure" for item in elements)
    ):
        page_area = page.rect.width * page.rect.height
        picked: list[int] = []
        try:
            images = page.get_images(full=True)
        except Exception:  # noqa: BLE001 - 畸形 PDF 的图字典可能是空的
            images = []
        for image in images:
            xref = int(image[0])
            if xref in skip_images or xref in picked:
                continue
            for rect in page.get_image_rects(xref):
                if page_area <= 0 or rect.width * rect.height < 0.06 * page_area:
                    continue
                label = f"第 {page_no} 页 插图"
                asset = _save_clip(page, rect, asset_dir, parsed, label, "", page_no, "figure")
                if asset is None:
                    continue
                picked.append(xref)
                elements.append(
                    _Element(
                        kind="figure",
                        content=_figure_chunk_text(label, ""),
                        page=page_no,
                        y=rect.y0,
                        asset_id=asset["asset_id"],
                        label=label,
                        chapter=chapter,
                        bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                    )
                )
                break

    for line in lines:
        x0, y0, x1, y1, text, _size, _block, _mono = line
        match = _FORMULA_NO_RE.match(text)
        if match is None:
            continue
        rect = _formula_rect(page, (x0, y0, x1, y1))
        label = f"式 ({match.group(1).replace(' ', '')})"
        asset = _save_clip(page, rect, asset_dir, parsed, label, "", page_no, "formula")
        if asset is None:
            continue
        elements.append(
            _Element(
                kind="formula",
                content=label,
                page=page_no,
                y=rect.y0,
                asset_id=asset["asset_id"],
                label=label,
                chapter=chapter,
                bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
            )
        )

    if parsed.section_per_page:
        elements = _ppt_page_order(elements)
    else:
        elements.sort(key=lambda item: item.y)

    anchors = [
        (item.y, item.chapter) for item in elements if item.kind == "text" and item.chapter
    ]
    for item in elements:
        if item.kind == "text" or not anchors:
            continue
        above = [chapter for y, chapter in anchors if y <= item.y]
        if above:
            item.chapter = above[-1]

    return elements, chapter

_PROSE_PUNCT_RE = re.compile(r"[，。？！；：]")


def _is_code_line(line, body_left: float, body_size: float) -> bool:
    """单行是不是代码。三个条件任一成立即可，各对着一种排版：

    - 左边缘缩进超过正文列；
    - 几乎全是等宽字体（顶格的代码行只能靠它）；
    - 字号明显小于正文。

    正文里夹的行内代码三样都不满足。三个都要，缺一个就有一类书解不出来。
    """
    share = line[7]
    if share <= 0.05:
        return False
    size = max(line[5], 1.0)
    if body_left > 0 and line[0] >= body_left + 0.4 * size:
        return True
    if share > 0.8:
        return True
    return body_size > 0 and size < body_size - 1.2


def _looks_like_code(block: list, body_size: float = 0.0) -> bool:
    """一组候选行到底是不是代码块。优先级是字号：明显小于正文的行即使全是中文也是代码
    注释；字号相同才看句读（单行正文多是以「？」「。」收尾的思考题）。多行块看过半数的行。
    """
    if len(block) == 1:
        item = block[0]
        if body_size > 0 and item[5] < body_size - 1.2:
            return True
        return not _PROSE_PUNCT_RE.search(item[4])
    prose_rows = sum(1 for item in block if _PROSE_PUNCT_RE.search(item[4]))
    return prose_rows * 2 <= len(block)


_FILE_HINT_RE = re.compile(r"\^\^=\s*File:\s*(\S+?)\s*\^\^=")

_LANG_BY_EXT = {
    "py": "python", "js": "javascript", "ts": "typescript", "jsx": "jsx",
    "tsx": "tsx", "sh": "bash", "bash": "bash", "zsh": "bash", "md": "markdown",
    "java": "java", "go": "go", "rs": "rust", "c": "c", "h": "c", "cpp": "cpp",
    "cc": "cpp", "hpp": "cpp", "cs": "csharp", "rb": "ruby", "php": "php",
    "json": "json", "yml": "yaml", "yaml": "yaml", "toml": "toml", "sql": "sql",
    "html": "html", "css": "css", "xml": "xml", "kt": "kotlin", "swift": "swift",
}


def _code_language(code: str) -> str:
    """从代码块里找出语言，认不出来就返回空串（围栏不带语言也能渲染）。"""
    match = _FILE_HINT_RE.search(code)
    if match is None:
        return ""
    extension = match.group(1).rsplit(".", 1)[-1].lower()
    return _LANG_BY_EXT.get(extension, "")


def _code_block_text(block: list) -> str:
    """一组代码行 → 带原始缩进的多行代码。

    缩进要还原：PDF 没有空格字符，缩进体现在左边缘，所以用 x0 减本块基准列、再按
    0.6 × 本行字号折回空格数。折行要接回去但多行结构不能接：判据是「上一行括号/引号
    没闭合 **且** 本行不更靠右」——只判括号会把多行字典误合并。
    """
    base = min(item[0] for item in block)
    char_width = max(block[0][5] * 0.6, 1.0)
    lines: list[str] = []
    last_x: float | None = None
    for item in block:
        indent = max(0, round((item[0] - base) / char_width))
        if lines and _unbalanced(lines[-1]) and last_x is not None \
                and item[0] <= last_x + 0.5 * char_width:
            tail = lines[-1].rstrip()
            joiner = "" if tail[-1:] in _OPEN_BRACKETS else " "
            lines[-1] = tail + joiner + item[4].strip()
        else:
            lines.append(" " * indent + item[4].rstrip())
        last_x = item[0]
    return "\n".join(lines).rstrip()


_SENTENCE_END = "。！？；：.!?;:"

_BULLET_RE = re.compile(r"^\s*([-*•·▪◦‣]|\d+[.)、]\s?|[（(]\d+[)）]|[a-z][.)]\s)")


_PPT_COLUMN_GAP = 48.0

_PPT_BULLET_RE = re.compile(r"^[\s\u200b]*[\uf000-\uf8ff➢➤►▶▸◆◇▪▫●○•·❖]")

_PPT_FIGURE_COVER = 0.5
_PPT_FIGURE_MAX_CHARS = 320

_PPT_FIGURE_MIN_CHARS = 60
_PPT_FIGURE_MIN_SHAPES = 20


def _has_side_by_side(box: list) -> bool:
    """框里是否有一行**并排排了两格以上**（两条行的 y 相差不到半个字高）。

    容差必须按字号算：表格里同一行的两格基线常有 2~3pt 的差（字号不同、
    对齐方式不同），按「y 完全相同」判会漏。
    """
    size = max(box[0][5], 1.0)
    heights = sorted(item[1] for item in box)
    return any(
        current - previous < 0.5 * size
        for previous, current in zip(heights, heights[1:])
    )


def _ppt_continues(previous: list, box: list) -> bool:
    """box 是不是接着 previous 那段话往下写的另一个文本框（PPT 独有：写到右边界溢出的
    部分在 PDF 里是另一个 block）。四条同时成立：

    1. 上一块最后一行没有句末标点；
    2. 那一行是本块最宽的一行（顶到右边界才被切断）；
    3. 本块首行比上一块首行更靠右；
    4. 本块首行不以条目符号开头，且横向落在上一块范围内。
    """
    tail = previous[-1]
    head = box[0]
    if tail[4].rstrip()[-1:] in _SENTENCE_END:
        return False
    if _has_side_by_side(box):
        return False
    if tail[2] < max(item[2] for item in previous) - 1.0:
        return False
    if _PPT_BULLET_RE.match(head[4]):
        return False
    size = max(head[5], tail[5], 1.0)
    indent = head[0] - tail[0]
    if not 0.2 * size <= indent <= 2.5 * size:
        return False
    low = min(item[0] for item in previous) - size
    high = max(item[2] for item in previous) + size
    if not low <= head[0] <= high:
        return False
    return -0.5 * size <= head[1] - tail[3] <= 1.5 * size


def _ppt_columns(ranges: list[tuple[float, float]]) -> list[int]:
    """给每个文本框判栏号：先按中心 x 的间隔粗分（48pt），再把「嵌在栏里的窄框」并回去。

    第二遍按横向重叠判：窄框有 70% 落在某栏范围内就归该栏。否则宽右栏里嵌着的小框会被
    当成独立一栏，读起来就成了「左栏 → 性能评价 → 右栏」。
    """
    centers = sorted({round((low + high) / 2, 0) for low, high in ranges})
    column_of: dict[float, int] = {}
    column = 0
    for position, center in enumerate(centers):
        if position and center - centers[position - 1] > _PPT_COLUMN_GAP:
            column += 1
        column_of[center] = column

    spans: dict[int, list[float]] = {}
    for low, high in ranges:
        span = spans.setdefault(column_of[round((low + high) / 2, 0)], [low, high])
        span[0] = min(span[0], low)
        span[1] = max(span[1], high)

    merged: list[list] = []  # [low, high, [原栏号...]]
    for key in sorted(spans):
        low, high = spans[key]
        if merged:
            wider = merged[-1]
            overlap = min(high, wider[1]) - max(low, wider[0])
            narrower = min(high - low, wider[1] - wider[0])
            if narrower > 0 and overlap / narrower >= 0.5:
                wider[0] = min(wider[0], low)
                wider[1] = max(wider[1], high)
                wider[2].append(key)
                continue
        merged.append([low, high, [key]])

    final: dict[int, int] = {}
    for index, (_low, _high, keys) in enumerate(merged):
        for key in keys:
            final[key] = index
    return [final[column_of[round((low + high) / 2, 0)]] for low, high in ranges]


def _ppt_paragraphs(lines: list) -> list[list]:
    """PPT 的段落重建：按文本框分组，再按「先左后右、再上到下」排。

    不能按几何行合并：一页常是几个并列文本框，同高度的行会被串成一句。三步：合并续写框
    （_ppt_continues）→ 框内按条目符号切段 → 按栏优先排序（_ppt_columns）。

    只给 PPT 用：正文书 93% 的行各自一个 block，按 block 聚合等于一行一段。
    """
    boxes: dict[int, list] = {}
    for line in lines:
        boxes.setdefault(int(line[6]), []).append(line)
    ordered = [
        sorted(box, key=lambda item: (round(item[1], 1), item[0]))
        for box in boxes.values()
    ]
    ordered.sort(key=lambda box: (round(box[0][1], 1), box[0][0]))

    groups: list[list] = []
    for box in ordered:
        for group in reversed(groups):
            if _ppt_continues(group, box):
                group.extend(box)
                break
        else:
            groups.append(box)

    paragraphs: list[list] = []
    for group in groups:
        current: list = []
        for line in group:
            if current and _PPT_BULLET_RE.match(line[4]):
                paragraphs.append(current)
                current = []
            current.append(line)
        if current:
            paragraphs.append(current)

    ranges = [
        (min(item[0] for item in group), max(item[2] for item in group))
        for group in paragraphs
    ]
    columns = _ppt_columns(ranges)
    order = sorted(
        range(len(paragraphs)),
        key=lambda index: (columns[index], round(paragraphs[index][0][1], 1)),
    )
    return [paragraphs[index] for index in order]


def _ppt_page_order(elements: list[_Element]) -> list[_Element]:
    """PPT 一页的元素顺序：正文保持 `_ppt_paragraphs` 的阅读顺序，只决定图和式插在哪两段之间
    （按「它上方有几段正文」定位，页首大图自然排到最前面）。

    不能按 y 重排：右栏的 y 比左栏末段还小，重排会把两栏又串成一条。
    """
    body = [item for item in elements if item.kind in {"text", "code"}]
    if not body:
        return sorted(elements, key=lambda item: item.y)
    pending = sorted(
        (item for item in elements if item.kind not in {"text", "code"}),
        key=lambda item: item.y,
    )
    ordered: list[_Element] = []
    for item in body:
        while pending and pending[0].y <= item.y:
            ordered.append(pending.pop(0))
        ordered.append(item)
    ordered.extend(pending)
    return ordered


def _pdf_shape_count(page) -> int:
    """一页的矢量图形条数。用来认「文字只剩几个标签、图形却几十条」的示意图页。"""
    try:
        return len(page.get_drawings())
    except Exception:  # noqa: BLE001 - 畸形页面取不到图形，按「没有图」处理
        return 0


def _pdf_graphic_cover(page) -> float:
    """一页里**最大一块图形**占页面面积的比例。

    用来判断「这一页其实是一张图」。幻灯片上的图是几十条矢量线加若干文字块，
    `get_images` 拿不到（它会返回空），只能看 `get_drawings` 合并后的簇。
    """
    try:
        rects = _pdf_graphic_rects(page)
    except Exception:  # noqa: BLE001 - 畸形页面取不到图形，按「不是图」处理
        return 0.0
    if not rects:
        return 0.0
    area = page.rect.width * page.rect.height
    if area <= 0:
        return 0.0
    widest = max(rects, key=lambda rect: rect.width * rect.height)
    return (widest.width * widest.height) / area


def _pdf_paragraphs(lines: list) -> list[list]:
    """把版面上一行行的文字合并成段落，不看 block 序号。

    不同工具链切 block 的方式完全相反：浏览器打印的 PDF 93% 是一行一个 block，LaTeX 一个
    block 能装 7 行——block 边界只是生成器的内部选择。能跨工具链通用的只有几何：
    行距 + 字号 + 标点 + 左边缘。
    """
    groups: list[list] = []
    current: list = []
    for item in lines:
        if not current:
            current = [item]
            continue
        previous = current[-1]
        size = max(previous[5], 1.0)
        gap = item[1] - previous[3]
        same_size = abs(item[5] - previous[5]) < 0.6
        tight = -0.3 * size <= gap <= 0.75 * size
        open_tail = previous[4].rstrip()[-1:] not in _SENTENCE_END
        indented = item[0] - previous[0] > 1.5 * size
        starts_list = bool(_BULLET_RE.match(item[4]))
        if same_size and tight and open_tail and not indented and not starts_list:
            current.append(item)
        else:
            groups.append(current)
            current = [item]
    if current:
        groups.append(current)
    return groups


def _join_lines(parts: list[str]) -> str:
    """把同一段里的多行拼成一行：西文断词去掉连字符直接接，西文句子之间补空格，中文直接接。
    **中文不补空格**是硬要求——补了会得到「抽样 定理」，检索按词匹配会全部失配。
    """
    text = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not text:
            text = part
            continue
        if text.endswith("-") and part[:1].isalpha():
            text = text[:-1] + part
        elif re.search(r"[A-Za-z0-9,;:.)]$", text) and re.match(r"[A-Za-z0-9(]", part):
            text = f"{text} {part}"
        else:
            text = text + part
    return text


def _match_caption(text: str) -> tuple[str, str] | None:
    """一行是不是「图 3-4 xxx」这种独占题注，返回 (label, caption)。三道过滤：

    整行太长（是正文句子）；题注以句末标点收尾；题注以「展示了 / 反映了」这类动词
    开头（正文在引用图）。缺了第 3 条会去截隔壁那张图，编号还会重号。
    """
    stripped = text.strip()
    if len(stripped) > 120:
        return None
    match = _CAPTION_RE.match(stripped)
    if match is None:
        return None
    kind, number, caption = match.group(1), match.group(2), match.group(3).strip()
    if caption.endswith(("。", "！", "？", ".")) and len(caption) > 12:
        return None
    if len(caption) <= 1:
        return None
    if caption.startswith(_CAPTION_VERBS):
        return None
    if len(caption) > 45:
        return None
    prefix = "图" if kind.lower().startswith(("图", "fig")) else "表"
    return f"{prefix} {number.replace(' ', '')}", caption


def _center_in(bbox: tuple[float, float, float, float], rects: list) -> bool:
    """这个文字行的中心点落在某个矩形里吗。用来判断「这行字属于表格」。"""
    x = (bbox[0] + bbox[2]) / 2
    y = (bbox[1] + bbox[3]) / 2
    return any(rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3] for rect in rects)


def _pdf_tables(page) -> list[tuple[str, tuple[float, float, float, float]]]:
    """找出这一页的表格，返回 (markdown 表格, bbox)。用 find_tables()，靠线条和对齐找
    网格，无边框表格会漏（不致命，文字仍会作为正文进去，只是失去行列对齐）。

    丢掉三种：少于 2 列或 2 行；面积超整页 75%（双栏正文被框成「表格」）；清洗后不足 2 行。
    """
    try:
        found = page.find_tables()
    except Exception:  # noqa: BLE001 - 畸形页面/空页面
        return []
    page_area = page.rect.width * page.rect.height
    result: list[tuple[str, tuple[float, float, float, float]]] = []
    for table in getattr(found, "tables", []):
        bbox = getattr(table, "bbox", None)
        if bbox is None:
            continue
        bbox = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if getattr(table, "col_count", 0) < 2 or getattr(table, "row_count", 0) < 2:
            continue
        if area > page_area * 0.75:
            continue
        try:
            rows = _clean_table(table.extract())
        except Exception:  # noqa: BLE001
            continue
        if len(rows) < 2:
            continue
        result.append((_rows_to_markdown(rows), bbox))
    return result


def _clean_table(rows: list[list[str | None]]) -> list[list[str]]:
    """把 extract() 的原始网格清洗成 markdown 表格。治三个毛病：

    单元格内换行被拆成新行（超过一半格子为空 → 是上一行的续行，拼回去）；
    合并单元格无值（第一列空而上一行有值 → 继承）；自动列名 Col1 / Col3 是噪音。

    清洗是有损的（−2⁷ 会拼成 −27），引用时用「原书第 N 页」让人能回原文核对。
    """
    grid: list[list[str]] = []
    for row in rows:
        cells = [_sanitize_surrogates((cell or "")).strip() for cell in row]
        cells = [re.sub(r"^Col\d+$", "", cell) for cell in cells]
        grid.append(cells)
    grid = [row for row in grid if any(cell for cell in row)]
    if not grid:
        return []
    cols = max(len(row) for row in grid)
    grid = [row + [""] * (cols - len(row)) for row in grid]

    cleaned: list[list[str]] = []
    for row in grid:
        if cleaned and not row[0] and cleaned[-1][0]:
            row = [cleaned[-1][0], *row[1:]]
        filled = sum(1 for cell in row if cell)
        if cleaned and filled * 2 <= cols:
            merged = False
            for index, cell in enumerate(row):
                if cell:
                    cleaned[-1][index] = _append_cell(cleaned[-1][index], cell)
                    merged = True
            if merged:
                continue
        cleaned.append(row)
    return cleaned


def _append_cell(left: str, right: str) -> str:
    """把续行的两截拼成一格。中文直接接，西文补空格（和拼行是同一个规则）。"""
    if not left:
        return right
    if re.search(r"[A-Za-z0-9,;:.)]$", left) and re.match(r"[A-Za-z0-9(]", right):
        return f"{left} {right}"
    return left + right


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """二维数组 → markdown 表格。单元格里的 `|` 要转义，否则表格会多出一列。"""
    if not rows:
        return ""

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").strip()

    width = len(rows[0])
    lines = ["| " + " | ".join(cell(value) for value in rows[0]) + " |"]
    lines.append("|" + "---|" * width)
    for row in rows[1:]:
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(lines)


def _is_layout_frame(rect, page_rect, text_rects: list) -> bool:
    """这个矩形是不是页面排版框（边距框 / 背景框），不是插图。

    两条同时成立：宽高都 ≥ 页面 60%，且半数以上文字块（90% 面积）落在它里面。
    必须排掉：它一旦进图形列表，会把真正的插图合并成整页大簇，导致全书一张图都配不上。
    放在收集阶段过滤，合并之后就分不出来了。
    """
    if rect.width < page_rect.width * 0.6 or rect.height < page_rect.height * 0.6:
        return False
    inside = sum(
        1 for block in text_rects if (block & rect).get_area() > block.get_area() * 0.9
    )
    return inside >= max(3, len(text_rects) // 2)


_SLIDE_ICON_TEXTS = {"play"}


def _is_slide_label(line, boxes: list) -> bool:
    """这一行是不是幻灯片上的**图内标注**（`A`、`f1`、`发射机`）。四条同时成立：
    落在某张图的截图范围里、短（≤ 20 字）、不是一句话、不是条目（`➢` 开头的是真要点）。

    只认「真的截下来的那张图」，不认图形簇——簇在排版宽松的页上会长得很大，把正文圈进去。
    """
    text = line[4].strip()
    if text.lower() in _SLIDE_ICON_TEXTS:
        return True
    if not text or len(text) > 20:
        return False
    if text[-1] in _SENTENCE_END:
        return False
    if _PPT_BULLET_RE.match(text):
        return False
    center_x = (line[0] + line[2]) / 2
    center_y = (line[1] + line[3]) / 2
    return any(
        box[0] <= center_x <= box[2] and box[1] <= center_y <= box[3] for box in boxes
    )


def _pdf_graphic_rects(page) -> list:
    """一页里所有「可能是插图」的版面矩形，位图和矢量图两路都收。

    只做位图会一张图都抽不到：扫描 / 重排过的教材图是线条画出来的，get_images 返回空。
    两路都过滤装饰元素：太小的（图标、分数线）、贯穿整页的细横线（页眉分隔线）。
    """
    pymupdf = _require("pymupdf", "pip install pymupdf")
    rects = []
    page_rect = page.rect
    try:
        for image in page.get_images(full=True):
            for rect in page.get_image_rects(image[0]):
                if rect.width >= 40 and rect.height >= 30:
                    rects.append(rect)
    except Exception:  # noqa: BLE001 - 畸形 PDF 的图字典可能是空的或类型不对
        pass
    try:
        text_rects = [
            pymupdf.Rect(block[:4]) for block in page.get_text("blocks") if block[4].strip()
        ]
    except Exception:  # noqa: BLE001
        text_rects = []
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is None:
                continue
            if rect.width < 20 or rect.height < 12:
                continue
            if rect.height < 4 and rect.width > page_rect.width * 0.5:
                continue
            if _is_layout_frame(rect, page_rect, text_rects):
                continue
            rects.append(rect)
    except Exception:  # noqa: BLE001
        pass
    return _merge_clusters(rects)


def _merge_clusters(rects: list, gap: float = 24.0) -> list:
    """把相邻的图形元素合成一个簇，返回簇的外包框。

    一张图在 PDF 里常常是几十条线加上几个文字块（轴标签），不合并的话
    会得到几十个碎片，截图只能截到其中一条线。
    """
    if not rects:
        return []
    ordered = sorted(rects, key=lambda r: (r.y0, r.x0))
    clusters: list[list] = [[ordered[0]]]
    for rect in ordered[1:]:
        current = clusters[-1]
        y_max = max(r.y1 for r in current)
        x_min = min(r.x0 for r in current)
        x_max = max(r.x1 for r in current)
        if rect.y0 - y_max <= gap and rect.x0 <= x_max + gap and rect.x1 >= x_min - gap:
            current.append(rect)
        else:
            clusters.append([rect])
    merged = []
    for cluster in clusters:
        box = cluster[0]
        for rect in cluster[1:]:
            box = box | rect
        merged.append(box)
    return merged


def _drop_inside(rects: list, box) -> list:
    """候选里已经被截进 `box` 的簇全部去掉。书页截的是一个簇，按相等剔除就够；幻灯片截的是
    若干簇的并集，得按「有没有一半落在里面」判，否则同页第二个题注会把第一张图再截一遍。
    """
    kept = []
    for rect in rects:
        area = (rect.x1 - rect.x0) * (rect.y1 - rect.y0)
        overlap = (min(box.x1, rect.x1) - max(box.x0, rect.x0)) * (
            min(box.y1, rect.y1) - max(box.y0, rect.y0)
        )
        if area > 0 and overlap > 0 and overlap / area >= 0.5:
            continue
        kept.append(rect)
    return kept


def _slide_figure_rect(rects: list, caption_bbox: tuple[float, float, float, float], page):
    """幻灯片上「题注上方」的截图范围：从题注往上逐段生长，再求并集。

    幻灯片不能只取最近的簇：连线、箭头、虚框各自成簇，最近的常只有 30×30pt。
    间隔 60pt 是「图里相邻部件」和「图与上一块文字」的分界。窗口下界取 -30 而不是 0，
    因为幻灯片的图常把题注包在自己里面。
    """
    picked = [
        rect
        for rect in rects
        if -30 <= caption_bbox[1] - rect.y1 <= 60 and _column_near(rect, caption_bbox)
    ]
    cursor = min((rect.y0 for rect in picked), default=caption_bbox[1])
    while picked:
        box = None
        for rect in picked:
            box = rect if box is None else box | rect
        grown = [
            rect
            for rect in rects
            if rect not in picked
            and -30 <= cursor - rect.y1 <= 60
            and _column_near(rect, (box.x0, box.y0, box.x1, box.y1))
        ]
        if not grown:
            break
        picked.extend(grown)
        cursor = min(rect.y0 for rect in grown)
    if not picked:
        return None
    box = picked[0]
    for rect in picked[1:]:
        box = box | rect
    if box.width * box.height < 0.15 * page.rect.width * page.rect.height:
        return None
    return box


def _column_near(rect, caption_bbox: tuple[float, float, float, float], margin: float = 40.0) -> bool:
    """横向和题注沾边：否则收到的是隔壁栏的图。"""
    return rect.x1 >= caption_bbox[0] - margin and rect.x0 <= caption_bbox[2] + margin


def _nearest_graphic_above(rects: list, caption_bbox: tuple[float, float, float, float]):
    """图注上方的图形簇。教材图题在图下方是惯例，所以只往上找。

    距离限制 340pt（约半页）是必须的：不限制的话，某页只有一个图注但上方
    有整页表格的线框，会把表格截成「图」。
    """
    caption_top = caption_bbox[1]
    caption_left, caption_right = caption_bbox[0], caption_bbox[2]
    best = None
    best_gap = 340.0
    for rect in rects:
        gap = caption_top - rect.y1
        if gap < -4 or gap > best_gap:
            continue
        if rect.x1 < caption_left - 30 or rect.x0 > caption_right + 30:
            continue
        if rect.height < 20:
            continue
        best, best_gap = rect, gap
    return best


def _formula_rect(page, line_bbox: tuple[float, float, float, float]):
    """公式的截图范围：编号所在行往上扩 46pt。不做公式识别（书上什么样就显示什么样），
    宁可多截一点带上下文，也别截掉上半截式子。
    """
    pymupdf = _require("pymupdf", "pip install pymupdf")
    x0, y0, x1, y1 = line_bbox
    width = page.rect.width
    return pymupdf.Rect(
        max(width * 0.08, x0 - 80),
        max(0, y0 - 46),
        min(width * 0.94, x1 + 80),
        min(page.rect.height, y1 + 6),
    )


def _save_clip(
    page,
    rect,
    asset_dir: Path,
    parsed: _Parsed,
    label: str,
    caption: str,
    page_no: int,
    kind: str,
) -> dict | None:
    """把一块版面截成 PNG 存盘，返回资产记录。

    用 `get_pixmap(clip=...)` 而不是「把图对象抽出来」：矢量图根本没有图对象，
    只有渲染后的像素。**统一走「渲染一块区域」**，位图和矢量图就只需要一条路径。
    """
    try:
        pixmap = page.get_pixmap(clip=rect, dpi=150)
    except Exception:  # noqa: BLE001 - 空区域/越界区域渲染会失败
        return None
    if pixmap.width < 40 or pixmap.height < 30:
        return None
    asset_id = f"as_{secrets.token_hex(4)}"
    path = asset_dir / f"{asset_id}.png"
    pixmap.save(str(path))
    record = {
        "asset_id": asset_id,
        "kind": kind,
        "label": label,
        "caption": caption or None,
        "page": page_no,
        "file_path": str(path),
        "width": pixmap.width,
        "height": pixmap.height,
    }
    parsed.assets.append(record)
    return record


def _figure_chunk_text(label: str, caption: str) -> str:
    """图对应的 chunk 内容 = 编号 + 题注。只有编号没有题注的图也保留——「第 3 章有图 3-4」
    这个事实能让检索说清「书上有这张图」。
    """
    return f"{label} {caption}".strip()


def _chapter_at(toc: list, page_no: int) -> str | None:
    """按书签目录算某一页属于哪一章，取最深一级的标题。

    目录页码与 `page_no` 同口径（都 1-based），直接比即可。没有目录返回 None——宁可不标，
    也不用字号猜一个错的（C2 要求页码章节与书一致）。
    """
    best: tuple[int, str] | None = None
    for entry in toc:
        if len(entry) < 3:
            continue
        level, title, page = entry[0], entry[1], entry[2]
        if isinstance(page, int) and 0 < page <= page_no:
            if best is None or level >= best[0]:
                best = (level, str(title))
    return best[1] if best else None



def _parse_pptx(data: bytes, asset_dir: Path) -> _Parsed:
    """一套课件 → 元素流。**一张幻灯片一条 chunk**（或按长度切几条）。

    PPT 是「一页一个主题」，跨页合并会把互不相干的短句拼在一起，和 PDF 的「段落跨页要合并」
    正好相反。图片按 `shape.image.blob` 取原始字节存盘；公式是 OMML，v1 只做页级引用。
    """
    pptx = _require("pptx", "pip install python-pptx")
    from io import BytesIO

    presentation = pptx.Presentation(BytesIO(data))
    parsed = _Parsed(page_count=len(presentation.slides), section_per_page=True)

    for slide_index, slide in enumerate(presentation.slides, start=1):
        title = ""
        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                value = shape.text_frame.text.strip()
                if not value:
                    continue
                if shape == getattr(slide.shapes, "title", None):
                    title = value.splitlines()[0][:80]
                texts.append(value)
                continue
            if getattr(shape, "shape_type", None) == 13 and hasattr(shape, "image"):
                blob = shape.image.blob
                extension = shape.image.ext or "png"
                asset_id = f"as_{secrets.token_hex(4)}"
                path = asset_dir / f"{asset_id}.{extension}"
                path.write_bytes(blob)
                label = f"幻灯片 {slide_index} 插图"
                parsed.assets.append(
                    {
                        "asset_id": asset_id,
                        "kind": "figure",
                        "label": label,
                        "caption": None,
                        "page": slide_index,
                        "file_path": str(path),
                        "width": int(shape.width / 9525) if shape.width else 0,
                        "height": int(shape.height / 9525) if shape.height else 0,
                    }
                )
                texts.append(f"[{label}]")

        body = "\n".join(texts).strip()
        if not body:
            continue
        parsed.elements.append(
            _Element(
                kind="text",
                content=body,
                page=slide_index,
                y=0,
                chapter=title or None,
            )
        )

    return parsed



def _parse_docx(data: bytes, asset_dir: Path) -> _Parsed:
    """一份 Word 教材 → 元素流。

    页数「不知道」：Word 的页数取决于渲染时的字体和纸张，python-docx 不提供。`page_count` 记 0、
    前端显示「—」，不编一个数字；引用只能给到章节。表格按行转成 `a | b | c` 纯文本进正文
    （不转就整块丢失）。
    """
    docx = _require("docx", "pip install python-docx")
    from io import BytesIO

    document = docx.Document(BytesIO(data))
    parsed = _Parsed(page_count=0)
    chapter: str | None = None
    page_no = 0

    for block in _iter_docx_blocks(document):
        kind, payload = block
        if kind == "paragraph":
            text = (payload.text or "").strip()
            if not text:
                continue
            style = ""
            try:
                style = payload.style.name or ""
            except Exception:  # noqa: BLE001
                style = ""
            if style.lower().startswith("heading") or _HEADING_RE.match(text):
                chapter = text[:80]
                page_no += 1
            caption = _match_caption(text)
            if caption is not None and caption[0].startswith("图"):
                label, caption_text = caption
                parsed.elements.append(
                    _Element(
                        kind="text",
                        content=f"{label} {caption_text}".strip(),
                        page=page_no,
                        y=0,
                        chapter=chapter,
                    )
                )
                continue
            parsed.elements.append(
                _Element(kind="text", content=text, page=page_no, y=0, chapter=chapter)
            )
        elif kind == "table":
            rows = []
            for row in payload.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parsed.elements.append(
                    _Element(
                        kind="text",
                        content="\n".join(rows),
                        page=page_no,
                        y=0,
                        chapter=chapter,
                    )
                )
        elif kind == "image":
            asset_id = f"as_{secrets.token_hex(4)}"
            extension, blob, width, height = payload
            path = asset_dir / f"{asset_id}.{extension}"
            path.write_bytes(blob)
            label = f"图 {len(parsed.assets) + 1}"
            parsed.assets.append(
                {
                    "asset_id": asset_id,
                    "kind": "figure",
                    "label": label,
                    "caption": None,
                    "page": page_no,
                    "file_path": str(path),
                    "width": width,
                    "height": height,
                }
            )
            parsed.elements.append(
                _Element(
                    kind="figure",
                    content=label,
                    page=page_no,
                    y=0,
                    asset_id=asset_id,
                    label=label,
                    chapter=chapter,
                )
            )
    return parsed


def _iter_docx_blocks(document):
    """按文档顺序遍历段落和表格。`document.paragraphs` 和 `document.tables` 是两个独立列表，
    分开遍历会把表格全排到正文后面，所以走底层 XML 按 body 子元素顺序还原。
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield "paragraph", Paragraph(child, document)
        elif tag == "tbl":
            yield "table", Table(child, document)
        elif tag == "sectPr":
            continue

    for rel in document.part.rels.values():
        if "image" not in rel.reltype:
            continue
        try:
            blob = rel.target_part.blob
            extension = rel.target_part.partname.ext.lstrip(".") or "png"
        except Exception:  # noqa: BLE001
            continue
        yield "image", (extension.lower(), blob, 0, 0)
