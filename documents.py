"""教材库：把 PDF / PPTX / DOCX 变成「可检索的段落」和「可引用的图」。

这是应用层模块，和 `memories.py` / `conversations.py` 同一个位置：只读写
`data/` 下的文件与 SQLite，**不 import 任何 agent 相关的东西**，因此也能被
命令行、探针、离线批处理直接调用。

## 这个模块只做三件事

1. **收文件**：算内容指纹去重、落盘、写一行 `pending`（A1/A5）。
2. **解析**：切段落、抽图、配对图注，写完把状态推到 `ready` 或 `failed`（A2/A6/A7）。
3. **存取**：列表、删除、按用户取段落（A3/A4）。

检索、排序、引用编号**不在这里**——那是 `retrieval.py` 的事。这条边界要守住：
一旦把「哪个段落更相关」写进来，删书、换切分策略、做离线评测就都得穿过检索逻辑。

## 为什么解析和收文件要分开

一次 300 页 PDF 的解析要几十秒。如果放在上传请求里同步做完，浏览器会卡在
一个转圈的框上，而且中间崩了什么都没留下。所以：

- `create_upload()` 只做落盘 + 写 `pending`，**毫秒级返回**；
- `parse_document()` 是重活，由应用层丢进线程池（`asyncio.to_thread`）。

代价是可能出现「写着 pending 但没人解析」的僵尸行（进程恰好在这时重启）。
`reconcile_pending()` 就是为此存在的：启动时把超时未完成的置为 `failed`，
**不让它永远停在「解析中」**——这是 A2 的验收标准里唯一需要主动兜底的一条。

## 切分的目标不是「切得均匀」，是「切出来能引用」

一条 chunk 要能独立回答一个问题，所以：

- **不跨章合并**：翻章处强制断开，否则「第 3 章的抽样定理」会带上第 4 章的开头；
- **允许跨页合并**：教材的段落本来就跨页，硬按页断会把一句话切成两半；
- **图注和图是同一条**：图注单独成 chunk 没有意义（谁也不知道那张图长什么样），
  所以图注 + 截图合成一条 `kind='figure'` 的 chunk，图注文本就是它的 content。

页码记 `page_start` / `page_end` 两个，因为跨页 chunk 只记一个页码必然是错的。
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

# 挂在 api.py 配置的 "agent" 下面：这一层不自己配 handler，
# 只往上冒泡（`agent` 已经有 handler 且 propagate=False，不会重复打印）。
_log = logging.getLogger("agent.documents")

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "documents.sqlite3"
UPLOAD_DIR = DATA_DIR / "uploads"
ASSET_DIR = DATA_DIR / "assets"

# 单本教材的大小上限。教材扫描版 PDF 常见 50-200MB。
#
# 原来卡在 80MB，实测 Hello-Agents（720 页、97.9MB）**直接被拒**——而它正是
# 这个项目最想支持的那类书。上限的成本不在磁盘，在内存：`create_upload` 会把
# 整个文件读进来算指纹，250MB 的文件峰值大约多占 250MB 内存，可以接受。
# 150→250（2026-10-21）：概率论教材 225MB 被挡在门外，这种大书是图片多不是页数多，
# PyMuPDF 按页流式处理，内存峰值可控。
MAX_FILE_BYTES = 250 * 1024 * 1024

# 允许的扩展名。**只认这三个**，不做「猜格式」：猜错会得到一堆乱码段落，
# 比直接拒绝难排查得多。
SUPPORTED_SUFFIXES = {".pdf", ".pptx", ".docx"}

# 解析状态。`pending` 是「已收、未解析」，`parsing` 是「正在解析」。
# 分成两个不是为了好看：`parsing` 才代表「有进程正在干这件事」，
# 崩溃恢复时只有 `parsing` 才是可疑的（`pending` 可能只是排在队列里）。
STATUS_PENDING = "pending"
STATUS_PARSING = "parsing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# 超过这么久还停在 parsing，就认为那次解析随进程一起死了。
STALE_PARSING_MINUTES = 30

# 切分参数（字数，按字符数算，中文一个字算一个）。
#
# TARGET 是「差不多该断了」，MAX 是「必须断了」——两者分开是为了尽量在句号处断，
# 而不是在任意位置硬切。MIN 以下不与下一段分开（几个字的残段没有检索价值）。
#
# 250 的依据（2026-10-21 对照实验，probe-chunksize.py）：500→250 后纯向量有用段
# 13→26、top1 4→9——病根是长尾混话题长段稀释了段向量；实际均值只从 265 降到 207。
# 代价对策：喂模型前做短块邻居扩展（retrieval._expand_neighbors），检索粒度和
# 喂模型粒度分开——这是 WeKnora「检索碎、合并厚」的同款设计。
TARGET_CHARS = 250
MAX_CHARS = 900
MIN_CHARS = 80

# 图注识别。**只认独占一行的题注**：正文里「如图 3-4 所示」这种不是题注，
# 拿它去截图只会截到一句话。
#
# 编号分隔符要收全：教材排版里 `2-5` 的连字符经常不是 ASCII 的 `-`，
# 而是 U+2011（不换行连字符）或 U+2013 / U+2014。只写 `-` 会把「图 2-5」
# 拆成编号 `2` 和题注 `-5 尾递归过程`——现象是图全对、编号全错，
# 而编号错了引用就没法按「图 2-5」对上书里的图。
_CAPTION_RE = re.compile(
    r"^\s*(图|表|Figure|Fig\.?|Table)\s*[（(]?\s*"
    r"([0-9]+(?:\s*[-－.．–—‑]\s*[0-9]+)*|[A-Z]\s*[-－.．–—‑]\s*[0-9]+)"
    r"\s*[)）]?\s*[:：.．、]?\s*(.*)$",
    re.IGNORECASE,
)
# 题注以这些词开头时，它是**正文在引用图**，不是题注本身。
# 「图 2-7 展示了以上三个算法的时间复杂度。」——这一行本来就在正文里，
# 当成题注会去截一张不存在的图（截到隔壁的图），并且编号还会重复。
_CAPTION_VERBS = (
    "所示", "中", "为", "是", "表示", "展示", "给出", "描述", "说明", "显示",
    "列出", "对比", "描绘", "列举", "呈现", "可见", "反映了", "是一种",
)
# 公式编号行：整行只剩下「(3-12)」「（3.12）」这种。
_FORMULA_NO_RE = re.compile(r"^\s*[（(]\s*([0-9]+(?:\s*[-－.．–—‑]\s*[0-9]+)+)\s*[)）]\s*$")
# 章节标题的正则兜底（PDF 没有书签目录时才用）。
_HEADING_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百]+\s*[章节篇部]"
    r"|[0-9]+(\.[0-9]+){1,2}\s*[、.．]?\s*\S"
    r"|Chapter\s+[0-9]+)",
    re.IGNORECASE,
)
# 页码：`12`、`- 12 -`、`第 12 页`、`12 / 300`。
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

# 初始化锁。理由与 `memories.py` 相同：数据操作的原子性由 SQLite 保证，
# 但「建文件 + 建表」这个动作不是原子的，两个线程同时进来会撞。
_init_lock = threading.Lock()


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """四张表都在才算就绪。

    **加了新表就必须把它加进来**：建表只在「不就绪」时跑，只判 `documents` 的话，
    一个已有的老库永远进不了建表分支，`categories` 建不出来，
    然后每次点「分类」都报 `no such table`。和 `memories` 补 `id` 列、
    `users` 补 `invites` 表是同一个坑，这已经是第三次踩，所以这次一次判全。
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
    """拿一个连接。建库建表在一次加锁里完成，之后的读写各自一个新连接。

    每个调用一个新连接而不是共享一个：SQLite 的连接不是线程安全的，而这个模块
    会被 `asyncio.to_thread` 在线程池里调用——线程池的线程是复用的，共享连接
    迟早撞上「另一个线程正在用」。连接本身很便宜（微秒级）。
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
    """给老库补新列。

    `CREATE TABLE IF NOT EXISTS` 只在**没有这张表**时生效，已经存在的库不会自己
    长出 `markdown` 列。项目的库里已经躺过真实数据（虽然现在清空了），删库重来
    不是选项，所以每次连接都检查一次——`PRAGMA table_info` 是微秒级的。

    这也是这个项目第一次真正需要迁移：以前加表都是新表，`IF NOT EXISTS` 就够了。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "markdown" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN markdown TEXT")
    if "warning" not in columns:
        # 「解析完成但有瑕疵」需要一个和 error 分开的家：error 在 ready 行上恒为空，
        # 把提示写进 error 会让「有错」和「能用但要注意」这两件事混成一件。
        conn.execute("ALTER TABLE documents ADD COLUMN warning TEXT")
    if "category_id" not in columns:
        # 可空 = 未分类。老库里的书全部落到未分类，这是**正确**的默认：
        # 我们没有任何依据替用户决定这本书属于哪一科。
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


# --------------------------------------------------------------------------
# 收文件（A1 / A5 / A2）
# --------------------------------------------------------------------------

