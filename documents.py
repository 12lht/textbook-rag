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

import base64
import difflib
import hashlib
import html.parser
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
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
# 比这还短的正文段一定是碎片（「开始」「则」「–结尾:」），再被图/公式夹着也要就近并掉
_TINY_CHARS = 10

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
_HEADING_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百]+\s*[章节篇部]"
    r"|[0-9]+(\.[0-9]+){1,2}\s*[、.．]?\s*\S"
    r"|Chapter\s+[0-9]+)",
    re.IGNORECASE,
)


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
    # 图片 sha1 → asset_id，只在单次解析内用来去重，不入库
    asset_hashes: dict[str, str] = field(default_factory=dict)


def parse_document(doc_id: str) -> dict[str, object]:
    """把一本 pending 的书解析完，返回最终记录。

    PDF 固定走 MinerU（`_parse_pdf` 里没有第二条路），失败即报错、不回退。

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

    **重切完必须让向量跟上**（`_sync_vectors`）：`chunk_id` 是位置式编号，重切后 id 不变
    但正文可能已经换了，`ensure_vectors` 不看内容就以为「已经有了」。以前靠调用方自觉补
    这一步，`scripts/fill-concepts.py` 就是漏的那个——图 chunk 的向量一直指向「第 N 页 插图」。
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
        _bump_corpus(conn)
        conn.commit()
    _sync_vectors(doc_id)
    return {"doc_id": doc_id, "chunk_count": len(chunks)}


def _sync_vectors(doc_id: str) -> dict | None:
    """让这本书的向量跟上当前段落。**失败只记日志**——向量是增强项，不能让重切失败。

    按内容哈希只补「变了或没有」的那些（见 `retrieval.ensure_vectors`），所以重复调用
    很便宜；`forget_document` 那种「全删重算」只在删书时用。
    """
    import retrieval

    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        return None
    user_id = str(row["user_id"])
    if Path(DB_PATH).parent.resolve() != retrieval.vector_db_path().parent.resolve():
        # 文档库和向量库不在同一个目录 = 有人把库指到了临时目录（探针、离线实验）。
        # 这时候同步向量等于往真库里塞临时书的数据，直接跳过。
        _log.debug("文档库与向量库不同目录，跳过后台补向量 doc=%s", doc_id)
        return None
    try:
        result = retrieval.ensure_vectors(user_id)
        retrieval.clear_cache(user_id)  # 段落变了，内存索引必须重建
        if result.get("reason") or result.get("error"):
            # 「没配 key」也算失败：向量停在旧正文上，检索会慢慢变错而且完全不报错
            _log.warning("重切后向量没补齐 doc=%s：%s", doc_id, result)
        return result
    except Exception:  # noqa: BLE001 - 向量服务挂了不该影响切分结果
        _log.warning("重切后补向量失败 doc=%s", doc_id, exc_info=True)
        return None


_PLACEHOLDER_ALT_RE = re.compile(r"!\[第 (\d+) 页 插图\]\((as_[0-9a-f]{4,})\.png\)")

# ── 存量修复：把正文里的裸 LaTeX 包成 `$..$` ───────────────────────────────────
# 老解析把 MinerU 的 `equation_inline` 直接拼成文本（整库 5749 处），前端不渲染、检索是噪声。
# 代码已修（`_inline_text` 现在会包），这里只处理**已入库的 Markdown**：重新解析一轮太贵，
# 而这些片段在中文正文里几乎不会与其他东西混淆，所以用边界扫描找回公式的边界。
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_BLOCK_MATH_RE = re.compile(r"\$\$.*?\$\$", re.S)
_INLINE_MATH_RE = re.compile(r"\$[^$\n]+\$")
# 图片引用必须整段保护：`![图注](as_x.png)` 里既有 `]` 又有 `(`，
# 公式边界一跨过去就会包成 `$...](as_x.png)$`，前端渲染直接报红（11_隐马尔科夫-I 踩过）。
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]{2,}")
_CJK_PUNCT = set("，。；：、！？（）《》「」【】…—·～“”‘’")


def _is_stop(ch: str) -> bool:
    """公式片段到这里就结束：汉字、中文标点、换行、反引号，以及被保护起来的占位符。"""
    if ch in "\x00\n`":
        return True
    if ch in _CJK_PUNCT:
        return True
    return "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"


def wrap_inline_math(markdown: str) -> tuple[str, int]:
    """把正文里裸的 LaTeX 片段包成 `$..$`，返回 (新文本, 包裹了几处)。

    先保护掉代码块、`$$..$$`、已有的 `$..$`（换成占位符），再以每个 `\\命令` 为锚点向左右
    扩到边界，重叠区间合并后一次性重建。**表格行整行跳过**——`|` 既是列分隔又可能是
    `\\left| x \\right|`，分不清就别动。
    """
    stash: list[str] = []

    def protect(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"\x00{len(stash) - 1}\x00"

    text = markdown
    for pattern in (_CODE_FENCE_RE, _IMAGE_REF_RE, _BLOCK_MATH_RE, _INLINE_MATH_RE):
        text = pattern.sub(protect, text)

    spans: list[tuple[int, int]] = []
    for match in _LATEX_CMD_RE.finditer(text):
        start, end = match.start(), match.end()
        if spans and start < spans[-1][1]:
            continue
        left = start
        while left > 0 and not _is_stop(text[left - 1]):
            left -= 1
        right = end
        while right < len(text) and not _is_stop(text[right]):
            right += 1
        segment = text[left:right]
        left += len(segment) - len(segment.lstrip())
        right -= len(segment) - len(segment.rstrip())
        if right <= left:
            continue
        # 表格行：这一行的第一个非空白字符是 `|` 就整行不动。
        # **不能拿 `text[line_start:left]` 判**：`|` 不是停止符，第一格以 `\left\| x`
        # 开头时 left 会一路退到行首，`lstrip().startswith("|")` 就变成「不以 | 开头」，
        # 整行被包成 `$| \left\| x \right\| |$`，表格结构直接毁掉。
        line_start = text.rfind("\n", 0, left) + 1
        line_end = text.find("\n", right)
        line = text[line_start: line_end if line_end >= 0 else len(text)]
        first_char = next((ch for ch in line if not ch.isspace()), "")
        if first_char == "|":
            continue
        if spans and left <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], right))
        else:
            spans.append((left, right))

    if not spans:
        return markdown, 0

    parts: list[str] = []
    cursor = 0
    for left, right in spans:
        parts.append(text[cursor:left])
        parts.append(f"${text[left:right]}$")
        cursor = right
    parts.append(text[cursor:])
    wrapped = "".join(parts)
    for index, original in enumerate(stash):
        wrapped = wrapped.replace(f"\x00{index}\x00", original)
    return wrapped, len(spans)



def fix_inline_math(doc_id: str) -> dict[str, object]:
    """把存量 Markdown 里的裸 LaTeX 包成 `$..$` 并重切。**不重解析**。

    改完 chunk 正文就变了，所以调用方必须跟一步 `retrieval.forget_document` + `ensure_vectors`
    ——`chunk_id` 不变、`ensure_vectors` 会以为「已经有了」直接跳过，语义检索就会对着旧文本算。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT markdown, status FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        raise KeyError(doc_id)
    if row["status"] != STATUS_READY or not row["markdown"]:
        raise DocumentError("这本书还没有解析完成。")

    updated, wrapped = wrap_inline_math(row["markdown"])
    if not wrapped:
        return {"doc_id": doc_id, "wrapped": 0}
    with _connect() as conn:
        conn.execute(
            "UPDATE documents SET markdown = ?, updated_at = ? WHERE doc_id = ?",
            (updated, _now(), doc_id),
        )
        conn.commit()
    rechunk(doc_id)
    return {"doc_id": doc_id, "wrapped": wrapped}


