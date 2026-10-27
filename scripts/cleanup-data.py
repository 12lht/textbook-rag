"""存量数据清理：把解析/切分层修好之后，**已经进库的脏数据**按新规则对齐一遍。

代码修好不等于数据修好。这个脚本处理四类，全部不碰 MinerU 额度（除了最后一档「重解析」不在这里）：

1. **重复图片**：同一本书里字节相同的图存了多份（实测 Hello-Algo 148 张 / 49.5MB）。
   按 sha1 分组，留第一份，把 markdown 引用和 `chunks.asset_id` 都改指过去，
   重复文件移到 `data/.dupes/<时间戳>/`（**不直接删**，确认没问题再手动清）。
2. **占位 label**：`assets.label` 是字面量「插图」的，改成 NULL（那个字段只放图号）。
3. **分类归属**：`category_id` 全为空的，按书名关键词落到已有分类。
4. **重切**：全部 ready 的书 `rechunk` 一遍。这一步会顺手修掉
   「只有页码锚点的空段」「<5 字的碎片」「三万字的代码块」——这些是切分层的问题，
   markdown 没变、只是之前切错了。**重切会自动同步向量**（`documents._sync_vectors`）。

用法（仓库根目录）：
    python scripts/cleanup-data.py --dry-run     # 只看会改什么
    python scripts/cleanup-data.py               # 真改（先自己跑一次 backup.py）
    python scripts/cleanup-data.py --no-rechunk  # 只清图/标签/分类
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")  # 不加载的话 ensure_vectors 会以为「没配向量服务」直接跳过
except ImportError:  # pragma: no cover
    pass

import documents  # noqa: E402

CATEGORY_RULES = [
    ("移动通信", ("移动通信", "抗衰落", "蜂窝", "电波传播", "编码和调制", "第五代移动通信")),
    ("生物信息", ("生物信息", "序列分析", "基因组", "基因表达", "蛋白质结构", "系统发育",
                  "隐马尔科夫", "生物学基础")),
]


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    one = lambda sql, *args: conn.execute(sql, args).fetchone()[0]
    return {
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "assets": one("SELECT COUNT(*) FROM assets"),
        "空锚点段": one(
            "SELECT COUNT(*) FROM chunks WHERE content GLOB '<!-- page:*' AND length(content) < 40"
        ),
        "不足5字": one("SELECT COUNT(*) FROM chunks WHERE char_count < 5"),
        "超3000字": one("SELECT COUNT(*) FROM chunks WHERE char_count > 3000"),
        "孤儿图": one(
            "SELECT COUNT(*) FROM assets a WHERE NOT EXISTS"
            " (SELECT 1 FROM chunks c WHERE c.asset_id = a.asset_id)"
        ),
        "label为插图": one("SELECT COUNT(*) FROM assets WHERE label = '插图'"),
        "未分类书": one("SELECT COUNT(*) FROM documents WHERE category_id IS NULL"),
    }


def dedupe_assets(conn: sqlite3.Connection, *, dry_run: bool) -> dict[str, int]:
    """按图片内容去重。返回 {组数, 重复张数, 省下字节}。"""
    rows = conn.execute(
        "SELECT asset_id, doc_id, file_path, page FROM assets ORDER BY doc_id, page, asset_id"
    ).fetchall()
    groups: dict[tuple[str, str], list[tuple]] = {}
    for asset_id, doc_id, file_path, page in rows:
        path = documents._resolve_asset_file(str(file_path), doc_id)
        if not Path(path).is_file():
            continue
        digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
        groups.setdefault((doc_id, digest), []).append((asset_id, str(path), page))

    duplicates: list[tuple[str, str, str]] = []  # (重复 id, 保留 id, 文件路径)
    saved = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = members[0][0]
        for asset_id, path, _page in members[1:]:
            duplicates.append((asset_id, keep, path))
            saved += Path(path).stat().st_size

    print(f"\n== 1. 重复图片 ==\n组数 {len(groups)}，可去重 {len(duplicates)} 张，"
          f"约 {saved / 1024 / 1024:.1f}MB")
    if dry_run or not duplicates:
        return {"groups": len(groups), "duplicates": len(duplicates), "bytes": saved}

    trash = documents.DATA_DIR / ".dupes" / datetime.now().strftime("%Y%m%d-%H%M%S")
    trash.mkdir(parents=True, exist_ok=True)
    mapping = {asset_id: keep for asset_id, keep, _ in duplicates}
    touched_docs: set[str] = set()

    for doc_id in {row[1] for row in rows}:
        markdown_row = conn.execute(
            "SELECT markdown FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        markdown = (markdown_row[0] if markdown_row else "") or ""
        if not markdown:
            continue
        updated = markdown
        for old, keep in mapping.items():
            updated = updated.replace(f"{old}.png", f"{keep}.png")
        if updated != markdown:
            conn.execute(
                "UPDATE documents SET markdown = ? WHERE doc_id = ?", (updated, doc_id)
            )
            touched_docs.add(doc_id)

    for asset_id, keep, _ in duplicates:
        conn.execute(
            "UPDATE chunks SET asset_id = ? WHERE asset_id = ?", (keep, asset_id)
        )
        conn.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))

    for asset_id, _keep, path in duplicates:
        source = Path(path)
        if source.is_file():
            try:
                shutil.move(str(source), str(trash / f"{asset_id}{source.suffix}"))
            except OSError as exc:
                print(f"  移动失败（不影响库）{source.name}：{exc}")
    conn.commit()
    print(f"  改了 {len(touched_docs)} 本书的 markdown，重复文件移到 {trash}")
    return {"groups": len(groups), "duplicates": len(duplicates), "bytes": saved}


def normalize_labels(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    count = conn.execute("SELECT COUNT(*) FROM assets WHERE label = '插图'").fetchone()[0]
    print(f"\n== 2. 占位 label ==\n「插图」字面量 {count} 行 → NULL")
    if dry_run or not count:
        return count
    conn.execute("UPDATE assets SET label = NULL WHERE label = '插图'")
    conn.commit()
    return count


def assign_categories(conn: sqlite3.Connection, *, dry_run: bool) -> int:
    categories = {
        row[0]: row[1]
        for row in conn.execute("SELECT category_id, name FROM categories").fetchall()
    }
    docs = conn.execute(
        "SELECT doc_id, title, category_id FROM documents WHERE category_id IS NULL"
    ).fetchall()
    planned: list[tuple[str, str, str]] = []
    for doc_id, title, _ in docs:
        for name, keywords in CATEGORY_RULES:
            if any(word in str(title) for word in keywords):
                match = next(
                    (cid for cid, cname in categories.items() if name in cname), None
                )
                if match:
                    planned.append((doc_id, str(title), match))
                break

    print(f"\n== 3. 分类归属 ==\n未分类 {len(docs)} 本，可归入 {len(planned)} 本"
          f"（现有分类：{', '.join(categories.values()) or '无'}）")
    for doc_id, title, cid in planned:
        print(f"  {title[:34]:<36} → {cid}")
    left = len(docs) - len(planned)
    if left:
        print(f"  仍留 {left} 本未分类（书名里没有分类关键词，需要人工判断）")
    if dry_run or not planned:
        return len(planned)
    conn.executemany(
        "UPDATE documents SET category_id = ?, updated_at = ? WHERE doc_id = ?",
        [(cid, documents._now(), doc_id) for doc_id, _, cid in planned],
    )
    conn.commit()
    return len(planned)


def fix_conversation_owners(*, dry_run: bool) -> None:
    """把指向不存在账号的会话归属收回来。

    典型来源：早期写死 `user_id="1"`、删掉的测试账号（`u-902b…`）。这些会话**谁都看不到**
    （`owner_of` 返回一个不存在的 id，任何真实账号比对都不等），但也没人删得掉。
    处理方式：只有一个真实账号时（本项目就是），把这些会话归给它；否则只报告不动手。

    游客 id（`g-…`）**不碰**：它本来就不在 users 表里。
    """
    path = REPO_ROOT / "data" / "conversation_owners.json"
    if not path.is_file():
        return
    owners = json.loads(path.read_text(encoding="utf-8") or "{}")
    summaries_path = REPO_ROOT / "data" / "conversation_summaries.json"
    summaries = {}
    if summaries_path.is_file():
        summaries = json.loads(summaries_path.read_text(encoding="utf-8") or "{}")

    import users

    with users._connect() as conn:  # noqa: SLF001 - 运维脚本，直接读表
        humans = [
            row[0] for row in conn.execute(
                "SELECT user_id FROM users WHERE user_id LIKE 'u-%'"
            ).fetchall()
        ]
    stale = {
        thread: owner
        for thread, owner in owners.items()
        if owner.startswith("u-") and owner not in humans
    }
    missing = [thread for thread in summaries if thread not in owners]

    print(f"\n== 4. 会话归属 ==\n真实账号 {humans}")
    print(f"  指向已消失账号的归属 {len(stale)} 条，没有归属的会话 {len(missing)} 条")
    if not stale and not missing:
        return
    if len(humans) != 1:
        print("  有多个真实账号，无法判断该归给谁——只报告，不动手")
        return
    target = humans[0]
    for thread, owner in stale.items():
        print(f"  {thread}：{owner} → {target}")
    for thread in missing:
        print(f"  {thread}：（无归属）→ {target}")
    if dry_run:
        return
    backup = path.with_suffix(f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    shutil.copy2(path, backup)
    owners.update({thread: target for thread in stale})
    owners.update({thread: target for thread in missing})
    path.write_text(json.dumps(owners, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已改，原文件备份到 {backup.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-rechunk", action="store_true")
    ap.add_argument("--no-owners", action="store_true", help="不碰会话归属")
    args = ap.parse_args()

    with documents._connect() as conn:
        before = stats(conn)

    print("== 清理前 ==")
    for key, value in before.items():
        print(f"  {key:<10}{value:>7}")

    with documents._connect() as conn:
        dedupe_assets(conn, dry_run=args.dry_run)
        normalize_labels(conn, dry_run=args.dry_run)
        assign_categories(conn, dry_run=args.dry_run)

    if args.no_rechunk:
        print("\n（跳过重切）")
    else:
        with documents._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, title FROM documents WHERE status = ? ORDER BY title",
                (documents.STATUS_READY,),
            ).fetchall()
        print(f"\n== 5. 重切 {len(rows)} 本（顺带按内容哈希补向量）==")
        for doc_id, title in rows:
            if args.dry_run:
                print(f"  [看] {str(title)[:34]}")
                continue
            try:
                result = documents.rechunk(doc_id)
                print(f"  [改] {str(title)[:34]:<36} {result['chunk_count']} 段", flush=True)
            except Exception as exc:  # noqa: BLE001 - 单本失败继续
                print(f"  [FAIL] {str(title)[:34]:<36} {type(exc).__name__}: {exc}", flush=True)

    if not args.no_owners:
        fix_conversation_owners(dry_run=args.dry_run)

    with documents._connect() as conn:
        after = stats(conn)
    print("\n== 清理后 ==")
    for key, value in after.items():
        delta = value - before[key]
        print(f"  {key:<10}{value:>7}  ({delta:+d})")
    if args.dry_run:
        print("\n（dry-run，什么都没写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