def create_upload(user_id: str, filename: str, data: bytes) -> dict[str, object]:
    """收下一个文件，落盘并写一行 `pending`。返回这条记录（含是否重复）。

    **去重按内容指纹，不按文件名**：同一个 PDF 改名再传一次不该变成两本；
    反过来，两个不同文件同名也必须都收下。

    重复上传时**不覆盖原记录**：原来已经解析好了，重新解析一遍纯属浪费，
    而且会让这期间产生的引用（chunk_id）全部失配。所以直接返回老记录，
    带 `duplicated=True` 让上层决定怎么提示。
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
        # 存原始文件的用途是「重新解析」：切分策略改了之后不用让用户再传一遍。
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


# --------------------------------------------------------------------------
# 解析（A2 / A6 / A7）
# --------------------------------------------------------------------------

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
    # 这一条占的版面范围（PDF 坐标），只有图和公式用得上，用于判断谁在上谁在下。
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class _Parsed:
    page_count: int
    elements: list[_Element] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    toc: list[list] = field(default_factory=list)
    # 解析完成、但结果有已知瑕疵时的提示（例如「检测到双栏，按单栏解析」）。
    # 和 `error` 分开：`error` 意味着这本书用不了，`warning` 意味着能用但要知道。
    warning: str | None = None
    # 一页一个主题（幻灯片）。为真时切分**按页硬断**，不允许跨页合并——
    # 两页 PPT 合成一条 chunk 会让「这页讲的是什么」变成「这两页混起来讲什么」。
    # 不能只看后缀：老师给的课件常常是 .pdf（PPT 导出/打印），后缀判断不出来。
    section_per_page: bool = False


def parse_document(doc_id: str) -> dict[str, object]:
    """把一本 `pending` 的书解析完，返回最终记录。

    **这个函数是阻塞的**（CPU + 磁盘，可能几十秒），调用方必须丢线程池。
    失败时把原因写进 `documents.error` 并置 `failed`——不抛异常出去，
    因为调用它的是一条后台任务，抛出没人接。

    产出**两份**东西，一份存一份用：
    - `documents.markdown`：带页码/章节锚点的全文，落库。它是**中间态**，
      也是给用户看的原文、给导出用的底稿；
    - `chunks`：从那份 Markdown 切出来的检索单元。

    **切分只读 Markdown，不读解析器**——这是这次重构的全部意义。之前切分只能
    紧跟在解析后面跑（段落、页码、图号都在内存的元素流里，程序结束就没了），
    所以哪怕只想调一下「目标长度 500 还是 300」，也得把整本 PDF 重解析一遍。
    现在 `rechunk()` 只在文本上跑一遍正则，一个 300 页的书不到一秒。
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
        # 一页一个主题就按页断开（PPT，以及 PPT 导出的 PDF）；连续文本允许跨页合并。
        # 判据由解析器给（`parsed.section_per_page`），不看后缀：
        # 老师的课件常常是 .pdf，光看扩展名永远判不成幻灯片。
        chunks = cut_chunks(
            markdown, doc_id, section_per_page=(kind == "pptx" or parsed.section_per_page)
        )
        _store(doc_id, parsed, markdown, chunks)
    except DocumentError as exc:
        _fail(doc_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - 解析器内部的任何意外都要落成 failed
        # 这里刻意 catch 宽泛：第三方解析库对畸形 PDF 抛的异常五花八门，
        # 一旦漏出去，这本书就永远停在 parsing。
        #
        # 存的是「原因」，不带「解析失败」四个字——前端已经有那个标签了，
        # 带上会显示成「解析失败 · 解析失败：FileDataError: 损坏」。
        #
        # 同时落一条日志：状态里只有一行摘要（给用户看的），排查需要 traceback
        # （给维护者看的）。这两件事受众不同，别指望一个字符串同时满足。
        _log.exception("解析失败 doc=%s", doc_id)
        _fail(doc_id, f"{type(exc).__name__}: {exc}")

    with _connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return _row_to_doc(row)


def rechunk(doc_id: str) -> dict[str, object]:
    """只重切，不重解析。用于调整切分参数后批量重跑。

    **这是 Markdown 中间态带来的那个能力**，也是它唯一不可替代的价值：
    解析 PDF 要几十秒还要原文件，重切只要读一列文本 + 一遍正则。

    不碰 `assets`：图是解析产物，重切不该动它（重切只改变「怎么分段」，
    不改变「书上有哪些图」）。
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
    """启动时把「解析中」但明显已经死掉的行置为失败。返回处理条数。

    判据只有一条：`status='parsing'` 且 `updated_at` 超过 `STALE_PARSING_MINUTES`。
    刻意**不动 `pending`**——它可能只是排着队还没轮到；而 `parsing` 意味着
    确实有进程在跑，进程死了才会一直停在这里。
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


# --------------------------------------------------------------------------
# 查询与删除（A3 / A4）
# --------------------------------------------------------------------------

def list_documents(user_id: str) -> list[dict[str, object]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_doc(row) for row in rows]


# --------------------------------------------------------------------------
# 分类（知识库管理页）
# --------------------------------------------------------------------------
#
# 一本书最多属于一个分类（`documents.category_id`），为空就是未分类。
# 不用多对多：用户说的是「把一个学科的几个 PDF 归到一个分类下」，
# 一本书同时属于两个学科是少数情况；而多对多会带来「删分类时书还有别的家吗」
# 这类判据，界面上也要处理「这本书在两个分类里都出现」。先做简单的。

# 分类名上限。够写「信号与系统（2024 秋）」这种，又不至于把侧栏撑爆。
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
    """这个人的全部分类，带上每类有几本书。按创建先后排，新的在后。

    用 `LEFT JOIN` 而不是「先查分类再逐个查书数」：后者是 N+1 次查询，
    分类一多就明显（虽然现在只有十几个）。`d.user_id = c.user_id` 那个条件
    不能少——它保证只数**这个人自己的**书，否则一本异常数据会让别人的计数串过来。
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
    """删分类，**类下的书退回未分类，不跟着删**。返回它此前是否存在。

    这个取舍是刻意的：书是几十 MB 解析出来的贵重资产，而分类只是一个标签。
    「删掉一个标签顺手删掉 8 本书」是那种点错了就没法挽回的操作。
    真要删书，在书自己身上点删除——那里删什么是明确的。
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
    """把一本书挪到某个分类（`None` = 挪回未分类）。

    返回 `"ok"` / `"missing"`（没这本书）/ `"no_category"`（目标分类不存在或不是他的）。

    **两个 id 都要按 `user_id` 验一遍**：只验书不验分类的话，别人可以把自己的书
    挂到我的分类 id 上（虽然界面看不出来，但计数会乱）。
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
    """删书：数据库行 + 原文件目录 + 图目录，一起删。返回它此前是否存在。

    先删数据库再删文件：反过来的话，文件删成功、数据库删失败，会留下一条
    「状态 ready 但文件不存在」的记录，之后每次检索都要处理这种悬空引用。
    留着孤儿文件（数据库已删、磁盘还在）只是浪费空间，不会让逻辑出错。
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


# --------------------------------------------------------------------------
# Markdown 中间态：解析产物与切分之间的唯一接口
# --------------------------------------------------------------------------
#
# 这一层存在的理由只有一条：**切分不该依赖解析器**。
#
# 解析 PDF 要原文件、要 pymupdf、要几十秒；切分只是「把长文本分成 500 字的段」。
# 之前两件事焊在一起（元素流只在解析过程中存在），所以调一个切分参数就得整本重解析。
#
# 现在解析的终点是一份 Markdown，切分的起点也是它。中间的信息（页码、章节、
# 图号）靠 HTML 注释锚点传递：
#
#     <!-- page:41 -->
#     <!-- chapter:3.2 低通抽样 -->
#
#     正文段落……
#
#     ![图 3-4 抽样过程的示意](as_77.png)
#
# 用注释而不是自定义标记（`{{page:41}}`）有两个好处：任何 Markdown 渲染器都会
# 把注释隐藏掉，所以这份文本可以直接给用户看、给导出用；而机器抓它只需要一条
# 行级正则。
#
# **锚点格式是存储契约**：已经入库的书按这个格式存着，改格式就得写迁移。

_PAGE_ANCHOR_RE = re.compile(r"^<!--\s*page:(\d+)\s*-->\s*$")
_CHAPTER_ANCHOR_RE = re.compile(r"^<!--\s*chapter:(.+?)\s*-->\s*$")
_IMAGE_LINE_RE = re.compile(r"^!\[(.*?)\]\(([^)]+)\)\s*$")

# 图引用的「可替换片段」。**公开**（不带下划线），因为把它换成可访问的 URL 是
# 应用层的事（要签名），而认哪个片段属于这一层——两处各写一份正则迟早对不上，
# 表现是「有的图能显示、有的不能」。
#
# 只匹配本模块自己生成的形态 `as_xxxx.png`：绝对路径、`http(s)://` 一律不碰，
# 免得把正文里本来就有的外链图当成自家 asset。
#
# 分三组，**第二组是 asset_id 本身**（`.png` 后缀固定，不跟进去）：替换方只要
# 拼「前缀 + 新 URL + 后缀」，不必关心扩展名怎么处理。
ASSET_REF_RE = re.compile(r"(!\[.*?\]\()(as_[0-9a-f]{4,})\.png(\))")

# 条目符号的字符集。两处共用：解析时认「这一行新起一条」，渲染时换成统一的 `- `。
#
# 私用区 U+F000–U+F8FF 是 Wingdings 字形：字体里没有对应字，用户看到的是一串
# 方块，模型读到的也是一个没有意义的码位。所以**必须**在渲染时换掉，而不是
# 原样存进 Markdown。名单是照着真实课件补出来的，不是抄「常见项目符号表」。
_BULLET_GLYPHS = "\uf000-\uf8ff➢➤►▶▸◆◇▪▫●○•·❖"

# 行首条目符（含其后可能有的空格/全角空格），渲染时整体换成 `- `。
_BULLET_LINE_RE = re.compile("^[ \t\u200b]*[" + _BULLET_GLYPHS + "][ \t\u3000]*")


def normalize_bullets(text: str) -> str:
    """把行首的条目符换成 Markdown 的 `- `。

    一条要点在 chunk 里长什么样，决定了它能不能被检索到：`➢要求稳定可靠` 里的
    箭头既占字符又干扰分词，私用区那几个干脆是方块。统一成 `- ` 之后，正文书里的
    无序列表和幻灯片的要点在存储里是同一种形态，下游（检索、引用、导出）只认一种。
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
    """元素流 → 带锚点的 Markdown。这是解析的**唯一出口**。

    页码锚点在每页第一个元素前打一次；章节锚点只在章节变化时打一次。
    两者都不针对每个元素重复——一份 300 页的书如果有 3000 个元素，
    每段都带锚点会让文本里一半是注释。
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
            # 图片用「asset_id.png」而不是绝对路径：这份 Markdown 会跟着 doc_id
            # 目录一起搬（备份、迁移），绝对路径一搬就断。
            parts.append(f"![{element.content}]({element.asset_id}.png)")
            continue
        if element.kind == "code":
            # **不能用 strip()**：代码的缩进是语义的一部分，只去掉尾部的空行。
            parts.append(f"```{element.label or ''}\n{element.content.rstrip()}\n```")
            continue
        body = normalize_bullets(element.content.strip())
        if body:
            parts.append(body)
    return "\n\n".join(parts)


def markdown_elements(markdown: str) -> list[_Element]:
    """Markdown → 元素流。`render_markdown` 的逆过程。

    这一遍解析刻意**不做任何智能判断**：不猜标题、不合并段落，只把注释读成
    「当前位置」、把空行读成「换个元素」。所有关于「怎么切」的思考都留给
    `chunk_elements`，这样两个步骤各自可测。
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
        # 代码围栏优先：围栏内部是**原样文本**，空行、`|` 开头、缩进都不能
        # 触发别的规则。所以这一支必须在所有分支之前。
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
            # 表格是一整块：中间的空行、`| --- |` 分隔行都属于它。
            if buffer_kind != "table":
                flush()
                buffer_kind = "table"
            buffer.append(line)
            continue

        if buffer_kind == "table":
            flush()
        buffer.append(line)
    if in_code and buffer:
        # 围栏没闭合（正常解析产出的 Markdown 不会出现，防的是手改过的文件）
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


# --------------------------------------------------------------------------
# 切分：元素流 → chunk
# --------------------------------------------------------------------------

def cut_chunks(markdown: str, doc_id: str, *, section_per_page: bool = False) -> list[dict]:
    """Markdown → 段落列表。**只读文本，不碰解析器**。

    `section_per_page` 是「每页必须断开」，只有 PPT 用。
    它原来是 `_Element` 上的一个标志，重构后**必须挪到这里**：标志存在元素上时，
    经过 `render_markdown` 就丢了（Markdown 里没有这个字段），于是 PPT 的三页
    又被合并成一条。而「页面之间要不要断」本来就是切分策略，不是元素的属性，
    放在这里才是它该在的地方。
    """
    return _cut_chunks(markdown_elements(markdown), doc_id, section_per_page=section_per_page)


def _cut_chunks(
    elements: list[_Element], doc_id: str, *, section_per_page: bool = False
) -> list[dict]:
    """元素流 → 段落列表。

    单遍扫描，三条规则按顺序生效：

    1. 图/公式**独占一条**（它自带 asset 引用，混进正文会让引用位置失真）；
    2. 长度够了（60% TARGET）之后，章变了就断；
    3. 长度到 TARGET 且刚好处在句末就断；到 MAX 无条件断。

    规则 2 里的「长度够了」不能省。第一版是「章变了就断」，实现在 Hello-Algo
    上跑出 94 条正文、平均 213 字——因为每一小节（2.3.1、2.3.2……）都会断一次，
    400 字的 TARGET 形同虚设。这正是「切得均匀」和「切得能引用」的冲突：
    短小节强行断开只会得到一堆没有上下文的小块，而小块检索命中后给不出答案。

    规则 3 的两个阈值分开，是为了「尽量在句号处断」——否则模型拿到的段落
    开头是半句话，答案也就跟着断章取义。

    表格是**原子块**：它不能被 `_split_long` 按句子切开（markdown 表格被拦腰
    截断就成了乱码），所以走单独一条分支——放得下就并进当前 chunk，放不下
    或者紧接着就 flush（不让表格吞掉后面的正文）。
    """
    chunks: list[dict] = []
    buffer: list[_Element] = []
    buffer_len = 0
    current_chapter: str | None = None
    current_page = 0
    # 章节可以断章的最低长度。低于它时换章只更新标记、不断开。
    chapter_cut = int(TARGET_CHARS * 0.6)

    def flush() -> None:
        nonlocal buffer, buffer_len
        if not buffer:
            return
        text = _join_elements(buffer)
        if text.strip():
            # 整条都是表格时标成 table，下游可以据此决定要不要原样展示
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
            # 表格和图片一样**独占一条**：它是自包含的信息单元（有表头、有行列），
            # 混进正文会被切分规则拦腰截断（markdown 表格断在一半就是乱码），
            # 而且引用「第 58 页表 3-1」会指向一大段混合文本。
            flush()
            chunks.append(
                _make_chunk(doc_id, chunks, [element], element.content, kind="table")
            )
            continue

        if element.kind == "code":
            # 代码同理，而且更严重：截在中间就是语法错误，模型读了会照着错的学习。
            flush()
            chunks.append(
                _make_chunk(doc_id, chunks, [element], element.content, kind="code")
            )
            continue

        # 每页硬断（PPT）：和「章变了」不同，这个不看长度。一页幻灯片是一个主题，
        # 两页合并会让一条 chunk 里同时装着「抽样定理」和「量化」。
        if section_per_page and element.page != current_page and buffer:
            flush()
            current_page = element.page
        current_page = element.page

        if element.chapter != current_chapter:
            if buffer_len >= chapter_cut:
                flush()
            current_chapter = element.chapter

        # 超长段落先按句切，避免单条 chunk 撑爆上下文
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

    短片段来自两处：图前面的最后一句（图必须独占一条，只能把它挤出来）、
    以及章尾的几行。它们单独成条时检索能命中但答不出东西，还会白占引用编号。

    **只合并正文，不动图/公式**：图的 chunk 里 content 是题注，并进正文反而
    会让 asset 引用出现在一段无关的文字里。跨章不合并——那正是要防的事。
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

    # 合并之后 seq 出现了空洞，而 chunk_id 是拿 seq 拼的，所以统一重排。
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
        # chunk_id 里带 doc_id：它是**全局唯一**的引用锚点，不依赖「本次检索的第几条」。
        # 短编号 c1/c2 是每轮对话的临时映射，由检索层负责，不落库——落库的话，
        # 同一段话在不同轮次会有不同 id，历史消息里的引用就再也对不上了。
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


# --------------------------------------------------------------------------
# PDF 解析
# --------------------------------------------------------------------------

def _require(module: str, hint: str):
    """延迟 import 第三方解析库。缺了给出**能照做的**提示，而不是 ModuleNotFoundError。"""
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise DocumentError(f"缺少解析依赖 {module}，请先安装：{hint}") from exc


def _looks_two_column(blocks: list, width: float) -> bool:
    """这一页是不是双栏排版。

    判据不看「有没有两列文字」，而是**文字块左边缘有没有一条明显的分界缝**：
    单栏时所有块的 x0 挤在一起（同一页边距），双栏时会出现两组，中间空一大块。

    三个门槛缺一不可：
    - 块数 ≥ 6：太少时一两个居中图注就能造出「间隙」；
    - 间隙 > 页宽 15%：普通缩进/列表不会拉这么开；
    - 左边贴着页边距、右边落在页面右半：排除「居中大标题 + 正文」这种伪双栏。
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
    """解析前的体检：先判断这本 PDF 属不属于「我们能解析的那类」。

    探 5 页（跳过封面两页，之后均匀取），算两个数：

    - `text_ratio`：有文字层的页占比。接近 0 就是扫描版（整页是图）；
    - `columns`：1 或 2，按文字块左边缘的分布判断。

    **为什么必须先探再解析**：这两个条件不成立时，解析器不会报错——它会安安静静
    产出一份读不通的文本，状态还是 `ready`。实测 3 页纯图 PDF 得到的是
    「已就绪 · 0 段」，用户只能猜是哪里坏了。宁可拒绝，也别给一份假成品。
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

    # 幻灯片：横版 + 每页内容少。两者缺一不可——横版的教材（大开本）字数不会低。
    # 实测：PPT 导出的课件是 720x540（比例 1.33）、每页 256 字；
    # 两本教材是竖版（比例 0.77）、每页 1132 / 636 字，分界很宽。
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

        # 体检不过就直接拒绝，别产出一份「已就绪但读不通」的文本。
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

        # 有书签目录就信目录；没有就只能靠字号猜（`_looks_heading`）。
        # 两条路的优先级不能反：字号猜出来的「标题」经常是封面上的书名、
        # 或者加粗的强调句，而目录是编辑器里人工维护的结构。
        has_toc = any(len(entry) >= 3 and isinstance(entry[2], int) for entry in toc)
        # 正文字号**总是**要算：代码块判定的第三条判据（代码字号常比正文小）
        # 要用它。而标题猜测只在没有目录时才允许，所以另给一个 `heading_size`。
        body_size = _pdf_body_size(pdf)
        heading_size = 0.0 if has_toc else body_size
        body_left = _pdf_body_left(pdf)
        chapter: str | None = None
        # 母版图只在幻灯片上才是问题（书里的图重复出现往往是真的重复引用，
        # 而且书的图靠题注配对，走不到这条路径）。全本扫一遍 xref 也不便宜，
        # 所以按需算。
        template_images = (
            frozenset(_pdf_template_images(pdf)) if parsed.section_per_page else frozenset()
        )

        for page_index in range(page_count):
            page = pdf[page_index]
            page_no = page_index + 1
            lines = _pdf_page_lines(page, junk, tight_cross_block=parsed.section_per_page)
            if has_toc:
                from_toc = _chapter_at(toc, page_no)
                # 目录只标章首页的页码，两章之间的页要沿用上一页的章名，
                # 所以拿不到时**不要**把 chapter 清成 None。
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
        # 目录只留下 `(级别, 标题, 页码)` 三元组。畸形 PDF 的书签可能是二元组
        # （没有页码），那种条目算不出章节归属，留着只会让 `_chapter_at` 多一条分支。
        parsed.toc = [
            [int(entry[0]), str(entry[1]), int(entry[2])]
            for entry in toc
            if len(entry) >= 3 and isinstance(entry[2], int)
        ]
        return parsed
    finally:
        pdf.close()


def _pdf_template_images(pdf, ratio: float = 0.3) -> set[int]:
    """反复出现在**大多数页**上的位图：母版背景、校徽、装饰条。

    幻灯片每一页都盖着同一层母版图。把它当插图抽出来，等于每页凭空多一张
    没有信息的图——marker 的 32 页输出里就有 32 张一模一样的 `Picture_0`。
    真正的内容图（新闻截图、示意图）通常只出现在一两页，所以按「出现页数
    占全本的比例」就能分开，不需要认图。
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
    """正文左边缘：抽若干页，取出现次数最多的行左边缘。

    和 `_pdf_body_size` 同一个思路——一本书的正文列是全局一致的。
    这个值用来把**代码块**和「正文里的行内代码」分开：代码块整体比正文右移
    一个容器的内边距（Hello-Agents 13pt、Hello-Algo 20pt），而行内代码
    就贴在正文列上。
    """
    counter: Counter[float] = Counter()
    for page_index in range(min(sample_pages, pdf.page_count)):
        for line in _pdf_raw_lines(pdf[page_index]):
            if len(line[4].strip()) >= 8:
                counter[round(line[0], 0)] += 1
    if not counter:
        return 0.0
    return counter.most_common(1)[0][0]


# 等宽字体的家族关键词。命中的行可能是代码，但**行内代码**也命中，
# 所以还要配合左边缘判断（见 `_pdf_page_elements`）。
_MONO_HINTS = (
    "menlo", "mono", "consol", "courier", "code", "hack", "fira",
    "jetbrains", "cascadia", "sarasa", "inconsolata", "sourcecodepro",
)


def _mono_share(line: dict) -> float:
    """一行里等宽字体占的字符比例。

    **不能按「主字体」判断**：两本书的正文恰好相反——
    Hello-Agents 用 Type3（每个字形一个字体名，碎得离谱），等宽字体反而
    在「单字体名字符数」上胜出；Hello-Algo 用 NotoSansSC（一整块覆中文），
    含中文注释的代码行反被判成正文。按比例算就不会两个方向都踩。
    """
    mono = total = 0
    for span in line.get("spans", []):
        count = len(span.get("text", ""))
        total += count
        if any(hint in span.get("font", "").lower() for hint in _MONO_HINTS):
            mono += count
    return (mono / total) if total else 0.0


# 括号/引号没闭合 → 上一行是「话没说完」，下一行是它的物理折行（PDF 页宽不够）。
# 判据要**同时**满足「本行不更靠右」，否则多行字典 `x = {` 的下一层缩进会被误合并。
_OPEN_BRACKETS = "([{"
# 折行判断用的括号配对。抽成模块常量，免得函数里再套一层元组——那层括号
# 写错时语法报错会指向上一行，很难找。
_BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))


