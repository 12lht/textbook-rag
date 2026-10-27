"""探针共用的样本 PDF 定位。

`textbook/` 在 2026-10-25 被移出仓库，源文件现在只存在 `data/uploads/<doc_id>/source.pdf`
（上传时落的那一份）。探针不能再硬编码 `textbook/Hello-Algo.pdf`——那样换台机器就是
`FileNotFoundError`，或者静默跳过（`exit(0)`）看起来像通过。

查找顺序：
1. 环境变量 `SAMPLE_PDF`（想指定任意一份时用）；
2. `textbook/` 下还能找到的话就用它（老机器/临时放回来时）；
3. 库里按书名模糊匹配，取 `data/uploads/<doc_id>/source.pdf`。

用法：
    from _sample import sample_pdf
    PDF = sample_pdf("Hello-Algo")          # 找不到就报错退出，不是静默跳过
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "documents.sqlite3"


def _from_db(hint: str) -> Path | None:
    if not DB_PATH.is_file():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT doc_id, title, file_path, status FROM documents"
            " WHERE title LIKE ? ORDER BY page_count DESC",
            (f"%{hint}%",),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        stored = Path(str(row["file_path"] or ""))
        if stored.is_file():
            return stored
        fallback = REPO_ROOT / "data" / "uploads" / str(row["doc_id"]) / stored.name
        if fallback.is_file():
            return fallback
    return None


def find_sample(hint: str = "Hello-Algo") -> Path | None:
    configured = os.getenv("SAMPLE_PDF", "").strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    for candidate in (
        REPO_ROOT / "textbook" / f"{hint}.pdf",
        REPO_ROOT / "textbook" / "pdf_test" / f"{hint}.pdf",
    ):
        if candidate.is_file():
            return candidate
    return _from_db(hint)


def sample_pdf(hint: str = "Hello-Algo") -> Path:
    """拿一份样本 PDF。**找不到就报错**，不要 `exit(0)` 假装通过。"""
    found = find_sample(hint)
    if found is None:
        raise SystemExit(
            f"找不到样本 PDF「{hint}」。textbook/ 已移出仓库，"
            f"请先在前端上传这本书，或用 SAMPLE_PDF=/path/to.pdf 指定。"
        )
    return found
