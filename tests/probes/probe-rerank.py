"""rerank 对照：在 TARGET=250 的语料上，检索候选池 → bge-reranker-v2-m3 重排 → 裁判打分。

候选池 = BM25 前 30 ∪ 向量前 30（并集），rerank 取 top 6。
对照组就是 fusion3-250.json 里已有的 weighted（9/22/10）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

TMP = Path(r"D:/agent/.workbuddy/tmp")
sys.path.insert(0, r"D:/agent")

_spec = importlib.util.spec_from_file_location("probe_judge", TMP / "probe-judge.py")
pj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pj)

import documents
import retrieval

DST = TMP / "ab-corpus-250"
QUERIES = pj.QUERIES
LIMIT = 6
POOL = 30
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def rerank(query: str, docs: list[str], top_n: int = LIMIT) -> list[tuple[int, float]]:
    key = os.environ["SILICON_API_KEY"]
    body = json.dumps(
        {"model": RERANK_MODEL, "query": query, "documents": docs, "top_n": top_n}
    ).encode()
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/rerank",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    delay = 5
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return [(r["index"], r["relevance_score"]) for r in data["results"]]
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                raise
            print(f"  (rerank 失败 {attempt+1}/3：{exc}，{delay}s 后重试)")
            time.sleep(delay)
            delay *= 2


def main() -> None:
    documents.DB_PATH = DST / "documents.sqlite3"
    documents.UPLOAD_DIR = DST / "uploads"
    documents.ASSET_DIR = DST / "assets"
    retrieval.VECTOR_DB = DST / "vectors.sqlite3"
    index = retrieval.get_index("u1")
    model = pj.build_judge()

    top1 = useful = answered = 0
    rows = []
    for query in QUERIES:
        q2 = retrieval.strip_filler(query)
        scored = index.score(q2)
        bm25_pos = [pos for _, _, pos in scored[:POOL]]
        vec_pos = [pos for sim, pos in index.vector_ranked(q2, limit=POOL)]
        cand = list(dict.fromkeys(bm25_pos + vec_pos))
        texts = [str(index.docs[p].meta.get("content") or "") for p in cand]

        ranked = rerank(query, texts)
        order = [cand[i] for i, _ in ranked[:LIMIT]]
        scores_of = {cand[i]: s for i, s in ranked}

        hits = []
        for pos in order:
            meta = index.docs[pos].meta
            content = str(meta.get("content") or "")
            if len(content) > retrieval.MAX_HIT_CHARS:
                content = content[: retrieval.MAX_HIT_CHARS].rstrip() + "……"
            hits.append({"chunk_id": meta.get("chunk_id"), "content": content,
                         "rerank": round(float(scores_of.get(pos, 0.0)), 3)})

        judge_scores = pj.judge(model, query, [h["content"] for h in hits])
        top1 += judge_scores[0] if judge_scores else 0
        useful += sum(judge_scores)
        answered += 1 if any(judge_scores) else 0
        rows.append({"query": query, "judge": judge_scores,
                     "hits": [{k: h[k] for k in ("chunk_id", "rerank")} for h in hits]})
        print(f"  {query[:18]:<20} 判1 {sum(judge_scores)}  rerank分 {[h['rerank'] for h in hits]}")

    print(f"\nrerank    第一条能答 {top1}/16   六条里有用段 {useful}   至少一条能答 {answered}/16")
    print("对照（fusion3-250）：bm25 8/17/9   vector 9/26/10   weighted 9/22/10")
    out = TMP / "rerank-result.json"
    out.write_text(json.dumps({"summary": {"top1": top1, "useful": useful, "answered": answered},
                               "detail": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("明细已写", out)


if __name__ == "__main__":
    main()