def _unbalanced(code: str) -> bool:
    text = code.replace('"""', "").replace("'''", "")
    if any(text.count(opening) != text.count(closing) for opening, closing in _BRACKET_PAIRS):
        return True
    return text.count('"') % 2 == 1


# 章节标题的**强**线索：编号形式。「第 3 章」「2.3.1」「Chapter 4」这类。
# 教材的小节几乎都带编号，而正文里的列表项、公式、表注、提示框标签不带，
# 所以这条比「字号大」可靠得多。
_HEADING_STRONG_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百]+\s*[章篇部]"
    r"|\d+\s*\.\s*\d+"
    r"|Chapter\s+\d+)",
    re.IGNORECASE,
)
# 提示框标签 / 摘要这类英文短词，字号常常和标题一样大。
_PANEL_LABEL_RE = re.compile(
    r"^\s*(Tip|Note|Question|Warning|Abstract|Summary|Example|Attention)\b", re.IGNORECASE
)


def _looks_heading(text: str, size: float, body_size: float) -> bool:
    """没有目录时判断一行是不是章节标题。

    **只认编号形式的标题**，不靠「字号大 + 行短」硬猜。第一版就是靠字号猜的，
    在一个 12 页的样本里造出 26 个「章节」，其中一半是假标题：

        Tip / Question                   提示框的标签
        表2-1 迭代与递归特点对比          表注
        1 + 1 + 10 + (1 + 5) × 𝑛= 6𝑛+ 12  公式行
        1. 第一步：统计操作数量           编号列表项

    假标题的危害不是「名字不好听」，是**它后面的正文全归到它名下**——引用时
    「出自哪一节」整段错掉，而 C2 的验收标准就是页码章节要和书一致。

    所以判据反过来：先看有没有编号（强线索），没有编号的必须字号明显更大
    且极短（弱线索，用来兜住「尾递归」这种不带编号的二级标题）。
    """
    if body_size <= 0 or size < body_size * 1.12:
        return False
    stripped = text.strip()
    if not (2 <= len(stripped) <= 40):
        return False
    if "，" in stripped or "。" in stripped:
        return False
    # 以标点收尾的也不是标题——「那契数列的第𝑛个数字：」这种正文引导句，
    # 字号碰巧大一点就会被认成章节。
    if stripped.endswith(("。", "！", "？", "，", "、", "；", "：", ":", ")", "）")):
        return False
    if _PANEL_LABEL_RE.match(stripped):
        return False
    if _match_caption(stripped) is not None:
        return False
    # 公式行：含等号/比较符且基本没有中文（教材里没编号的标题不会长这样）
    if re.search(r"[=<>≤≥]", stripped) and not re.search(r"[\u4e00-\u9fff]{2,}", stripped):
        return False
    if re.match(r"^[‧·•●○▪◆\-—*=+]", stripped):
        return False
    if _PAGE_NO_RE.match(stripped):
        return False
    if _HEADING_STRONG_RE.match(stripped):
        return True
    # 弱线索：不带编号、字号大得多、短。**但「1. 常数阶」这种单级编号的列表项
    # 不算**——教材常把并列内容写成「1. 常数阶 / 2. 线性阶」，它们字号也大，
    # 认成章节会让目录里冒出五六个假节点。
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

    数学字母区（U+1D400-U+1D7FF，𝐀𝑥 这类）在坏映射下会被拆成高低两个孤立
    代理码点，str 里合法、一旦 encode 成 UTF-8 就炸（UnicodeEncodeError:
    surrogates not allowed），整本书解析失败。修法：能配上对的重新合成一个
    真字符，配不上的丢弃——数学装饰符号丢了不影响检索，整本炸了才要命。
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
            # 零宽空格是排版占位符（上下标的位置标记），不是内容。留着会让
            # 「S_t」变成带着不可见字符的「St」，检索时按词切分对不上。
            text = text.replace("\u200b", "").replace("\ufeff", "")
            if not text.strip():
                continue
            bbox = line.get("bbox", (0, 0, 0, 0))
            size = max((float(span.get("size", 0)) for span in line.get("spans", [])), default=0.0)
            result.append((bbox[0], bbox[1], bbox[2], bbox[3], text, size))
    return result


