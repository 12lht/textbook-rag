"""把 textbook/ 下的六本书（26 个 PDF）走生产链路入库。

create_upload → parse_document（同步解析）→ ensure_vectors。
单本失败不中断（扫描版/坏文件记下来继续），最后汇总 + 清索引缓存。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(REPO_ROOT))
from dotenv import load_dotenv

load_dotenv(str(REPO_ROOT / ".env"))

import documents
import retrieval

ROOT = REPO_ROOT / "textbook"
USER = "1"

files = sorted(ROOT.glob("*.pdf"))
for folder in sorted(p for p in ROOT.iterdir() if p.is_dir()):
    files.extend(sorted(folder.glob("*.pdf")))

print(f"共 {len(files)} 个文件\n")
results = []
for path in files:
    t0 = time.time()
    try:
        data = path.read_bytes()
        doc = documents.create_upload(USER, path.name, data)
        done = documents.parse_document(doc["doc_id"])
        results.append((path.name, done.get("status"), done.get("page_count"),
                        done.get("chunk_count"), done.get("warning") or "", time.time() - t0))
        print(f"[OK] {path.name[:36]:<38} {done.get('status')}  "
              f"{done.get('page_count')}页 {done.get('chunk_count')}段  "
              f"{time.time()-t0:.0f}s  {done.get('warning') or ''}")
    except Exception as exc:  # noqa: BLE001 — 单本失败继续下一本
        results.append((path.name, f"ERROR: {exc}", None, None, "", time.time() - t0))
        print(f"[FAIL] {path.name[:36]:<38} {type(exc).__name__}: {str(exc)[:80]}  {time.time()-t0:.0f}s")

print("\n补算向量……")
stat = retrieval.ensure_vectors(USER)
print("向量：", stat)
retrieval.clear_cache(USER)

ok = sum(1 for r in results if r[1] == "ready")
print(f"\n完成：{ok}/{len(results)} 本 ready")
