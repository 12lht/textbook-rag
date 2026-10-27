"""把已入库书里的「第 N 页 插图」占位图注补成模型写的一句话。

**不重解析、不烧 MinerU 额度**：改的是数据库里那一列 Markdown，然后重切（`rechunk`）。
一批书跑一次，成本是几十次视觉模型调用（几毛钱）。

用法（仓库根目录）：
    python scripts/fill-concepts.py                # 全部 ready 的书
    python scripts/fill-concepts.py --doc <id>     # 只补某一本
    python scripts/fill-concepts.py --limit 50     # 单本最多补多少条
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
import figures  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", help="只补这一本")
    ap.add_argument("--limit", type=int, default=200, help="单本最多补多少条")
    args = ap.parse_args()

    if not figures.available():
        raise SystemExit("没配 SILICON_API_KEY，视觉模型不可用")

    conn = sqlite3.connect(documents.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT doc_id, title, user_id FROM documents WHERE status = ? ORDER BY title",
            (documents.STATUS_READY,),
        ).fetchall()
    finally:
        conn.close()

    targets = [r for r in rows if not args.doc or str(r["doc_id"]) == args.doc]
    total_filled = total_seen = 0
    for row in targets:
        try:
            result = documents.describe_placeholders(str(row["doc_id"]), limit=args.limit)
        except Exception as exc:  # noqa: BLE001 - 单本失败继续下一本
            print(f"[FAIL] {str(row['title'])[:34]:<36} {type(exc).__name__}: {exc}", flush=True)
            continue
        seen = int(result.get("total") or 0)
        filled = int(result.get("filled") or 0)
        total_seen += seen
        total_filled += filled
        if seen:
            note = result.get("reason") or f"{filled}/{seen}"
            print(f"[{'OK' if filled else '..'}] {str(row['title'])[:34]:<36} 占位 {seen} 条 → {note}",
                  flush=True)

    print(f"\n合计：占位 {total_seen} 条，补上 {total_filled} 条")


if __name__ == "__main__":
    main()
