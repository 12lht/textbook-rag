"""检索召回探针：留一法（leave-one-out）基线。

从库里抽若干正文段，取其中一句原文当查询，看检索能不能把**它自己所在的段**找回来。
不需要人工标注，也不需要知道答案——原文本身就是 gold。

指标：
  recall@1/3/5  正确段在不在前 k
  MRR           正确段排名的倒数均值（第 1 名 = 1.0，第 2 名 = 0.5）
  自信误判率     正确段明明找回来了，却被判 confident=False（说明阈值过严）

用法（仓库根目录）：
    python tests/probes/probe-retrieval.py --samples 60
    python tests/probes/probe-retrieval.py --samples 200 --rerank
    python tests/probes/probe-retrieval.py --out .workbuddy/tmp/retrieval-before.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001 - 没有 dotenv 也能跑（环境变量已在外部导入）
    pass

import retrieval  # noqa: E402

_WS = re.compile(r"\s+")
_MD_NOISE = re.compile(r"^[|#>*\-+=\s]+|[|`]+")


def pick_query(content: str, *, low: int = 14, high: int = 40) -> str | None:
    """从一段正文里挑一句能当查询的原文。要够长（有信息量）又不能太长（不像问题）。"""
    best: str | None = None
    for raw in content.splitlines():
        line = _WS.sub(" ", _MD_NOISE.sub("", raw)).strip()
        if not (low <= len(line) <= high):
            continue
        if sum(1 for ch in line if "\u4e00" <= ch <= "\u9fff") < 6:
            continue
        if line.endswith(("：", ":")):
            continue
        if best is None or len(line) > len(best):
            best = line
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.getenv("BENCH_USER_ID", "u-671d242584"))
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rerank", action="store_true", help="打开 rerank（默认关，慢且要钱）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.rerank:
        os.environ["RERANK_ENABLED"] = "1"
    else:
        os.environ["RERANK_ENABLED"] = "0"

    index = retrieval.get_index(args.user)
    pool = [
        doc for doc in index.docs
        if str(doc.meta.get("kind") or "") == "text" and 200 <= len(str(doc.meta.get("content") or "")) <= 900
    ]
    rng = random.Random(args.seed)
    rng.shuffle(pool)

    records: list[dict] = []
    started = time.perf_counter()
    for doc in pool:
        if len(records) >= args.samples:
            break
        content = str(doc.meta.get("content") or "")
        query = pick_query(content)
        if not query:
            continue
        chunk_id = str(doc.meta.get("chunk_id"))
        try:
            result = retrieval.search(args.user, query, limit=max(5, args.k))
        except Exception as exc:  # noqa: BLE001 - 探针要看清崩在哪，而不是整个中断
            records.append({"chunk_id": chunk_id, "query": query, "error": f"{type(exc).__name__}: {exc}"})
            continue
        hits = result["hits"]
        ids = [str(hit["chunk_id"]) for hit in hits]
        rank = ids.index(chunk_id) + 1 if chunk_id in ids else 0
        # 同一段正文可能被切成多个 chunk（重复内容），出现任意一个都算命中
        if not rank:
            same = {str(index.docs[p].meta.get("chunk_id")) for p in range(len(index.docs))
                    if index.docs[p].flat == doc.flat}
            rank = next((ids.index(sid) + 1 for sid in ids if sid in same), 0)
        records.append({
            "chunk_id": chunk_id,
            "query": query,
            "doc_title": doc.meta.get("doc_title"),
            "rank": rank,
            "confident": result["confident"],
            "top_coverage": hits[0]["coverage"] if hits else 0.0,
            "top_cosine": hits[0]["cosine"] if hits else 0.0,
        })

    ok = [r for r in records if "error" not in r]
    if not ok:
        print("没有可用样本")
        return 1

    def rate(threshold: int) -> float:
        return sum(1 for r in ok if 0 < r["rank"] <= threshold) / len(ok)

    mrr = sum(1.0 / r["rank"] for r in ok if r["rank"]) / len(ok)
    found = [r for r in ok if r["rank"]]
    blind = [r for r in found if not r["confident"]]
    errors = [r for r in records if "error" in r]

    elapsed = time.perf_counter() - started
    print(f"\n样本 {len(ok)}  用户 {args.user}  库 {len(index.docs)} 段  "
          f"rerank={'on' if args.rerank else 'off'}  {elapsed:.1f}s")
    print(f"recall@1 {rate(1):.3f}   recall@3 {rate(3):.3f}   recall@{args.k} {rate(args.k):.3f}   MRR {mrr:.3f}")
    print(f"找回但被判不自信 {len(blind)}/{len(found)}" + (f"   崩 {len(errors)}" if errors else ""))
    for r in ok:
        if not r["rank"]:
            print(f"  漏   {r['query'][:26]:<28} cov={r['top_coverage']:.3f} cos={r['top_cosine']:.3f}")
    if errors:
        print("  错误样本：")
        for r in errors[:5]:
            print(f"    {r['query'][:24]}  {r['error']}")

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "user": args.user, "samples": len(ok), "k": args.k,
            "rerank": args.rerank, "elapsed_s": round(elapsed, 1),
            "recall_at_1": rate(1), "recall_at_3": rate(3),
            "recall_at_k": rate(args.k), "mrr": mrr,
            "found_but_not_confident": len(blind), "found": len(found),
            "errors": errors, "records": records,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