def _dominant_size(line: dict) -> float:
    """一行的主字号：**按字符数**取最多的那个 span 的字号。

    不能用 `max(span.size)`：正文里夹一个公式字符（数学字体通常比正文大一号），
    整行的字号就被抬上去了。段落合并要靠字号相同来判「这两行是一段」，
    一抬高就和相邻行对不上，一句话会被切成两段（「…时间复杂度为常数」＋「阶」）。
    """
    counter: Counter[float] = Counter()
    for span in line.get("spans", []):
        counter[round(float(span.get("size", 0)), 1)] += len(span.get("text", ""))
    return counter.most_common(1)[0][0] if counter else 0.0


# 上标的内容只能是这些字符。刻意**不含 `[` `]`**：参考文献角标 `[1]` 和数学
# 上标在版面上没法区分（字号都是 0.83 倍、都抬高 4pt、都紧贴前一个字），
# 唯一的差别就是方括号。宁可漏掉「方括号当上标」的极少数排版，也不能把
# 全书的引用角标改成 `^[1]`。
#
# 长度上限 4：指数里见过最长的是 `−308`（浮点数量级）和 `2i/d`（位置编码）。
# 放宽到 5 会吃进表格里错位的 `10000`——那是原 PDF 排版里的一段碎片，
# 不是谁的指数，转出来是假信息。
_SCRIPT_TEXT_RE = re.compile(r"[0-9A-Za-z+\-−=/.]{1,4}")

