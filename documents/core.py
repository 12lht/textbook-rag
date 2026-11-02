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
import time
from datetime import datetime, timedelta
from pathlib import Path

# 解析器已搬到 documents_parse.py。这里再导出一次，是因为外部（api / service / 探针）
# 一直是从 documents 这个名字拿这些符号的，签名不能因为内部搬家就改。
from .parse import (  # noqa: F401
    _CAPTION_RE, _CAPTION_VERBS, _DECOR_AREA_RATIO, _DECOR_ASPECT,
    _DECOR_SHORT_SIDE, _HEADING_RE, _HtmlTable, _ILLUS_AREA_RATIO,
    _ILLUS_SHORT_SIDE, _INLINE_MATH_TYPES, _MINERU_NOISE, _SENTENCE_END,
    _UFFFD, _VLM_ASK_BUDGET, _VLM_DESC_BUDGET, _VLM_FAIL_LIMIT,
    _VLM_INTERVAL, _astral_repair, _bbox_area, _caption_label,
    _clean_heading, _elements_from_mineru, _figure_verdict, _html_table_to_markdown,
    _image_size, _inline_text, _iter_docx_blocks, _looks_decorative,
    _match_caption, _mineru_image_path, _parse_docx, _parse_pdf,
    _parse_pdf_mineru, _parse_pptx, _require, _save_mineru_image,
    _vision_budget,
)
from .types import DocumentError, _Element, _Parsed

from . import db as _db
from .db import (  # noqa: F401
    _SCHEMA,
    _init_lock,
    _now,
    _schema_ready,
    _connect,
    _migrate,
    _bump_corpus,
    corpus_version,
    _row_to_doc,
)
from .text import (  # noqa: F401
    _PLACEHOLDER_ALT_RE,
    _CODE_FENCE_RE,
    _BLOCK_MATH_RE,
    _INLINE_MATH_RE,
    _IMAGE_REF_RE,
    _LATEX_CMD_RE,
    _CJK_PUNCT,
    _is_stop,
    wrap_inline_math,
    _PAGE_ANCHOR_RE,
    _CHAPTER_ANCHOR_RE,
    _IMAGE_LINE_RE,
    ASSET_REF_RE,
    _BULLET_GLYPHS,
    _BULLET_LINE_RE,
    normalize_bullets,
    _SYMBOL_GREEK_UP,
    _SYMBOL_GREEK_LO,
    _SYMBOL_MAP,
    _SYMBOL_BOX_CODES,
    _build_symbol_table,
    _SYMBOL_TABLE,
    fix_symbol_font,
)
_log = logging.getLogger("agent.documents")


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
        doc_dir = _db.UPLOAD_DIR / doc_id
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
        asset_dir = _db.ASSET_DIR / doc_id
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
    if Path(_db.DB_PATH).parent.resolve() != retrieval.vector_db_path().parent.resolve():
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

    for directory in (_db.UPLOAD_DIR / doc_id, _db.ASSET_DIR / doc_id):
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
    先认存的；不认就退回 `_db.ASSET_DIR/<doc_id>/<文件名>`——那是图唯一合法的落点。
    不在库里改写路径：换一次机器就要改一次，改在读取侧只需一次。"""
    path = Path(stored or "")
    if path.is_file():
        return str(path)
    fallback = _db.ASSET_DIR / doc_id / path.name
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
            # `element.label` 可能是 None；`numbered` 为真已经蕴含 label 非空，
            # 但类型检查推不出来，显式判一次（顺带挡住未来改动）。
            if numbered and element.label and element.label not in element.content:
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


