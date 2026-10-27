"""把 textbook/ 下的六本书（26 个 PDF）走生产链路入库。

create_upload → parse_document（同步解析）→ ensure_vectors。
单本失败不中断（扫描版/坏文件记下来继续），最后汇总 + 清索引缓存。

**会真实消耗 MinerU 额度**（按页算，1000 页/天）。库里已经 ready 的书会直接跳过
（`create_upload` 按内容指纹去重、`parse_document` 对 ready 直接返回），所以中断了
再跑一遍是安全的，不会重复烧。

用法（仓库根目录）：
    python tests/probes/ingest-textbooks.py            # 默认进单人库的那个账号
    python tests/probes/ingest-textbooks.py u-xxxxxxxx # 指定账号
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(REPO_ROOT))
from dotenv import load_dotenv

load_dotenv(str(REPO_ROOT / ".env"))

import documents  # noqa: E402
import retrieval  # noqa: E402
import users  # noqa: E402


def _sole_user() -> str:
    """单人库直接取唯一账号；多账号时必须显式传，猜错了书就进了别人名下。"""
    conn = sqlite3.connect(users.DB_PATH)
    try:
        rows = [row[0] for row in conn.execute("SELECT user_id FROM users ORDER BY created_at")]
    finally:
        conn.close()
    if len(rows) != 1:
        raise SystemExit(f"库里有 {len(rows)} 个账号，请显式传入 user_id")
    return rows[0]


ROOT = REPO_ROOT / "textbook"
MINERU_MAX_BYTES = 200 * 1024 * 1024  # 云端单文件上限，超了会直接报错，别白等

ap = argparse.ArgumentParser()
ap.add_argument("user_id", nargs="?", help="入库账号，默认取本机唯一账号")
ap.add_argument("--max-pages", type=int, default=600,
                help="本次最多烧多少页 MinerU 额度（默认 600，日额度 1000）")
ap.add_argument("--dry-run", action="store_true", help="只列清单和页数，不传不解析")
ap.add_argument("--include-tests", action="store_true",
                help="连 textbook/pdf_test 一起入（那目录是解析器测试用的切片，默认排除）")
args = ap.parse_args()
USER = args.user_id or _sole_user()
print(f"入库账号：{USER}　本次预算 {args.max_pages} 页\n")


def _pages(path: Path) -> int:
    import pymupdf

    pdf = pymupdf.open(str(path))
    try:
        return pdf.page_count
    finally:
        pdf.close()


if not ROOT.is_dir():
    # textbook/ 在 2026-10-25 被移出仓库：以前这里会打印「共 0 个文件」然后继续跑向量，
    # 看起来像成功。批量入库这条路现在走不通，直接说清楚。
    raise SystemExit(
        f"找不到 {ROOT}。textbook/ 已移出仓库，批量入库脚本停用；"
        "请在网页端逐本上传（上传即走同一条解析链路）。"
    )

plan = []
for path in sorted(ROOT.glob("*.pdf")):
    plan.append(path)
# `pdf_test/` 是当初验解析器用的切片样本（成绩单、简历、某本书的 25 页节选），
# 不是教材；一起入会白烧几百页额度，还把「简历」变成可检索教材。
skipped_dirs = []
for folder in sorted(p for p in ROOT.iterdir() if p.is_dir()):
    if folder.name == "pdf_test" and not args.include_tests:
        skipped_dirs.append((folder.name, len(list(folder.glob("*.pdf")))))
        continue
    plan.extend(sorted(folder.glob("*.pdf")))
for name, count in skipped_dirs:
    print(f"（跳过 {name}/：{count} 个测试切片，要入加 --include-tests）")

sized = []
for path in plan:
    sized.append((path, _pages(path), path.stat().st_size))
sized.sort(key=lambda item: (item[1], item[0].name))  # 小的先入：先出可用的书

if not sized:
    raise SystemExit(f"{ROOT} 下一个 PDF 都没有，中止（避免打印「0 个文件」后继续空跑）。")
print(f"共 {len(sized)} 个文件 / {sum(p for _, p, _ in sized)} 页；按页数升序开始\n")

results, remaining, used = [], [], 0
for index, (path, pages, size) in enumerate(sized, 1):
    if size > MINERU_MAX_BYTES:
        results.append((path.name, "SKIP: 超过云端 200MB", pages))
        print(f"[SKIP] {path.name[:34]:<36} {size / 1024 / 1024:.0f}MB 超过云端 200MB 上限")
        continue
    if used + pages > args.max_pages:
        remaining.append((path.name, pages))
        continue
    if args.dry_run:
        used += pages
        print(f"[PLAN] {path.name[:34]:<36} {pages:>4}页  累计 {used}")
        continue

    t0 = time.time()
    try:
        doc = documents.create_upload(USER, path.name, path.read_bytes())
        if doc.get("duplicated") and doc["status"] == documents.STATUS_READY:
            print(f"[{index}/{len(sized)}] {path.name[:34]:<36} 已入库，跳过", flush=True)
            continue
        done = documents.parse_document(doc["doc_id"])
        used += int(done.get("page_count") or pages)
        results.append((path.name, done.get("status"), done.get("page_count")))
        print(f"[{index}/{len(sized)}] {path.name[:34]:<36} {done.get('status')}  "
              f"{done.get('page_count')}页 {done.get('chunk_count')}段  "
              f"{time.time()-t0:.0f}s  累计 {used}页  {done.get('error') or ''}", flush=True)
    except Exception as exc:  # noqa: BLE001 — 单本失败继续下一本
        results.append((path.name, f"ERROR: {exc}", None))
        print(f"[FAIL] {path.name[:34]:<36} {type(exc).__name__}: {str(exc)[:70]}", flush=True)

if args.dry_run:
    print(f"\n计划内合计 {used} 页，预算外 {len(remaining)} 个文件")
    raise SystemExit(0)

print("\n补算向量……", flush=True)
stat = retrieval.ensure_vectors(USER)
print("向量：", stat)
retrieval.clear_cache(USER)

ok = sum(1 for r in results if r[1] == "ready")
print(f"\n完成：{ok}/{len(results)} 本 ready，本次用掉 {used} 页")
if remaining:
    print(f"\n预算外还有 {len(remaining)} 个（约 {sum(p for _, p in remaining)} 页），下次接着跑：")
    for name, pages in remaining:
        print(f"  {pages:>4}页  {name[:50]}")