# 上标前面必须是能当底数的字符：字母（含希腊字母和数学斜体）、数字、右括号。
# 句末、逗号后面的小字号数字更可能是编号，不动。
#
# 数学斜体（`𝑛` U+1D45B、`𝑂` U+1D442）必须用 `\U` 八位写。写成 `\u1d7ff`
# 会被 Python 截成 `\u1d7f` 再加一个字面量 `f`——范围顶只剩 U+1D7F，
# 半数数学变量（包括 `𝑛`）都漏在外面，上标前面是变量时一律认不出。
_SCRIPT_BASE_RE = re.compile(r"[0-9A-Za-z\u0370-\u03ff\U0001D400-\U0001D7FF)]")


def _is_superscript(span: dict, previous: dict, baseline: float, base_size: float) -> bool:
    """这个 span 是不是前一个字符的上标。

    PDF 里上标没有任何语义标记，只有三条几何特征，必须同时成立：

    | 特征 | 实测值（Hello-Algo `𝑂(𝑛²)`） | 阈值 |
    |---|---|---|
    | 字号比前一个 span 小 | 8.32 / 11.88 = 0.70 | ≤ 0.85 |
    | 基线比本行主基线高 | 4.3pt / 主字号 9.96 = 0.43 | ≥ 0.25 |
    | 与前一个 span 水平紧贴 | 间隙 0.0 | ≤ 0.15 × 主字号 |

    比值 0.85 定得比较松，因为正文里数学字体本来就比中文字体大一号，
    上标要和**前一个 span**比（11.88），不能和行主字号（9.96）比——后者
    算出来是 0.835，几乎贴着阈值，稍一漂移就漏。反过来也不能更松：
    再松就会把「正文里偶尔出现的小字号脚注」也算进来。
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

    `O(n2)` 和 `O(n²)` 在原 PDF 里只差一个基线偏移，提取出来都是 `O(n2)`，
    读的人（和检索、和模型）没法知道那是平方还是变量名。还原成 `O(n^2)`
    —— 单字符不加括号，多字符用 `^(2i/d)` 括起来，避免 `10000^2i/d` 这种
    到底谁是上标的歧义。
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
    """过滤页眉页脚和纯页码，并给每行带上 block 序号与「是否等宽字体」。

    同一基线上的多段会先合并成一行。这一步不能省：行尾注释在 PDF 里是
    两个独立的 line 对象（`self._front = node` 和 `# 更新头节点` 的 y 相同、
    只有 x 不同），不合并就会按 x 算出几十个空格的缩进。
    """
    height = page.rect.height
    data = _sanitize_page_dict(page.get_text("dict"))
    lines: list[list] = []
    for block_index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = _line_text(line)
            # 零宽空格是排版占位符（上下标的位置标记），不是内容。留着会让
            # 「S_t」变成带着不可见字符的「St」，检索时按词切分对不上。
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

    # 同一「视觉行」的片段先归组，组内**按 x 重新排序**再拼接。
    #
    # 直接顺序追加是错的。一行里夹着数学变量时，PDF 会把它切成很多独立的
    # line 对象，而且它们的 y0 并不相同（变量本身大一号、下标更低）：
    #
    #     (79.6,555.5) '3. 环境接收到行动 '
    #     (162.1,556.3) 'A'          ← 大一号，y0 高 0.8
    #     (171.0,561.1) 't'          ← 下标，y0 低 5.6
    #     (174.6,555.5) ' 后，会转移到一个新的状态 '
    #     (297.0,556.3) 'S'
    #     (304.2,559.6) 't+1'
    #     (318.4,555.5) '。'
    #
    # 按 y 排序再追加，`A`/`S` 会跑到句尾、`t`/`t+1` 会掉成独立行，
    # 整句话就成了「…新的状态 A S 。」＋「t+1」＋「t」。
    # 分两步：先按 y 粗分成「行带」，**组内全部排好序再**按 x 切成视觉行。
    #
    # 顺序不能反。直接在扫描时同时判 x 会出错：右边那段先被处理，此时中间的
    # 下标（y0 更靠下、排在后面）还没进组，两段之间的间隙就显得太大，
    # 一行被劈成两半（「（新状态」＋「St+1 和奖励 Rt+1）」）。
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
    """行带内按 x 切成视觉行：间隙超过阈值就断开。

    默认 2 倍字号，两头都够用：一段文字里给上下标留的空位大约 1.4 倍
    （正文 9.8 字号时 14pt）得接上；而左右并排的东西间隙是几倍甚至几十倍
    ——幻灯片上「Play」按钮离图题注 278pt，靠这条分开，否则题注不再以
    「图 1-4」开头，配图直接失败。双栏的左右栏也是靠它分开的。

    `tight_cross_block` 只给**幻灯片**用，把跨文本框的阈值收到 0.35 倍：
    幻灯片上的图内标注（`发话器`、`A`、`f1`）和正文挤在同一条基线上，
    间隔只有 14pt（24 号字约 0.6 倍），按 2 倍会被吞进句子里，读出来是
    「即任一方讲话时，发话器可以听到对方的话音」。

    不能对正文书也收紧：Word 的行内公式是独立文本框，两侧各留一个空格，
    实测间隙 0.5~1 倍字号（「分数除以一个缩放因子 dk（dk是K向量的维度）」），
    收了就把句子劈成两半——Agents 一本书 900 多行都受影响。
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
            # 片段之间有明显空白就补一个空格，否则「行动」和「后」会粘成
            # 「行动后」——原文那里本来就有一个空格，只是被切到两个片段里了。
            if gap > 0.6 * max(entry[5], 1.0) and parts and not parts[-1].endswith(
                (" ", "\u3000")
            ):
                parts.append(" ")
        parts.append(entry[4])
        previous_end = entry[2] if previous_end is None else max(previous_end, entry[2])

    # 字号和等宽占比都按**字符数**加权：变量那一段字号大，但只有一两个字符，
    # 不该代表整行。段落的「字号是否相同」全靠这个值。
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

    图和公式要**插回它们在版面上的位置**，不能统一堆到页尾：回答里「如图 3-4」
    出现的位置应该就是书上那张图的位置，否则读起来是脱节的。

    返回 `(元素, 本页最后的章节名)`：章节是**跨页连续**的状态，翻到下一页、
    甚至翻到下一章的正文时都要沿用，所以不能按页各算各的。
    """
    elements: list[_Element] = []

    # 0) 表格：pymupdf 的 find_tables 靠线条和文字对齐找表格，能找到就给 markdown。
    #    **必须最先做**，因为表格里的文字在文本层里也在，不排除就会既进表格、
    #    又进正文，检索时同一句话命中两次。
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

    # 1) 图注配对：找到独占一行的题注，去它附近找图。
    #    **必须排在正文聚合前面**，因为配对成功的题注行要从正文里剔除——
    #    否则同一句「图 2-5 尾递归过程」会既是一条 figure chunk、又躺在正文 chunk 里，
    #    检索命中两次、引用指向两处，其中一处还是没图的纯文字。
    figure_rects = _pdf_graphic_rects(page)
    consumed: set[tuple[float, float, str]] = set()
    for line in lines:
        x0, y0, x1, y1, text, _size, _block, _mono = line
        caption = _match_caption(text)
        if caption is None:
            continue
        label, caption_text = caption
        if label.startswith("表"):
            # 表格内容本身就在正文文本里，截图只会截到一堆线和字。表注留着当文本用。
            continue
        rect = (
            _slide_figure_rect(figure_rects, (x0, y0, x1, y1), page)
            if parsed.section_per_page
            else _nearest_graphic_above(figure_rects, (x0, y0, x1, y1))
        )
        if rect is None:
            # 没配上图的题注**留在正文**：至少证明「书上有图 3-7」，
            # 比让它凭空消失好（读者会问「你刚才说图 3-7 呢」）。
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

    # 2) 代码块与正文段落。**代码要先切**：代码行之间行距紧、没有句末标点，
    #    先做段落合并会把 `import os` / `from tavily import ...` 粘成一行
    #    （实测拼出过 `import osfrom tavily import TavilyClient`）。
    #
    #    代码行的判据是「含等宽字体 **且** 左边缘超出正文列」：
    #    只用字体不行，正文里夹着的行内代码（`get_attraction`）也含等宽；
    #    而代码块整体比正文右移一个容器的内边距（13~20pt），行内代码贴在正文列上。
    #    幻灯片还有一类「正文」是**图内部的标注**：A / B / T / R / f1 / f2 /
    #    发射机 / 受话器 / 天线共用器。它们在图里是图的一部分，在文本层里却是
    #    普通文字，照单全收的话第 27 页会吐出 25 个互不相干的词。判据只有一条：
    #    **落在我们刚截下来的那张图里面**（图里已经有这些字，再当正文重复一遍
    #    只会把句子读断）。
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
        # `body_left` 和 `body_size` 都为 0 时宁可不认代码块：判据会退化成
        # 「几乎全部满足」，整本书的等宽行都会被包进围栏，比不识别更糟。
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
        # 行级判据会把**缩进排版的正文**也认成代码：思考题、列表续行常常整体
        # 右移，又夹着几个行内代码（`GRASP`、`Sublayer(x)`），两条都满足。
        # 到块一级才分得开——正文是「每行都有中文句读」，代码只有注释行才有。
        # 否决掉的必须**退回正文**，不能丢：这些行已经被 `is_code` 排除了，
        # 不显式收回来就会整句消失。
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

    # 剩下的正文行按**几何**合并成段，不按 block 序号：浏览器打印出来的 PDF
    # 有 93% 的行各自一个 block，按 block 聚合等于一行一段（Hello-Agents 整本
    # 21879 个「段落」、中位 27 字）。
    #
    # PPT 反过来——幻灯片一页就是几个文本框，必须按 block 聚合（见 `_ppt_paragraphs`）。
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

    # 2.5) PPT 的大图页：整页几乎都是矢量图、文字只剩几个散标签时，逐行输出
    #      那些标签等于交给读者一堆「管」「端」「云」「应用」，谁也看不懂原图
    #      长什么样。这时把这一页整体截下来，比输出零散文字有用得多。
    #      两条判据取并：图形占满半页，或者「字没几个、图形几十条」。
    #      已经配到题注的页跳过——那种页的图已经单独抽过了，再来一张整页就重复。
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

    # 2.6) PPT 的位图：幻灯片上的照片、截图、示意图是**嵌进去的位图**，不是
    #      矢量图形，2.5) 那套「看图形覆盖面积」的判据完全碰不到它们——
    #      结果是整本课件只剩零星几张带「图 1-1」题注的图，其余全丢。
    #      按面积门槛逐张抽，并跳过每一页都盖着的那层母版（`skip_images`）。
    #      已经出过整页图的页不再抽：整页里就包含这些小图。
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

    # 3) 公式编号：（3-12）单独一行时，把编号所在行连同上方一小块截下来
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

    # PPT 不能按 y 全局重排：正文刚按「先左栏后右栏」排好，而 y 在两栏之间
    # 是来回跳的，一重排又串回交错。图和式按「上方有几段正文」插进去。
    if parsed.section_per_page:
        elements = _ppt_page_order(elements)
    else:
        elements.sort(key=lambda item: item.y)

    # 图和表格的 chapter 是**在正文聚合之前**取的（那时还不知道本页有几个标题），
    # 所以页首的图会顶着「上一节」的名字，排序后就出现
    # 「2.3.1 → 2.3 → 2.3.1」这种抖动，目录里同一节出现两次。
    # 这里按版面位置回填：取**它上方最近的那个文字块**的章节。
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

# 中文句读。用来在**块一级**把「缩进排版的正文」和「代码」分开：
# 正文几乎每行都有，代码只有中文注释行才有。
_PROSE_PUNCT_RE = re.compile(r"[，。？！；：]")


def _is_code_line(line, body_left: float, body_size: float) -> bool:
    """单行是不是代码。三个条件**任一**成立即可，各自对着一种排版：

    - **左边缘缩进**：代码块整体在内边距里（Hello-Agents 84.8 vs 正文 72）；
    - **几乎全是等宽字体**：顶格的代码行（Hello-Algo 的 `def` 就顶在正文列上，
      光看缩进会漏掉它，连带着整块的缩进基准都算错）；
    - **字号明显小于正文**：代码常用小一号字（Hello-Algo 8.4 vs 正文 10）。

    三个都要，缺一个就有一类书解不出来。反过来，正文里夹的行内代码
    （`GRASP`、`Sublayer(x)`）三样都不满足。
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
    """一组候选行到底是不是代码块（行级判据只保证「像」，这里确认）。

    单行块没有上下文可比。优先级是**字号**：字号明显小于正文的行，即使
    整句都是中文也是代码里的注释（`# 暂存数据（常量，一般用大写字母表示）`
    就是这样，它含中文逗号，只按句读判断会被误判回正文）。
    字号和正文一样时才退回看句读——那时单行正文多是以 `？`／`。` 收尾的
    思考题、图注引导句。

    多行块看过半数的行：代码块里注释占少数，正文块里句读是常态。
    """
    if len(block) == 1:
        item = block[0]
        if body_size > 0 and item[5] < body_size - 1.2:
            return True
        return not _PROSE_PUNCT_RE.search(item[4])
    prose_rows = sum(1 for item in block if _PROSE_PUNCT_RE.search(item[4]))
    return prose_rows * 2 <= len(block)


