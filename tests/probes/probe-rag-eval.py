"""RAG 测试集跑分：`eval/rag_cases.jsonl` 里的人工题。

和 `probe-retrieval.py` 的分工：
- 那个是**留一法**（拿原文摘句当查询），偏 BM25，只能当回归网；
- 这个是**人手写的问句**（口语化、换过词），用来测「像学生那样问」时能不能找对段。

用法（仓库根目录）：
    python tests/probes/probe-rag-eval.py                    # 跑 test 分片
    python tests/probes/probe-rag-eval.py --split val
    python tests/probes/probe-rag-eval.py --split all -v     # 逐条打印
    python tests/probes/probe-rag-eval.py --rerank           # 打开 rerank

指标：
  recall@1/@3/@k  gold 段（或 gold 书）排在不在前 k
  MRR             gold 段排名的倒数均值
  拒答准确率       负例题里 confident=False 的比例
  误报            负例题被判成「书里有」的条数（这类最难修，逐条打印）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import retrieval  # noqa: E402

CASES = ROOT / "eval" / "rag_cases.jsonl"


def load_cases(path: Path, split: str) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        case = json.loads(line)
        if split != "all" and case.get("split") != split:
            continue
        rows.append(case)
    return rows


def _gold_overlap(index, query: str, gold_ids: list[str]) -> float:
    """题面与 gold 段落的字面重合度（和排序无关，只看这一段里有没有这些词）。

    用它的目的不是评分，是**查标注**：一道题如果和它标的段落几乎没有一个共用的词，
    多半是 target 标错了，或者那段本身是公式块 / 图注。
    """
    query = retrieval.strip_filler(query)
    weights: dict[str, float] = {}
    for token, weight in retrieval.tokenize(query):
        weights[token] = max(weights.get(token, 0.0), weight)
    if not weights:
        return 0.0
    total = sum(index.idf(t) * w for t, w in weights.items()) or 1.0
    best = 0.0
    for chunk_id in gold_ids:
        position = index.position_of.get(chunk_id)
        if position is None:
            continue
        doc = index.docs[position]
        matched = sum(index.idf(t) * w for t, w in weights.items() if doc.tf.get(t))
        best = max(best, matched / total)
    return round(best, 3)


def audit(cases: list[dict], user: str) -> int:
    """跑分之前的体检：**只查标注，不查检索**。

    分两类：
    - 错误（必须改）：结构不对、gold 指向的 chunk/书不存在、负例的锚词其实在库里。
    - 提醒（人看）：正例题面和 gold 几乎没有共用的词。
    负例的 `anchor` 是「断言库里没有这个词」——它把主观判断变成一个能被 LIKE 推翻的
    事实。之前把「股票的市盈率怎么算」当负例，就是因为没有这句话可查，
    直到跑分把它标成误报才发现 Hello-Agents 里真的写了一段市盈率公式。
    """
    errors: list[str] = []
    warnings: list[str] = []
    index = retrieval.get_index(user)
    conn = _connect()

    seen_ids: set[str] = set()
    seen_queries: dict[str, str] = {}
    for case in cases:
        cid = str(case.get("id") or "")
        if not cid:
            errors.append("有用例缺 id")
            continue
        if cid in seen_ids:
            errors.append(f"{cid}: id 重复")
        seen_ids.add(cid)

        kind = case.get("kind")
        if case.get("split") not in {"test", "val"}:
            errors.append(f"{cid}: split 必须是 test / val，现在是 {case.get('split')!r}")
        if kind not in {"answerable", "unanswerable"}:
            errors.append(f"{cid}: kind 必须是 answerable / unanswerable")
        query = str(case.get("query") or "").strip()
        if not query:
            errors.append(f"{cid}: query 为空")
        elif query in seen_queries:
            warnings.append(f"{cid}: 和 {seen_queries[query]} 的题面完全一样")
        else:
            seen_queries[query] = cid

        if kind == "answerable":
            gold_chunks = [str(x) for x in case.get("gold_chunk_ids") or []]
            gold_docs = [str(x) for x in case.get("gold_doc_ids") or []]
            if not gold_chunks and not gold_docs:
                errors.append(f"{cid}: 可答题没有 gold")
            for chunk_id in gold_chunks:
                row = conn.execute(
                    "SELECT c.chunk_id, d.status FROM chunks c JOIN documents d USING(doc_id)"
                    " WHERE c.chunk_id = ?", (chunk_id,)
                ).fetchone()
                if row is None:
                    errors.append(f"{cid}: gold chunk {chunk_id} 不存在（重解析过？）")
                elif row["status"] != "ready":
                    errors.append(f"{cid}: gold chunk {chunk_id} 所属文档状态是 {row['status']}")
            for doc_id in gold_docs:
                row = conn.execute("SELECT status FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
                if row is None:
                    errors.append(f"{cid}: gold doc {doc_id} 不存在")
                elif row["status"] != "ready":
                    errors.append(f"{cid}: gold doc {doc_id} 状态是 {row['status']}")
            overlap = _gold_overlap(index, query, gold_chunks) if gold_chunks else None
            if overlap is not None and overlap < 0.12:
                warnings.append(f"{cid}: 题面与 gold 字面重合只有 {overlap:.3f}，确认 target 没标错")
        else:
            anchor = str(case.get("anchor") or "").strip()
            if not anchor:
                warnings.append(f"{cid}: 负例没有 anchor，无法机械核验「库里确实没有」")
                continue
            hits = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE content LIKE ?", (f"%{anchor}%",)
            ).fetchone()[0]
            if hits:
                errors.append(
                    f"{cid}: 锚词「{anchor}」在库里有 {hits} 段——这条大概率其实是可答题"
                )

    conn.close()

    print(f"\n体检 {len(cases)} 条：错误 {len(errors)}，提醒 {len(warnings)}")
    for item in errors:
        print(f"  ✗ {item}")
    for item in warnings:
        print(f"  · {item}")
    if not errors:
        print("  结构、gold 指向、负例锚词全部通过")
    return 1 if errors else 0


def _connect():
    import sqlite3

    from documents import db

    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_case(user: str, case: dict, k: int) -> dict:
    started = time.perf_counter()
    result = retrieval.search(user, case["query"], limit=k)
    elapsed = time.perf_counter() - started
    hits = result["hits"]
    gold_chunks = set(case.get("gold_chunk_ids") or [])
    gold_docs = set(case.get("gold_doc_ids") or [])

    rank = 0
    matched_by = ""
    for position, hit in enumerate(hits, start=1):
        if str(hit["chunk_id"]) in gold_chunks:
            rank, matched_by = position, "chunk"
            break
        if gold_docs and str(hit["doc_id"]) in gold_docs and not gold_chunks:
            # 书级题：同一本书里任意一段都算命中，但要的是「这本被排到前面」，
            # 所以按第一条命中该书的位置记名次
            rank, matched_by = position, "doc"
            break

    return {
        "id": case["id"],
        "kind": case["kind"],
        "tags": case.get("tags") or [],
        "query": case["query"],
        "rank": rank,
        "matched_by": matched_by,
        "confident": result["confident"],
        "hits": [
            {
                "chunk_id": h["chunk_id"],
                "doc_title": h["doc_title"],
                "score": h["score"],
                "coverage": h["coverage"],
                "cosine": h["cosine"],
                "head": str(h["content"])[:60].replace("\n", " "),
            }
            for h in hits
        ],
        "ms": round(elapsed * 1000),
    }


def summarize(records: list[dict], k: int) -> dict:
    ok = [r for r in records if r["kind"] == "answerable"]
    neg = [r for r in records if r["kind"] == "unanswerable"]

    def rate(limit: int, rows: list[dict]) -> float | None:
        if not rows:
            return None
        return round(sum(1 for r in rows if 0 < r["rank"] <= limit) / len(rows), 4)

    mrr = round(sum(1.0 / r["rank"] for r in ok if r["rank"]) / len(ok), 4) if ok else None
    refuse = round(sum(1 for r in neg if not r["confident"]) / len(neg), 4) if neg else None
    per_tag: dict[str, dict] = {}
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        for tag in r["tags"] or ["untagged"]:
            by_tag[tag].append(r)
    for tag, rows in by_tag.items():
        per_tag[tag] = {
            "n": len(rows),
            "recall@1": rate(1, rows),
            "recall@k": rate(k, rows),
            "mrr": round(sum(1.0 / r["rank"] for r in rows if r["rank"]) / len(rows), 4),
        }
    return {
        "n_answerable": len(ok),
        "n_unanswerable": len(neg),
        "recall@1": rate(1, ok),
        "recall@3": rate(3, ok),
        f"recall@{k}": rate(k, ok),
        "mrr": mrr,
        "refusal_accuracy": refuse,
        "false_positive": [r["id"] for r in neg if r["confident"]],
        "per_tag": per_tag,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--user", default=os.getenv("BENCH_USER_ID", "u-671d242584"))
    ap.add_argument("--split", default="test", choices=["test", "val", "all"])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--audit", action="store_true", help="只体检标注，不跑检索")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    os.environ["RERANK_ENABLED"] = "1" if args.rerank else "0"

    cases = load_cases(Path(args.cases), args.split)
    if not cases:
        print(f"没有 {args.split} 分片的用例")
        return 1

    if args.audit:
        return audit(cases, args.user)

    retrieval.get_index(args.user)  # 预热，别把建索引的时间算进第一条
    records = [run_case(args.user, case, args.k) for case in cases]
    summary = summarize(records, args.k)

    print(f"\n分片 {args.split}  用例 {len(records)}  rerank={'on' if args.rerank else 'off'}  "
          f"中位耗时 {sorted(r['ms'] for r in records)[len(records) // 2]}ms")
    print(f"可答 {summary['n_answerable']}  负例 {summary['n_unanswerable']}")
    print(f"recall@1 {summary['recall@1']}   recall@3 {summary['recall@3']}   "
          f"recall@{args.k} {summary[f'recall@{args.k}']}   MRR {summary['mrr']}")
    if summary["n_unanswerable"]:
        print(f"拒答准确率 {summary['refusal_accuracy']}   误报 {len(summary['false_positive'])} 条")

    print("\n分类：")
    for tag, item in sorted(summary["per_tag"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {tag:<7} n={item['n']:<3} recall@1 {item['recall@1']}  @{args.k} {item['recall@k']}  MRR {item['mrr']}")

    misses = [r for r in records if r["kind"] == "answerable" and not r["rank"]]
    if misses:
        print(f"\n没召回到（{len(misses)} 条）：")
        for r in misses:
            top = r["hits"][0] if r["hits"] else None
            where = f"{top['doc_title'][:18]} cov={top['coverage']} cos={top['cosine']}" if top else "无结果"
            print(f"  {r['id']:<9} {r['query'][:26]:<28} top1: {where}")

    if summary["false_positive"]:
        print(f"\n负例误报（{len(summary['false_positive'])} 条）：")
        for r in records:
            if r["id"] not in summary["false_positive"] or not r["hits"]:
                continue
            top = r["hits"][0]
            print(f"  {r['id']:<9} {r['query'][:24]:<26} → {str(top['doc_title'])[:16]} "
                  f"cov={top['coverage']} cos={top['cosine']} {top['head'][:26]}")

    if args.verbose:
        print("\n逐条：")
        for r in records:
            mark = "✓" if (r["rank"] or (r["kind"] == "unanswerable" and not r["confident"])) else "✗"
            print(f"  {mark} {r['id']:<9} rank={r['rank']} conf={int(r['confident'])} {r['query'][:30]}")

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "split": args.split, "k": args.k, "rerank": args.rerank,
            "user": args.user, "summary": summary, "records": records,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
