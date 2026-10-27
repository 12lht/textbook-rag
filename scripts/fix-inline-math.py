"""存量修复：把已入库 Markdown 里裸的 LaTeX 包成 `$..$`。

起因：老解析没认 MinerU 的 `equation_inline` 类型，把公式源码直接拼进正文（整库 5679 处），
前端不渲染、检索里全是反斜杠噪声。代码已修，这个脚本处理**已经入库**的那批。

**不重解析、不烧 MinerU 额度**：改库里的 markdown → 重切 → 重算这一本的向量。

用法（仓库根目录）：
    python scripts/fix-inline-math.py --dry-run     # 只统计
    python scripts/fix-inline-math.py               # 全部修
    python scripts/fix-inline-math.py --doc <id>    # 只修一本
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

import documents  # noqa: E402


def _bare(markdown: str) -> int:
    text = documents._CODE_FENCE_RE.sub(" ", markdown)
    text = documents._BLOCK_MATH_RE.sub(" ", text)
    text = documents._INLINE_MATH_RE.sub(" ", text)
    return len(documents._LATEX_CMD_RE.findall(text))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", help="只修这一本")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    conn = sqlite3.connect(documents.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT doc_id, title, user_id, markdown FROM documents WHERE status = ?"
            " ORDER BY page_count DESC",
            (documents.STATUS_READY,),
        ).fetchall()
    finally:
        conn.close()

    if args.doc:
        rows = [r for r in rows if str(r["doc_id"]) == args.doc]

    touched, total = [], 0
    for row in rows:
        markdown = row["markdown"] or ""
        before = _bare(markdown)
        if not before:
            continue
        preview, count = documents.wrap_inline_math(markdown)
        total += count
        print(f"[{'看' if args.dry_run else '改'}] {str(row['title'])[:34]:<36} "
              f"裸 LaTeX {before:>4} → {_bare(preview):>3}　包裹 {count:>3}", flush=True)
        if args.dry_run or not count:
            continue
        documents.fix_inline_math(str(row["doc_id"]))
        touched.append((str(row["doc_id"]), str(row["user_id"])))

    if args.dry_run or not touched:
        print(f"\n合计可包裹 {total} 处（dry-run，未写库）")
        return

    # 正文变了向量必须重算——这一步已经收进 `documents.rechunk`（按内容哈希只补变了的段落），
    # 脚本不再手动 `forget_document`：那会把整本的向量全删再全算，白花钱还慢。
    print(f"\n改了 {len(touched)} 本，合计包裹 {total} 处（向量已按内容哈希增量更新）")


if __name__ == "__main__":
    main()