def describe_placeholders(doc_id: str, *, limit: int = 200) -> dict[str, object]:
    """把「第 N 页 插图」这种占位图注换成模型写的一句话，**不重解析**。

    走的是 Markdown 中间态那条路：改文本 → 重切。所以既不烧 MinerU 额度，也不要原文件。
    图注写不出来就保持占位——这是个纯优化，任何一张失败都不该让整本书失败。
    """
    import figures

    if not figures.available():
        return {"doc_id": doc_id, "filled": 0, "total": 0, "reason": "未配置视觉模型"}

    with _connect() as conn:
        row = conn.execute(
            "SELECT markdown, status FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise KeyError(doc_id)
        if row["status"] != STATUS_READY or not row["markdown"]:
            raise DocumentError("这本书还没有解析完成。")
        paths = {
            item["asset_id"]: item["file_path"]
            for item in conn.execute(
                "SELECT asset_id, file_path FROM assets WHERE doc_id = ?", (doc_id,)
            )
        }

    markdown = row["markdown"]
    total = len(set(_PLACEHOLDER_ALT_RE.findall(markdown)))
    if not total:
        return {"doc_id": doc_id, "filled": 0, "total": 0, "reason": "没有占位图注"}

    filled: dict[str, str] = {}
    failures = 0
    for _, asset_id in _PLACEHOLDER_ALT_RE.findall(markdown):
        if len(filled) >= limit or failures >= _VLM_FAIL_LIMIT:
            break
        if asset_id in filled:
            continue
        path = paths.get(asset_id)
        if not path:
            continue
        # 和解析链路一样节流：全库补图注是连着打几百次，不打间隔会被服务端限流
        # （实测响应从 4s 掉到 40s，接着就触发熔断，剩下的全成占位）
        time.sleep(_VLM_INTERVAL)
        text = figures.describe(Path(path))
        if text:
            filled[asset_id] = text
            failures = 0
        else:
            failures += 1

    if not filled:
        return {"doc_id": doc_id, "filled": 0, "total": total, "reason": "模型没写出图注"}

    def swap(match: re.Match) -> str:
        text = filled.get(match.group(2))
        return f"![{text}]({match.group(2)}.png)" if text else match.group(0)

    updated = _PLACEHOLDER_ALT_RE.sub(swap, markdown)
    with _connect() as conn:
        conn.execute(
            "UPDATE documents SET markdown = ?, updated_at = ? WHERE doc_id = ?",
            (updated, _now(), doc_id),
        )
        conn.executemany(
            "UPDATE assets SET caption = ? WHERE asset_id = ?",
            [(text, asset_id) for asset_id, text in filled.items()],
        )
        conn.commit()
    rechunk(doc_id)
    return {"doc_id": doc_id, "filled": len(filled), "total": total}


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
        _bump_corpus(conn)
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
        _bump_corpus(conn)
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


def _resolve_asset_file(stored: str, doc_id: str) -> str:
    """库里的绝对路径可能来自旧机器（换盘、搬目录后整批失效，实测 916/916 全断）。
    先认存的；不认就退回 `ASSET_DIR/<doc_id>/<文件名>`——那是图唯一合法的落点。
    不在库里改写路径：换一次机器就要改一次，改在读取侧只需一次。"""
    path = Path(stored or "")
    if path.is_file():
        return str(path)
    fallback = ASSET_DIR / doc_id / path.name
    return str(fallback if fallback.is_file() else path)


def file_of(doc_id: str, user_id: str) -> dict[str, object] | None:
    """取一本书的原始上传文件。**归属检查在这一层**（doc → user 一次 SQL）。

    原来只给前端看解析出的 Markdown，现在普通用户直接看 PDF 原文——
    渲染那套（KaTeX、分块、折叠）留给开发者/管理员，普通用户不需要它。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT doc_id, title, filename, kind, status, file_path FROM documents"
            " WHERE doc_id = ? AND user_id = ?",
            (doc_id, user_id),
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    path = Path(str(record.get("file_path") or ""))
    record["path"] = path
    record["exists"] = path.is_file()
    return record


def asset_of(asset_id: str, user_id: str) -> dict[str, object] | None:
    """取一张图。**三级越权检查**：asset → doc → user，一次 SQL 走完。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT a.asset_id, a.doc_id, a.kind, a.label, a.caption, a.page, a.file_path,"
            " a.width, a.height FROM assets a JOIN documents d ON d.doc_id = a.doc_id"
            " WHERE a.asset_id = ? AND d.user_id = ?",
            (asset_id, user_id),
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["file_path"] = _resolve_asset_file(str(record.get("file_path") or ""), str(record["doc_id"]))
    return record


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


"""Symbol 字体（SymbolMT 等）排的数学符号没有 ToUnicode，两个引擎都原样吐出 U+F0xx，
编辑器里显示成空白——不映射就等于符号全丢。下面是 Adobe Symbol 的内建编码（0x20-0xFE）。"""
_SYMBOL_GREEK_UP = "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"
_SYMBOL_GREEK_LO = "αβχδεφγηιϕκλμνοπθρστυϖωξψζ"
_SYMBOL_MAP = {
    0x20: " ", 0x21: "!", 0x22: "∀", 0x23: "#", 0x24: "∃", 0x25: "%", 0x26: "&",
    0x27: "∋", 0x28: "(", 0x29: ")", 0x2A: "∗", 0x2B: "+", 0x2C: ",", 0x2D: "−",
    0x2E: ".", 0x2F: "/", 0x3A: ":", 0x3B: ";", 0x3C: "<", 0x3D: "=", 0x3E: ">",
    0x3F: "?", 0x40: "≅", 0x5B: "[", 0x5C: "∴", 0x5D: "]", 0x5E: "⊥", 0x5F: "_",
    0x60: "‾", 0x7B: "{", 0x7C: "|", 0x7D: "}", 0x7E: "∼",
    0xA0: "€", 0xA1: "ϒ", 0xA2: "′", 0xA3: "≤", 0xA4: "⁄", 0xA5: "∞", 0xA6: "ƒ",
    0xA7: "♣", 0xA8: "♦", 0xA9: "♥", 0xAA: "♠", 0xAB: "↔", 0xAC: "←", 0xAD: "↑",
    0xAE: "→", 0xAF: "↓", 0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×",
    0xB5: "∝", 0xB6: "∂", 0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈",
    0xBC: "…", 0xBD: "⏐", 0xBE: "⎯", 0xBF: "↵", 0xC0: "ℵ", 0xC1: "ℑ", 0xC2: "ℜ",
    0xC3: "℘", 0xC4: "⊗", 0xC5: "⊕", 0xC6: "∅", 0xC7: "∩", 0xC8: "∪", 0xC9: "⊃",
    0xCA: "⊇", 0xCB: "⊄", 0xCC: "⊂", 0xCD: "⊆", 0xCE: "∈", 0xCF: "∉", 0xD0: "∠",
    0xD1: "∇", 0xD2: "®", 0xD3: "©", 0xD4: "™", 0xD5: "∏", 0xD6: "√", 0xD7: "·",
    0xD8: "¬", 0xD9: "∧", 0xDA: "∨", 0xDB: "⇔", 0xDC: "⇐", 0xDD: "⇑", 0xDE: "⇒",
    0xDF: "⇓", 0xE0: "◊", 0xE1: "⟨", 0xE5: "∑", 0xF1: "⟩", 0xF2: "∫", 0xF3: "⌠",
    0xF4: "⎮", 0xF5: "⌡",
}
# 矩阵/分式的括号拼块（0xE6-0xEF、0xF6-0xFE）本身没有语义，留着只是噪音
_SYMBOL_BOX_CODES = (0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE,
                     0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE)


def _build_symbol_table() -> dict[int, str]:
    table = {0xF000 + code: ch for code, ch in _SYMBOL_MAP.items()}
    table.update({0xF030 + i: chr(0x30 + i) for i in range(10)})
    table.update({0xF041 + i: ch for i, ch in enumerate(_SYMBOL_GREEK_UP)})
    table.update({0xF061 + i: ch for i, ch in enumerate(_SYMBOL_GREEK_LO)})
    table.update({0xF000 + code: " " for code in _SYMBOL_BOX_CODES})
    return table


_SYMBOL_TABLE = _build_symbol_table()


def fix_symbol_font(text: str) -> str:
    if not text:
        return text
    return text.translate(_SYMBOL_TABLE)


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
            # 图号要写回 alt：markdown_elements 会把 `图 2-5 题注` 拆成 label + 题注，
            # 这里不拼回去的话，渲染→解析一来回图号就丢了。
            # **只拼带数字的 label**——`插图` 是没号时的兜底文案，拼进去会变成「插图 OFDM…」。
            alt = element.content
            numbered = element.label and any(ch.isdigit() for ch in element.label)
            if numbered and element.label not in element.content:
                alt = f"{element.label} {element.content}"
            parts.append(f"![{alt}]({element.asset_id}.png)")
            continue
        if element.kind == "code":
            # `rstrip("\n")` 而不是 `rstrip()`：后者会把最后一行行尾的空格也吃掉，
            # render → parse → render 一次就少一个字符（probe-markdown 的往返断言会红）
            parts.append(f"```{element.label or ''}\n{element.content.rstrip(chr(10))}\n```")
            continue
        body = normalize_bullets(fix_symbol_font(element.content.strip()))
        if body:
            parts.append(body)
    return "\n\n".join(parts)


def _image_element(alt: str, path: str, page: int, chapter: str | None) -> _Element:
    """`![alt](as_x.png)` → 元素。alt 里带「图 3-4 题注」时要拆开：label 记图号，
    题注才拿去检索——整句塞进 content 会让引用重复编号。"""
    asset_id = path.rsplit(".", 1)[0]
    hit = _match_caption(alt)
    return _Element(
        kind="formula" if alt.startswith("式") else "figure",
        content=(hit[1] if hit else alt),
        page=page,
        chapter=chapter,
        asset_id=asset_id,
        label=(hit[0] if hit else None),
    )


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
    in_formula = False
    for raw in markdown.splitlines():
        stripped = raw.lstrip()
        if in_formula:
            # $$ 块可能跨行（MinerU 的长公式），一直吃到收尾的 $$
            buffer.append(raw.rstrip())
            if raw.rstrip().endswith("$$"):
                text = "\n".join(buffer)
                elements.append(
                    _Element(kind="formula", content=text, page=page, chapter=chapter)
                )
                buffer = []
                in_formula = False
            continue
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
            anchor_line = raw.rstrip()
            if not (
                _PAGE_ANCHOR_RE.match(anchor_line) or _CHAPTER_ANCHOR_RE.match(anchor_line)
            ):
                buffer.append(raw)  # 代码块里的行尾空格是内容的一部分，别 rstrip
                continue
            # **围栏没闭合时不能一路吞到文件尾**：MinerU 偶尔给出不成对的 ```，
            # 旧逻辑会把后面几十页全当代码（Hello-Agents 因此产出一条 36106 字的 code chunk，
            # 里面混着页码锚点和整段正文）。页码/章节锚点是硬边界，遇到先把代码收尾。
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

        line = raw.rstrip()
        anchor = _PAGE_ANCHOR_RE.match(line)
        if anchor:
            flush()
            page = int(anchor.group(1))
            continue
        anchor = _CHAPTER_ANCHOR_RE.match(line)
        if anchor:
            flush()
            # 顺手洗一遍：老文档的 md 里章节还带着 `<sup>` 之类标记，rechunk 就能修好
            chapter = _clean_heading(anchor.group(1)) or anchor.group(1)
            continue
        if not line.strip():
            flush()
            continue

        # 行间公式：MinerU 的 equation_interline 转成了 `$$..$$`，这里必须认回来，
        # 否则过一遍 md 就退化成正文，引用里也不会标「公式」
        if line.startswith("$$"):
            flush()
            if len(line.strip()) > 2 and line.strip().endswith("$$"):
                elements.append(
                    _Element(kind="formula", content=line.strip(), page=page, chapter=chapter)
                )
            else:
                buffer = [line]
                in_formula = True
            continue

        image = _IMAGE_LINE_RE.match(line)
        if image:
            flush()
            elements.append(_image_element(image.group(1), image.group(2), page, chapter))
            continue

        if "![" in line and _IMAGE_REF_RE.search(line):
            # 图片夹在正文同一行（MinerU 偶尔给 `…如图 ![图 2-3 x](as_1.png) 所示…`）。
            # 不拆开的话这条 `![](as_x.png)` 永远进不了独立 chunk，`assets` 里那张图
            # 就成了「不在任何 chunk 里」的孤儿（实测 106 张），figures_by_doc 也看不到。
            cursor = 0
            for match in _IMAGE_REF_RE.finditer(line):
                head = line[cursor:match.start()].strip()
                if head:
                    if buffer_kind == "table":
                        flush()
                    buffer.append(head)
                flush()
                whole = match.group(0)
                alt = whole[2:whole.index("]")]
                path = whole[whole.index("](") + 2:-1]
                elements.append(_image_element(alt, path, page, chapter))
                cursor = match.end()
            tail = line[cursor:].strip()
            if tail:
                buffer.append(tail)
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


_ANCHOR_ONLY_RE = re.compile(r"^(?:\s*<!--\s*(?:page|chapter):[^>]*-->\s*)+$")
MAX_CODE_CHARS = 1600  # 代码块超过这么长就按行切开：36k 一条的向量等于什么都搜不到


def _split_code(text: str, limit: int = MAX_CODE_CHARS) -> list[str]:
    """按行切代码，每块尽量接近 `limit`。**不切断行**（单行超长才硬切）。

    代码块原来整块成一条 chunk（不经过 `_split_long`，因为切开会破坏语法）。可实测最坏
    36106 字一条，向量被稀释得几乎没用，上下文窗口也会被它吃掉大半；按行切成 1.6k 左右
    既不破坏语法，也让每块有独立语义。
    """
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and current:
            pieces.append("".join(current))
            current, size = [], 0
        while len(line) > limit:  # 单行超长：硬切，否则永远攒不下一块
            pieces.append(line[:limit])
            line = line[limit:]
        current.append(line)
        size += len(line)
    if current:
        pieces.append("".join(current))
    return [piece for piece in pieces if piece.strip()]


def _cut_chunks(
    elements: list[_Element], doc_id: str, *, section_per_page: bool = False
) -> list[dict]:
    """元素流 → 段落列表。单遍扫描，三条规则按顺序生效：

    1. 图/公式独占一条（自带 asset 引用，混进正文会让引用位置失真）；
    2. 长度够了（60% TARGET）之后章变了才断——不能章一变就断，否则短小节被切成一堆
       没有上下文的碎块；
    3. 到 TARGET 且刚好在句末就断，到 MAX 无条件断（尽量在句号处断，别给半句话）。

    表格是原子块：不能被 _split_long 切开（markdown 表格拦腰截断就是乱码），放不下就 flush。
    代码块按行切开（见 `_split_code`）：既保住语法，又不让单条长到几万字。
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
        if text.strip() and not _ANCHOR_ONLY_RE.match(text):
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
            for piece in _split_code(element.content):
                chunks.append(
                    _make_chunk(doc_id, chunks, [element], piece, kind="code")
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
    """把过短的正文片段并进相邻段落，然后重排序号。

    短片段（图前最后一句、章尾几行、被 OCR 切出来的「故」「则」「即」）单独成条时检索能
    命中但答不出东西，还白占引用编号。两遍：
    1. **向前并**——最常见，碎片本来就是上一段的尾巴；
    2. **向后并**——上一段是图/表/公式时向前并不了（图注不是正文），只能挂到下一段开头。

    只合并正文：图的 content 是题注，并进正文会让 asset 引用出现在一段无关的文字里；
    跨章不合并（章节是引用的定位单元）。
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

    # 第二遍：向前并不掉的，试着挂到下一段开头
    folded: list[dict] = []
    index = 0
    while index < len(merged):
        chunk = merged[index]
        following = merged[index + 1] if index + 1 < len(merged) else None
        if (
            following is not None
            and chunk["kind"] == "text"
            and following["kind"] == "text"
            and len(chunk["content"]) < MIN_CHARS
            and len(chunk["content"]) + len(following["content"]) <= MAX_CHARS
            and chunk["chapter"] == following["chapter"]
        ):
            following["content"] = f"{chunk['content']}\n\n{following['content']}"
            following["page_start"] = min(chunk["page_start"], following["page_start"])
            index += 2
            continue
        folded.append(chunk)
        index += 1

    # 第三遍：被图/公式夹住的碎片（一张流程图拆成「开始」「初始化」「判断」几行，
    # 每行前后都是图），前两遍都够不着正文。就近并进 3 条以内最近的正文段。
    index = 0
    while index < len(folded):
        chunk = folded[index]
        if chunk["kind"] != "text" or len(chunk["content"]) >= _TINY_CHARS:
            index += 1
            continue
        target = None
        for distance in (1, 2, 3):
            for offset in (-distance, distance):
                candidate = index + offset
                if not 0 <= candidate < len(folded):
                    continue
                other = folded[candidate]
                if (
                    other["kind"] == "text"
                    and other["chapter"] == chunk["chapter"]
                    and len(other["content"]) + len(chunk["content"]) + 2 <= MAX_CHARS
                ):
                    target = candidate
                    break
            if target is not None:
                break
        if target is None:
            index += 1
            continue
        other = folded[target]
        if target < index:
            other["content"] = f"{other['content']}\n\n{chunk['content']}"
            other["page_end"] = max(other["page_end"], chunk["page_end"])
        else:
            other["content"] = f"{chunk['content']}\n\n{other['content']}"
            other["page_start"] = min(other["page_start"], chunk["page_start"])
        del folded[index]

    for position, chunk in enumerate(folded):
        chunk["seq"] = position
        chunk["chunk_id"] = chunk["chunk_id"].rsplit("-", 1)[0] + f"-{position:04d}"
    return folded


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


def _parse_pdf(data: bytes, asset_dir: Path) -> _Parsed:
    """PDF 解析入口：只有 MinerU 一条路。

    **不设本地兜底**——没有 OCR、没有公式结构的备用管线，兜出来的结果只会让人以为解析成功了。
    本地两档的实现在 `D://pdf解析` 备份着，刻意不 import，免得又把分支带回来。
    """
    return _parse_pdf_mineru(data, asset_dir)

def _clean_heading(text: str) -> str:
    """章节名要纯文本：MinerU 的标题里会夹 `<sup>` 标记和 `$`，直接进引用摘要会显示成
    `指数阶 O(2<sup>n</sup>)`。这里转成 `^` / `_` 的写法，正文元素本身不动（前端能渲染）。"""
    text = re.sub(r"<sup>(.*?)</sup>", r"^\1", text or "")
    text = re.sub(r"<sub>(.*?)</sub>", r"_\1", text)
    text = re.sub(r"<[^>]{1,20}>", "", text).replace("$", "")
    return re.sub(r"\s{2,}", " ", text).strip()


_INLINE_MATH_TYPES = {"equation_inline", "inline_equation", "equation", "math"}


def _inline_text(items: list | None) -> str:
    r"""v2 的行内元素是 `[{type, content}]`，拼成一行文本。

    **`equation_inline` 是裸 LaTeX 源码**，不包 `$` 的话前端不渲染、检索里全是反斜杠噪声
    （实测整库 5749 处，`\boldsymbol`、`\sqrt` 满屏）。包成 `$..$` 交给 marked + KaTeX。
    """
    parts: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content", ""))
        if (
            item.get("type") in _INLINE_MATH_TYPES
            and text.strip()
            and not text.strip().startswith("$")
        ):
            text = f"${text.strip()}$"
        parts.append(text)
    return "".join(parts).strip()


class _HtmlTable(html.parser.HTMLParser):
    """MinerU 的表格以 HTML 给出，转成 markdown pipe 表格才能进 chunk。

    必须展开 `rowspan`：MinerU 把「整数」在 4 行上写成 `rowspan="4"`，不展开就会让后面
    每行少一格、整表左移一列（`short` 的「2 字节」跑到「符号」列）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: dict[int, str] | None = None
        self._cell: list[str] | None = None
        self._rowspan = 1
        self._carry: dict[int, list] = {}
        self._cursor = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("sup", "sub"):
            # 表格里的指数写在 <sup> 里，只收文本会把 2^63 变成 263（等于改错数据）。
            # 标签原样留给 marked 渲染成真上标，和正文里 MinerU 给的写法保持一致。
            if self._cell is not None:
                self._cell.append(f"<{tag}>")
        elif tag == "tr":
            self._row = {}
            for col in list(self._carry):
                value, left = self._carry[col]
                self._row[col] = value
                if left <= 1:
                    del self._carry[col]
                else:
                    self._carry[col][1] = left - 1
            self._cursor = 0
        elif tag in ("td", "th"):
            self._cell = []
            self._rowspan = 1
            for key, value in attrs:
                if key == "rowspan" and value and value.isdigit():
                    self._rowspan = max(1, int(value))

    def handle_endtag(self, tag):
        if tag in ("sup", "sub") and self._cell is not None:
            self._cell.append(f"</{tag}>")
        elif tag == "tr" and self._row is not None:
            width = (max(self._row) + 1) if self._row else 0
            self.rows.append([self._row.get(i, "") for i in range(width)])
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                while self._cursor in self._row:
                    self._cursor += 1
                value = "".join(self._cell).strip()
                self._row[self._cursor] = value
                if self._rowspan > 1:
                    self._carry[self._cursor] = [value, self._rowspan - 1]
                self._cursor += 1
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _html_table_to_markdown(body: str) -> str:
    if not body or "<t" not in body.lower():
        return ""
    parser = _HtmlTable()
    parser.feed(body)
    rows = [row for row in parser.rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").strip()

    lines = ["| " + " | ".join(cell(v) for v in rows[0]) + " |", "|" + "---|" * width]
    for row in rows[1:]:
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def _caption_label(caption: str) -> str:
    """题注 → 图号。不能按空格切：`图0-1 本书主要内容` 切成 `图0-1`，
    但 `图 2-7 算法…` 会只剩一个 `图`——同一套 `_match_caption` 才稳。"""
    if not caption:
        return ""
    hit = _match_caption(caption)
    return hit[0] if hit else "插图"


# 装饰图的几何判据。阈值是拿课件样本量的：装饰 193–296px / 占页面 1.2%–2.9%，
# 真插图最小 606px / 6.5%，中间那段（320–450px、4%–8%）就是「判不了」的灰区。
_DECOR_SHORT_SIDE = 320
_DECOR_AREA_RATIO = 0.04
_DECOR_ASPECT = 8.0
# 灰区的另一头：够大就别问了，直接当插图（一次 VLM 调用 ≈2.3s，全书问一遍比解析还贵）
_ILLUS_SHORT_SIDE = 450
_ILLUS_AREA_RATIO = 0.08
_VLM_ASK_BUDGET = 30  # 单本最多问几次「是不是装饰」；超预算一律保留
_VLM_DESC_BUDGET = 500  # 单本最多生成几条图注；超了退「第 N 页 插图」
_VLM_INTERVAL = 0.3  # 每次调用前的间隔（秒），连着打会被服务端限流
_VLM_FAIL_LIMIT = 5  # 连续失败几次就整本停掉（熔断）；3 太急，一次限流就全军覆没


def _mineru_image_path(media_dir: Path, rel_path: str | None) -> Path | None:
    """MinerU 块里的图片引用 → 真实文件。

    `media_dir` 是**整个临时目录**（每批一个 `batch_N/images/` 子目录），块里的路径形如
    `batch_2/images/xxx.jpg`，所以要先按相对路径找，找不到再退回按文件名找（兼容老格式）。
    不能只取 `Path(rel).name`：MinerU 每批从 0 重新编号，不同批的同名图会被后一批盖掉。
    """
    if not rel_path:
        return None
    parts = [
        part for part in str(rel_path).replace("\\", "/").split("/")
        if part not in ("", ".", "..")
    ]
    if not parts:
        return None
    for candidate in (media_dir.joinpath(*parts), media_dir / parts[-1]):
        if candidate.is_file():
            return candidate
    return None


def _image_size(media_dir: Path, rel_path: str | None) -> tuple[int, int] | None:
    src = _mineru_image_path(media_dir, rel_path)
    if src is None:
        return None
    try:
        from PIL import Image

        with Image.open(src) as img:
            return img.size
    except Exception:  # noqa: BLE001 - 读不出尺寸就当不知道，别因此丢图
        return None


def _bbox_area(bbox) -> float:
    """bbox 是 0-1000 归一化坐标，面积占比直接就能算，不需要真实页面尺寸。"""
    if not bbox or len(bbox) < 4:
        return 1.0
    x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    return max(0.0, (x1 - x0) / 1000) * max(0.0, (y1 - y0) / 1000)


def _figure_verdict(width: int, height: int, area: float, caption: str) -> str:
    """三档：`keep` 明确是插图、`drop` 明确是装饰、`ask` 判不了（交给 VLM）。

    灰区才值得花一次模型调用——大多数图靠几何量就够了。有题注的永远 keep：
    作者写了图号就是插图，这一条不需要问模型。
    """
    if caption:
        return "keep"
    short, long = min(width, height), max(width, height)
    if long / max(1, short) >= _DECOR_ASPECT:
        return "drop"  # 分隔条、页边竖线
    # **两个条件必须同时成立**：单看尺寸会误杀窄高的真流程图，单看面积会误杀整页大图里的居中小图
    if short < _DECOR_SHORT_SIDE and area < _DECOR_AREA_RATIO:
        return "drop"
    if short >= _ILLUS_SHORT_SIDE or area >= _ILLUS_AREA_RATIO:
        return "keep"
    return "ask"


def _looks_decorative(width: int, height: int, area: float, caption: str) -> bool:
    """几何上能确定的装饰图。灰区算 False——它问的是「确定是装饰吗」，不是「像不像」。"""
    return _figure_verdict(width, height, area, caption) == "drop"


def _save_mineru_image(
    media_dir: Path, asset_dir: Path, rel_path: str | None, parsed: _Parsed,
    page: int, caption: str,
) -> str | None:
    """MinerU 的图是 jpg，但 `render_markdown` 固定按 `.png` 拼引用，统一转存 PNG。

    **同一本书里字节相同的图只存一份**：课件/教材里同一张图常出现多次（页眉示意图、
    重复的流程框图），实测 Hello-Algo 一本书就有 148 张重复、49.5MB。重复的判定用 PNG
    字节的 sha1，所以 `_Parsed.asset_hashes` 只在单次解析内有效——重解析会重新算。
    """
    src = _mineru_image_path(media_dir, rel_path)
    if src is None:
        return None
    try:
        from PIL import Image

        buffer = io.BytesIO()
        with Image.open(src) as img:
            rgb = img.convert("RGB")
            rgb.save(buffer, format="PNG")
            width, height = rgb.size
        blob = buffer.getvalue()
    except Exception:  # noqa: BLE001 - 坏图不该让整本失败
        return None

    digest = hashlib.sha1(blob).hexdigest()
    existing = parsed.asset_hashes.get(digest)
    if existing:
        return existing

    asset_id = f"as_{secrets.token_hex(4)}"
    dst = asset_dir / f"{asset_id}.png"
    dst.write_bytes(blob)
    parsed.asset_hashes[digest] = asset_id
    label = _caption_label(caption)
    if label == "插图":
        # `assets.label` 只放**图号**。「插图」是没编号时的兜底文案，写进库就是 795 行
        # 字面量重复（既不能按图号导航，也看不出区别）；没有编号就留空。
        label = ""
    parsed.assets.append({
        "asset_id": asset_id,
        "kind": "figure",
        "label": label or None,
        "caption": caption or None,
        "page": page,
        "file_path": str(dst),
        "width": width,
        "height": height,
    })
    return asset_id


_MINERU_NOISE = {"page_header", "page_number", "page_footer", "header", "footer", "aside_text"}


def _vision_budget():
    """两个模型的闸门：`ask` 判灰区图是不是装饰，`describe` 给无题注图写一句图注。

    两件事都会失败（网络、额度、回答跑偏、服务端限流），处理方式不同：
    - `ask` 拿不到答案 → 保留图（删错了补不回来）
    - `describe` 拿不到答案 → 退回「第 N 页 插图」（至少 chunk 不是空的）

    三个兜底，缺一个都会让解析变慢或变脏：
    - **次数上限**：一本 200 页的课件可能上百张图，不限的话解析会被模型拖到十几分钟；
    - **轻微节流**：连着打会被服务端限流，实测响应从 4s 掉到 40s；
    - **连续失败熔断**：连着 3 张拿不到结果就整本停掉，剩下的一律走占位——
      否则限流时 40 张各等 30s，光等就是 20 分钟。
    """
    import figures

    if not figures.available():
        return None, None
    asks, descriptions, failures = [_VLM_ASK_BUDGET], [_VLM_DESC_BUDGET], [0]

    def _call(func, path: Path):
        if failures[0] >= _VLM_FAIL_LIMIT:
            return None
        time.sleep(_VLM_INTERVAL)
        result = func(path)
        failures[0] = 0 if result is not None else failures[0] + 1
        return result

    def ask(path: Path) -> bool:
        if asks[0] <= 0:
            return False
        asks[0] -= 1
        return _call(figures.judge, path) is False

    def describe(path: Path) -> str | None:
        if descriptions[0] <= 0:
            return None
        descriptions[0] -= 1
        return _call(figures.describe, path)

    return ask, describe


def _parse_pdf_mineru(data: bytes, asset_dir: Path) -> _Parsed:
    """走 MinerU 云端，再把 v2 blocks 折成本项目的元素流。"""
    import pymupdf

    import mineru as mineru_client

    if not mineru_client.available():
        raise DocumentError("没配 MINERU_API_KEY")
    # 每次全新临时目录：清理想做但做不成时（宿主批量删除保护）也不会读到上一轮的旧 json
    workdir = Path(tempfile.mkdtemp(prefix="mineru_parse_"))
    try:
        payload = mineru_client.parse(
            data, "document.pdf", workdir, model=os.getenv("MINERU_MODEL", "vlm")
        )
        pdf = pymupdf.open(stream=data, filetype="pdf")
        try:
            def repair(page_index: int, bbox, text: str) -> str:
                if not 0 <= page_index < pdf.page_count:
                    return text
                return _astral_repair(text, pdf[page_index], bbox)

            ask, describe = _vision_budget()
            return _elements_from_mineru(
                payload, asset_dir, repair=repair, ask=ask, describe=describe)

        finally:
            pdf.close()
    finally:
        # 解压出来的 json + 原图有几十 MB，图已转存到 asset_dir，原件不必留
        mineru_client.purge_dir(workdir)


_UFFFD = "\ufffd"


def _astral_repair(text: str, page, bbox) -> str:
    """MinerU 把**星平面字符**（U+1D400 起的数学斜体字母等，UTF-16 要两个码元）一律吐成 U+FFFD，
    `𝑂(2𝑛)` 变成 `�(2�)`。PDF 文字层里这些字符还在，按块 bbox 取回、NFKC 归一化后回填。

    先按 difflib 对齐填；对齐覆盖不到的再用剩余字符按顺序兜。填不上就原样留着，
    宁可留个 U+FFFD 也不猜错——猜错等于把符号改成了另一个变量。
    """
    if not text or _UFFFD not in text or page is None or not bbox:
        return text
    import pymupdf

    try:
        width, height = page.rect.width, page.rect.height
        x0, y0, x1, y1 = (float(value) for value in bbox[:4])
        clip = pymupdf.Rect(
            x0 / 1000 * width - 2, y0 / 1000 * height - 2,
            x1 / 1000 * width + 2, y1 / 1000 * height + 2,
        )
        reference = page.get_text("text", clip=clip)
    except Exception:  # noqa: BLE001 - 取不到文字层就保持原样
        return text
    pool = [
        unicodedata.normalize("NFKC", char)
        for char in reference
        if ord(char) > 0xFFFF and unicodedata.normalize("NFKC", char)
    ]
    if not pool:
        return text

    out: list[str] = []
    used = 0
    matcher = difflib.SequenceMatcher(None, text, reference, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        segment = text[i1:i2]
        if tag == "equal" or _UFFFD not in segment:
            out.append(segment)
            continue
        candidates = [
            c for c in reference[j1:j2]
            if ord(c) > 0xFFFF and unicodedata.normalize("NFKC", c)
        ]
        candidates = [unicodedata.normalize("NFKC", c) for c in candidates]
        cursor = 0
        for char in segment:
            if char == _UFFFD and cursor < len(candidates):
                out.append(candidates[cursor])
                cursor += 1
                used += 1
            else:
                out.append(char)
    repaired = "".join(out)

    spare = pool[used:]
    if _UFFFD in repaired and spare:
        filled, cursor = [], 0
        for char in repaired:
            if char == _UFFFD and cursor < len(spare):
                filled.append(spare[cursor])
                cursor += 1
            else:
                filled.append(char)
        repaired = "".join(filled)
    return repaired


def _elements_from_mineru(
    payload: dict, asset_dir: Path, repair=None, ask=None, describe=None,
) -> _Parsed:
    parsed = _Parsed(page_count=int(payload["page_count"]))
    media_dir: Path = payload["images_dir"]  # 临时目录根，每批一个 batch_N/images/
    chapter: str | None = None
    for block in payload["blocks"]:
        kind = block.get("type")
        if not kind or kind in _MINERU_NOISE:
            continue
        page = int(block.get("page_idx", 0)) + 1
        content = block.get("content") or {}
        raw_bbox = block.get("bbox")
        bbox = tuple(float(v) for v in raw_bbox) if raw_bbox else None

        def fix(value: str) -> str:
            return repair(page - 1, bbox, value) if repair else value

        if kind == "title":
            text = fix(_inline_text(content.get("title_content")))
            if not text:
                continue
            # 云端会把「图 2-2 是该嵌套循环的流程框图。」这类正文句也标成标题
            if text.endswith(("。", "；", "：")) and len(text) > 20:
                parsed.elements.append(_Element(
                    kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
                continue
            chapter = _clean_heading(text) or text
            parsed.elements.append(_Element(
                kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
        elif kind == "paragraph":
            text = fix(_inline_text(content.get("paragraph_content")))
            if text:
                parsed.elements.append(_Element(
                    kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
        elif kind == "list":
            items = [
                fix(_inline_text(item.get("item_content")))
                for item in content.get("list_items") or []
            ]
            text = "\n".join(f"- {item}" for item in items if item)
            if text:
                parsed.elements.append(_Element(
                    kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
        elif kind in {"code", "algorithm"}:
            body = fix(_inline_text(
                content.get("code_content") or content.get("algorithm_content")
            ))
            if not body:
                continue
            label = content.get("code_language") or ""
            parsed.elements.append(_Element(
                kind="code", content=body, page=page, label=str(label),
                chapter=chapter, bbox=bbox))
        elif kind == "table":
            markdown = _html_table_to_markdown(str(content.get("html") or ""))
            if not markdown:
                # 复杂表只给了截图、没给 HTML：退化成图，别整块消失
                if content.get("image_source"):
                    kind = "image"
                else:
                    _log.debug("表格既没有 html 也没有截图，跳过（第 %d 页）", page)
                    continue
            else:
                # 表格里的 U+FFFD 也要回填：MinerU 会把 `𝑂(2𝑛)` 这类星平面字符吐成替换符，
                # 表格行同样中招（Hello-Algo 就有）
                caption = fix(_inline_text(content.get("table_caption")))
                text = f"{caption}\n\n{fix(markdown)}" if caption else fix(markdown)
                parsed.elements.append(_Element(
                    kind="table", content=text, page=page, label=caption or None,
                    chapter=chapter, bbox=bbox))
                continue

        if kind == "image" or kind == "chart":
            # chart 是「算法画的图」，和 image 同构（都有 image_source + 题注），
            # 早先没接这一类，等于整张图连同题注一起消失。
            source = content.get("image_source") or {}
            rel = source.get("path")
            caption = fix(_inline_text(
                content.get("image_caption") or content.get("chart_caption")))
            path = _mineru_image_path(media_dir, rel)
            if not caption and path is not None:
                size = _image_size(media_dir, rel)
                if size:
                    verdict = _figure_verdict(size[0], size[1], _bbox_area(bbox), caption)
                    if verdict == "drop":
                        continue  # 明确的装饰：不存图、不产 chunk、不占 assets
                    if verdict == "ask" and ask and ask(path):
                        continue  # 灰区被判成装饰
                # 无题注的真插图：先让模型写一句「这图讲什么」，写不出来再退占位。
                # 占位能救「chunk 正文为空」，但没有语义，模型看不出该不该展示这张图。
                if describe:
                    caption = describe(path) or ""
                if not caption:
                    caption = f"第 {page} 页 插图"
            asset_id = _save_mineru_image(
                media_dir, asset_dir, rel, parsed, page, caption)
            parsed.elements.append(_Element(
                kind="figure", content=caption, page=page, asset_id=asset_id,
                label=_caption_label(caption) or None,
                chapter=chapter, bbox=bbox))
        elif kind in {"equation_interline", "equation", "inline_equation"}:
            # 行间公式是独立块（`math_content` 是 LaTeX 源码），之前没有分支，
            # 整条公式直接丢了。包成 `$$..$$` 才能被前端和切分当成公式。
            latex = str(content.get("math_content") or content.get("equation_content") or "").strip()
            if not latex:
                continue
            latex = fix(latex)
            if not latex.startswith("$"):
                latex = f"$${latex}$$"
            parsed.elements.append(_Element(
                kind="formula", content=latex, page=page, chapter=chapter, bbox=bbox))
        else:
            # 静默丢弃会让「少了一块」永远查不出来，至少留个线索
            _log.debug("MinerU 未处理的块类型：%s", kind)
    return parsed


_SENTENCE_END = "。！？；：.!?;:"


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