# 代码块里夹带的文件名标记（Hello-Algo 每段代码第一行都有）。
# 拿它当围栏的语言标注，比按内容猜 `import`/`def` 可靠。
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
    """一组连续的代码行 → 带原始缩进的多行代码。

    两件事容易做错：

    1. **缩进要还原**。PDF 里没有「空格字符」，缩进只体现在左边缘上，
       所以要拿 x0 减掉本块的基准列，再除以字符宽度折回空格数。
       字符宽度按等宽字体的经验值 `0.6 × 字号`，字号取**这一行自己的**
       （代码字号常比正文小：Hello-Algo 代码 8.4、正文 10）。

    2. **折行要接回去，多行结构不能接**。PDF 页宽不够时一行代码会被折成两行，
       但字典字面量 `x = {` 的下一层本来就该换行。判据是「上一行括号/引号没闭合
       **且** 本行不更靠右」——只判括号会把多行字典误合并。
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


# 句末标点：上一行以此收尾，说明它把一句话说完了，下一行是新段。
_SENTENCE_END = "。！？；：.!?;:"

# 列表符号 / 编号。这些行各自成段，不能跟前一行合并——合并后
# 「1. 采样 2. 量化 3. 编码」会变成一句话。
_BULLET_RE = re.compile(r"^\s*([-*•·▪◦‣]|\d+[.)、]\s?|[（(]\d+[)）]|[a-z][.)]\s)")


# PPT 分栏的判据：两个文本框中心 x 相差超过这个值，就算「换了一栏」。
_PPT_COLUMN_GAP = 48.0

# PPT 的条目符号。一级是 Wingdings 字形，落在私用区（U+F070，普通字体里
# 根本画不出来），二级是 ➢。它们是「这一行新起一条」的唯一信号——一旦
# 丢了，四条并列的要点就会被拼成一句没有标点的长句。
#
# ❖（U+2756）是同一批课件里的第三种条目符（「❖低空智联网 / ❖海洋通感一体化」）。
# 它不在原来的字符集里，于是那几页的要点全被拼进了一段——条目符的名单只能靠
# 真实课件补齐，不能照着一份「常见项目符号表」写。
_PPT_BULLET_RE = re.compile(r"^[\s\u200b]*[\uf000-\uf8ff➢➤►▶▸◆◇▪▫●○•·❖]")

# PPT 里「这一页其实是一张图」的判据：最大一块图形占到页面的这个比例、
# 且整页文字少于此数，就整页截图。实测这批课件：p13 84%、p19 81%、
# p29 100%、p30 62% 全是大图，而文字都在 260 字以内；正文页都在 10%。
_PPT_FIGURE_COVER = 0.5
_PPT_FIGURE_MAX_CHARS = 320

# 第二类大图页：**文字只剩几个标签、图形却有几十条**。这批课件里 p30 是
# 「云/管/端/接入网/核心网」的演进图，49 条矢量线互不相连，合并成一个簇
# 只占页面 13%，覆盖率判据碰不到它——结果输出的是「端 / 管 / 云 / 应用」
# 七个互不相干的词。25 个字配 49 条线，这只有「整页是一张图」能解释。
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
    """`box` 是不是接着 `previous` 那段话往下写的**另一个文本框**。

    这是 PPT 独有的一种断句：一句话写到框的右边界，溢出的部分在 PDF 里
    变成**另一个 block**。按 block 分组就会断在半个词上：

        无线信号传输及移动通        ← block 8，写到右边界
        信系统组网；                ← block 9，「通信」被劈成「通」「信」

    四条同时成立才算续写：
    1. 上一块最后一行没有句末标点——话没说完；
    2. 上一块最后一行**是这一块里最宽的一行**——它顶到了右边界才被切断
       （段落的最后一行通常很短，不会是最宽的）；
    3. 这一块首行比上一块首行更靠右——续行缩进，不是新起一段；
    4. 这一块首行不以条目符号开头，且横向落在上一块的范围内——不是
       隔壁栏的另一个框。
    """
    tail = previous[-1]
    head = box[0]
    if tail[4].rstrip()[-1:] in _SENTENCE_END:
        return False
    # 同一个框里同一 y 上有两条以上的行 = 一格格里并排排的表行（表头、表体、
    # 左右对照），不是「一句话没写完」。接了就成了「频率范围名称对应的
    # 频率范围」这种看不懂的字符串。
    if _has_side_by_side(box):
        return False
    if tail[2] < max(item[2] for item in previous) - 1.0:
        return False
    if _PPT_BULLET_RE.match(head[4]):
        return False
    size = max(head[5], tail[5], 1.0)
    # 缩进量要按**上一行**算，不能按整段首行算：否则「表格右边一栏在很靠右
    # 的位置」也算「缩进了一个字」，整张表会被接到标题后面。
    indent = head[0] - tail[0]
    if not 0.2 * size <= indent <= 2.5 * size:
        return False
    low = min(item[0] for item in previous) - size
    high = max(item[2] for item in previous) + size
    if not low <= head[0] <= high:
        return False
    # 中间隔了空行就不是续写，是另起一块。
    return -0.5 * size <= head[1] - tail[3] <= 1.5 * size


def _ppt_columns(ranges: list[tuple[float, float]]) -> list[int]:
    """给每个文本框判栏号：先按中心 x 的间隔粗分，再把「嵌在栏里的窄框」并回去。

    第一遍只看中心 x：同一栏内文本框的左右抖动只有几个点（内边距），
    两栏之间在 100pt 以上，48pt 足够分开。

    第二遍是因为实测有一类版面过不去——右栏很宽（318~725），中间嵌着一个
    只占左半边的窄框（「性能评价」282~407）。它的中心 x 和右栏差 128，
    第一遍会把它判成独立的一栏，读起来就成了「左栏 → 性能评价 → 右栏」，
    可它其实是右栏的第一条。判据换成横向**重叠**：窄框有 70% 落在右栏
    范围里，就归右栏；而左栏（16~312）和右栏只重叠 30/125，不会被误并。
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
    """PPT 的段落重建：**按文本框分组**，再按「先左后右、再上到下」排。

    幻灯片和正文书不是一回事。正文书的每一行是连续正文的一段，按几何行合并
    是对的；而幻灯片的一页常常是几个并列的文本框（左栏一列、右栏一列），
    按几何行合并会把两栏在同一高度上的行串成一句，读出来是交错的碎片：

        ➢无线电频谱可以正交分割(时     ← 右栏 y=88
        ➢无线频谱资源有限稀缺          ← 左栏 y=102
        间\频率\空间)                 ← 右栏 y=116，被劈成两半

    所以分三步：

    1. **合并续写框**：一句话写到框右边界，溢出部分在 PDF 里是另一个
       block，按 block 分组会断在半个词上（见 `_ppt_continues`）；
    2. **框内按条目符号切段**：一个框里常常竖着排了四条 ➢ 要点，全部拼成
       一行就是「➢要求稳定可靠➢要求携带方便➢要求低功耗」；
    3. **按栏优先排序**：同一栏里从上到下，换栏才往右走（见 `_ppt_columns`）。

    这一套**只给 PPT 用**。正文书不能按 block 聚合——浏览器打印出来的 PDF
    有 93% 的行各自一个 block，按 block 聚合等于一行一段。
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
    """PPT 一页的元素顺序：正文保持阅读顺序，图和式按位置插进去。

    书页按 y 排序是对的——一页只有一栏，y 天然单调。幻灯片不是：正文刚由
    `_ppt_paragraphs` 排成「左栏说完再说右栏」，右栏的 y 比左栏末段还小，
    再按 y 排一遍就把两栏重新串成一条。

    所以正文一块都不动，只决定图和式插在哪两段之间——按「它上方有几段
    正文」定位，这跟页首的大图（y 取整页上边界）能自然排到最前面。
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
    """把版面上一行行的文字合并成段落，**不看 block 序号**。

    为什么不能按 block 聚合：不同工具链切 block 的方式完全相反。
    浏览器打印（Skia）出来的 PDF 有 93% 的行各自一个 block，按 block 聚合
    等于一行一段；LaTeX 出来的书一个 block 里能装 7 行。同一份代码在两类书上
    表现相反，说明 block 边界根本不是「段落边界」，只是生成器的内部选择。

    能跨工具链通用的只有几何：**行距 + 字号 + 标点 + 左边缘**。
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
        # 行内间距通常不超过 0.75 倍字号；段间距在 1 倍以上。
        tight = -0.3 * size <= gap <= 0.75 * size
        open_tail = previous[4].rstrip()[-1:] not in _SENTENCE_END
        # 下一行比本行更靠右一个缩进宽度 → 它是新段的首行（中文首行缩进 2 字）。
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
    """把同一段里的多行拼成一行。

    三种拼法：西文断词（行尾 `-`）去掉连字符直接接；西文句子之间补空格；
    中文直接接。**中文不补空格**是硬要求——补了会得到「抽样 定理」这种
    每个字之间带空格的文本，检索时按词匹配会全部失配。
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
    """判断一行是不是「图 3-4 xxx」这种独占题注。返回 `(label, caption)`。

    三道过滤，缺一不可：
    1. 整行太长 → 是正文里的一句话，不是题注；
    2. 题注文字以句末标点收尾 → 是正文句子（题注很少带句号）；
    3. 题注文字以「展示了 / 反映了」这类动词开头 → 正文在引用这张图。

    第 3 条是实测出来的：Hello-Algo 里「图 2-7 展示了以上三个算法函数的时间复杂度。」
    被当成题注后，程序真的去上一块区域截图，把正上方那张**属于图 2-6 的图**
    又截了一遍。现象是「图比书里多一倍，编号还重号」。
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
    # 题注是「名字」，一个字的题注不存在。实测 Hello-Agents 里正文有断行后留下
    # 「图2.10）？」这种残句，`_CAPTION_RE` 会把 `？` 当题注文字收下——它会让
    # 程序去截一张不存在的图，也污染图注召回的分母。
    if len(caption) <= 1:
        return None
    if caption.startswith(_CAPTION_VERBS):
        return None
    # 题注是「名字」，不是「句子」：超过 45 字基本可以断定是正文行被断行后
    # 恰好以「图 2-11」开头的那种（实测：「图 2-11 和以下代码模拟了细胞分裂的过程…」）。
    #
    # 这里刻意**不按「以虚词开头」判断**：试过加一条「以 和/对/在 开头即否决」，
    # 「图 2-12 对数阶的时间复杂度」当场被误杀（首字是「对」），图就少了一张。
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
    """找出这一页的表格，返回 `(markdown 表格, bbox)`。

    用 pymupdf 的 `find_tables()`：它靠**线条和文字对齐**找网格，对带边框的
    表格很准，对无边框表格会漏（漏了不致命——那些文字仍然会作为正文进去，
    只是失去了行列对齐）。

    三种结果直接丢掉：
    - 少于 2 列或 2 行：那不是表格，是一段被误判的文字；
    - 面积超过整页 75%：通常是双栏正文被整体框成了一个「表格」，
      真的表格不会占满整页；
    - 清洗后不足 2 行。
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
    """把 `extract()` 的原始网格清洗成能写进 markdown 的表格。

    原始结果有三个毛病，都要治：

    1. **单元格内的换行被拆成了新行**。教材表格的行高常常是两三行文字，
       按 y 坐标切就变成「实现方」和「式」两行、「时间效」和「率」两行。
       判据：一整行里超过一半格子是空的 → 它是上一行的续行，拼回去。
    2. **合并单元格没有值**。表 3-1 第一列「整数」跨三行，后面两行第一列是空的。
       判据：第一列空而上一行第一列有值 → 继承上一行的。
    3. **自动列名**。没识别出来的表头会被叫成 `Col1`、`Col3`，这些是噪音。

    清洗是**有损**的：`−2⁷` 这种上标在文本层里是独立字形，拼出来是 `−27`。
    这没法从文本层恢复（要视觉模型），所以表格里的数学式子可能会串位——
    这一点在引用时用「原书第 58 页」兜底，让人能自己回原文核对。
    """
    grid: list[list[str]] = []
    for row in rows:
        # find_tables 的 extract() 有自己独立的文本提取路径，不过 get_text("dict")
        # 的清洗——孤立代理对（𝐀 这类数学字母被坏 CMap 拆成半截）要从这里再挡一道，
        # 否则 encode 成 UTF-8 时整本解析失败（2026-10-21 基因组学课件踩过）。
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
    """这个矩形是**页面排版框**（边距框/背景框），不是插图。

    判据两条同时成立：
    - 大到能装下正文（宽高都 ≥ 页面 60%）；
    - 它真的把**半数以上**的文字块框在里面（90% 面积都在它里面才算框住）。

    为什么必须排掉：Hello-Agents 每页都有一个 `[72,30,540,760]` 的矢量矩形
    （页面 612x792），是排版框。它一旦进了图形列表，`_merge_clusters` 会把它
    和真正的插图合并成**一个整页大簇**，而 `_nearest_graphic_above` 要求
    「图注顶 − 簇底 ∈ [0, 340]」——整页簇的底边在题注下方（760 > 540），
    gap 是负的，于是被排除，结果是**整本书一张图都配不上**。

    这条过滤放在收集阶段而不是合并之后：合并是「把邻近的碎片粘成一张图」，
    一旦被它粘进去就分不出来了。
    """
    if rect.width < page_rect.width * 0.6 or rect.height < page_rect.height * 0.6:
        return False
    inside = sum(
        1 for block in text_rects if (block & rect).get_area() > block.get_area() * 0.9
    )
    return inside >= max(3, len(text_rects) // 2)


# 幻灯片里内嵌视频的「播放」按钮，文本层里就是一个 `Play` 单词，夹在句子
# 中间读起来莫名其妙（第 26、27 页各一处）。
_SLIDE_ICON_TEXTS = {"play"}


def _is_slide_label(line, boxes: list) -> bool:
    """这一行是不是幻灯片上的**图内标注**（`A`、`T`、`f1`、`发射机`、`天线共用器`）。

    四条同时成立：落在某张图的截图范围里、短（≤ 20 字）、不是一句话
    （没有句末标点）、不是条目（`➢` 开头的是真要点，压在图上也照留）。

    只认「我们真的截下来的那张图」，不认「图形簇」——图形簇在排版宽松的
    幻灯片上会长得很大，把正文一起圈进去，第 12、17、24 页的正文就是这么被
    误伤的（「无线通信的速率」「频率相近的组合频率」都落在簇里）。
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
    """一页里所有「可能是插图」的版面矩形。

    两路都收：
    - **位图**（`get_images`）：绝大多数导出型教材的图都是内嵌图片，位置精确；
    - **矢量图**（`get_drawings`）：扫描/重排过的教材里图是线条画出来的，
      `get_images` 对它们**返回空**——这是踩过的坑，只做位图会一张图都抽不到。

    两者都过滤掉装饰性元素：太小的（图标、公式里的分数线）、贯穿整页的
    细横线（页眉下的分隔线）。
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
    """候选里已经被截进 `box` 的簇全部去掉。

    数据集的图注配图截的是**一个簇**，直接按相等剔除就够；幻灯片截的是
    若干簇的并集，得按「有没有一半落在里面」判断，否则同一页的第二个题注
    会把第一张图再截一遍。
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
    """幻灯片上「题注上方」的截图范围：从题注往上**逐段生长**再求并集。

    教材里图注独占一行、图是一整块，`_nearest_graphic_above` 找最近的簇就够。
    幻灯片不行：图注是混在文本框里的，图上方的连线、箭头、虚框各自成簇，
    最近的那一簇常常只有 30×30pt——截出来是一个谁也看不懂的小色块
    （实测「图 1-3」「图 1-4」都是 57×71 像素的一小块）。

    所以从题注往上长：先收底部贴近题注的簇，再收底部贴近刚才那批的簇，
    最后取并集。间隔 60pt 是「图里相邻部件」和「图与上一块文字」的分界。

    窗口下界是 **-30 而不是 0**：幻灯片的图往往把题注**包在自己里面**
    （图的外框一直画到题注下方），按「必须在题注上方」过滤会把整张图漏掉。
    """
    picked = [
        rect
        for rect in rects
        if -30 <= caption_bbox[1] - rect.y1 <= 60 and _column_near(rect, caption_bbox)
    ]
    cursor = min((rect.y0 for rect in picked), default=caption_bbox[1])
    while picked:
        # 横向基准是**已选簇的并集**而不是题注：幻灯片上的图常常比题注宽
        # （右侧突出的框、图例、跨栏的注释），一直拿题注做基准，宽图会被
        # 左右裁边（实测图 2.13 的「测量」框被切掉一半、题注整条丢失）。
        # 第一轮 picked 才用题注约束（防吸进隔壁栏），之后跟着图的并集走。
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
    # 并集只有一小块，说明收到的是几个装饰碎屑而不是图，宁可不截。
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
        # 横向要有重叠，否则可能是隔壁栏的图
        if rect.x1 < caption_left - 30 or rect.x0 > caption_right + 30:
            continue
        if rect.height < 20:
            continue
        best, best_gap = rect, gap
    return best


def _formula_rect(page, line_bbox: tuple[float, float, float, float]):
    """公式的截图范围：编号所在行往上扩一块。

    不做公式识别（书上是什么样就显示什么样）。经验做法是往上扩 46pt——
    公式主体通常在编号的上方或与编号同行，但高度差得很远，所以宁可多截一点
    带上下文，也别截掉上半截式子。
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
    """图对应的 chunk 内容 = 编号 + 题注。

    只有编号没有题注的图（很多教材图注就是「图 3-4」四个字）仍然保留——
    「第 3 章有图 3-4」这个事实本身能让检索说清「书上有这张图」，
    比假装它不存在好。
    """
    return f"{label} {caption}".strip()


def _chapter_at(toc: list, page_no: int) -> str | None:
    """按书签目录算出某一页属于哪一章。取**最深一级**的标题。

    PDF 目录的页码是 1-based，和 `page_no` 同口径，所以直接比，不需要 ±1。
    没有目录（`toc` 为空）时返回 None——宁可不标章节，也不用字号猜一个错的，
    因为 C2 的验收标准里「页码和章节要和书一致」。
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


# --------------------------------------------------------------------------
# PPTX 解析
# --------------------------------------------------------------------------

def _parse_pptx(data: bytes, asset_dir: Path) -> _Parsed:
    """一套课件 → 元素流。**一张幻灯片一条 chunk**（或按长度切成几条）。

    PPT 是「一页一个主题」，跨页合并会得到一堆互不相干的短句拼在一起，
    这和 PDF 的「段落跨页要合并」正好相反。所以这里不做跨页合并。

    图片按 `shape.image.blob` 取出原始字节存盘（PPT 里的图是独立对象，
    不需要像 PDF 那样渲染区域）。公式是 OMML，抽不出来，v1 只做页级引用。
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
                # 标题占位符单独记下来当章节名（PPT 的「章」就是它的标题）
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


# --------------------------------------------------------------------------
# DOCX 解析
# --------------------------------------------------------------------------

def _parse_docx(data: bytes, asset_dir: Path) -> _Parsed:
    """一份 Word 教材 → 元素流。

    页数是「不知道」——Word 的页数取决于渲染时的字体和纸张，`python-docx`
    根本不提供。所以 `page_count` 记 0，前端显示成「—」，而不是编一个数字。
    引用时页码也就只能给到章节，这一点在引用格式里体现（有章节没页码）。

    表格按行转成 `a | b | c` 的纯文本进正文：表格内容不转成文本就会整块丢失，
    而 v1 不做「表格 → 结构化数据」。
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
            # `payload` 是 `Paragraph` 对象，不是字符串——文本在 `.text` 上。
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
    """按**文档顺序**遍历段落和表格。

    `python-docx` 的 `document.paragraphs` 和 `document.tables` 是两个独立列表，
    分开遍历会把顺序打乱（表格全部跑到正文后面）。所以走底层 XML，
    按 body 里子元素的出现顺序还原。
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
            # 节属性里可能挂着图片（页眉/水印），不作为正文插图
            continue

    # 内嵌图片按关系表顺序取。**拿不到它在正文里的位置**——图片在 XML 里是
    # 一个 run 里的 drawing 节点，要精确配对得解析 r:embed 和题注的相对位置，
    # 收益不大（Word 教材用图少，且图注都在图下方），所以只保留「有这张图」。
    for rel in document.part.rels.values():
        if "image" not in rel.reltype:
            continue
        try:
            blob = rel.target_part.blob
            extension = rel.target_part.partname.ext.lstrip(".") or "png"
        except Exception:  # noqa: BLE001
            continue
        yield "image", (extension.lower(), blob, 0, 0)
