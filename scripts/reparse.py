"""重新解析一本书（PDF 一律走 MinerU）。

**会消耗云端额度**：按书的页数算，跑之前会先把页数列出来让你确认。

用法（仓库根目录）：
    python scripts/reparse.py --list            # 看现有文档
    python scripts/reparse.py <doc_id>          # 重解析（先确认页数）
    python scripts/reparse.py <doc_id> --yes    # 跳过确认（批量脚本用）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

import documents  # noqa: E402
import retrieval  # noqa: E402


def show_list() -> None:
    conn = sqlite3.connect(documents.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT doc_id, title, kind, page_count, status, chunk_count"
            " FROM documents ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("（还没有文档）")
        return
    for row in rows:
        print(
            f"{row['doc_id']}  {str(row['title'])[:28]:<30} {row['kind']:<5} "
            f"{row['page_count'] or 0:>4}页  {row['status']:<8} {row['chunk_count'] or 0}段"
        )


def _fetch(doc_id: str):
    """运维脚本直接查库：`get_document` 要 user_id，这里不该关心归属。"""
    conn = sqlite3.connect(documents.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT doc_id, user_id, title, page_count, status FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    finally:
        conn.close()


def _reset_ready(doc_id: str) -> None:
    """`parse_document` 对 ready 的书直接返回（防重复烧额度），重解析必须先把它打回 pending。"""
    conn = sqlite3.connect(documents.DB_PATH)
    try:
        conn.execute(
            "UPDATE documents SET status = ?, error = NULL WHERE doc_id = ?",
            (documents.STATUS_PENDING, doc_id),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("doc_id", nargs="?", help="要重解析的文档 id")
    ap.add_argument("--list", action="store_true", help="列出所有文档")
    ap.add_argument("--yes", action="store_true", help="跳过额度确认")
    args = ap.parse_args()

    if args.list or not args.doc_id:
        show_list()
        return

    record = _fetch(args.doc_id)
    if not record:
        raise SystemExit(f"找不到文档 {args.doc_id}")
    title = record["title"]
    pages = record["page_count"] or 0
    print(f"将把《{title}》({pages} 页) 交给 MinerU 云端，会消耗额度。")
    if not args.yes and input("继续？[y/N] ").strip().lower() not in {"y", "yes"}:
        raise SystemExit("已取消")

    if record["status"] == documents.STATUS_READY:
        _reset_ready(args.doc_id)
        print("状态已打回 pending（否则 parse_document 会直接返回、什么都不做）。")

    # 新 chunk_id 和旧的落在同一段区间，不清旧向量的话它们会被贴到新正文上——
    # 检索出来的「语义匹配」是上一版的，肉眼完全看不出来。
    retrieval.forget_document(args.doc_id)

    result = documents.parse_document(args.doc_id)
    print(
        f"状态 {result.get('status')}  段落 {result.get('chunk_count')}  "
        f"页 {result.get('page_count')}  图/式 {result.get('asset_count')}"
    )
    if result.get("error"):
        print(f"错误：{result['error']}")
        return

    vectors = retrieval.ensure_vectors(str(record["user_id"]))
    print(f"向量补齐：{vectors.get('embedded')}/{vectors.get('total')} {vectors.get('reason') or ''}")


if __name__ == "__main__":
    main()
