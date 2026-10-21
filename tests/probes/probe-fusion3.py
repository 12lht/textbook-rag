# -*- coding: utf-8 -*-
"""四种检索模式同题对照：纯 BM25 / 纯向量 / RRF / 加权融合（现行）。

复用 probe-judge.py 的题目、裁判和判分口径（前 6 条混给模型逐条打 0/1，
温度 0、关推理）。差别只在检索这一层：

- bm25     ：index.matrix 置 None（生产代码路径，向量关掉）
- weighted ：现行加权融合（0.75/0.25 或 0.35/0.65，按 coverage 分流）
- rrf      ：标准 RRF，k=60，两路各取前 50 名，1/(k+rank) 相加
- vector   ：纯向量 top6（cosine >= _VEC_MIN_COS）

RRF / vector 两条路是探针本地实现（不改生产代码），hit 字段对齐生产格式。
每题每模式的命中明细 + 判分落盘 JSON，用来诊断「差在排序还是差在切分/检索词」。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

TMP = Path(r"D:/agent/.workbuddy/tmp")
sys.path.insert(0, r"D:/agent")

_spec = importlib.util.spec_from_file_location("probe_judge", TMP / "probe-judge.py")
pj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pj)  # 副作用：load_dotenv + import 三件套

import documents
import retrieval

CORPUS = pj.CORPUS
LIMIT = pj.LIMIT
QUERIES = pj.QUERIES
RRF_K = 60
RRF_DEPTH = 50


def _make_hits(index, order, scored, cosines):
    coverage_of = {pos: cov for _, cov, pos in scored}
    hits = []
    for pos in order:
        meta = index.docs[pos].meta
        content = str(meta.get("content") or "")
        if len(content) > retrieval.MAX_HIT_CHARS:
            content = content[: retrieval.MAX_HIT_CHARS].rstrip() + "……"
        hits.append(
            {
                "chunk_id": meta.get("chunk_id"),
                "doc_title": meta.get("doc_title"),
                "chapter": meta.get("chapter"),
                "page_start": meta.get("page_start"),
                "kind": meta.get("kind"),
                "coverage": round(float(coverage_of.get(pos, 0.0)), 3),
                "cosine": round(float(cosines.get(pos, 0.0)), 3),
                "content": content,
            }
        )
    return hits


def search_rrf(user_id: str, query: str, limit: int = LIMIT) -> dict:
    query = retrieval.strip_filler(query)
    index = retrieval.get_index(user_id)
    scored = index.score(query)
    bm25_ranked = [pos for _, _, pos in scored][:RRF_DEPTH]
    vec = index.vector_ranked(query, limit=retrieval._VECTOR_DEPTH)
    vec_ranked = [pos for sim, pos in vec if sim >= retrieval._VEC_MIN_COS][:RRF_DEPTH]
    cosines = {pos: sim for sim, pos in vec}

    fused: dict[int, float] = {}
    for rank, pos in enumerate(bm25_ranked):
        fused[pos] = fused.get(pos, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, pos in enumerate(vec_ranked):
        fused[pos] = fused.get(pos, 0.0) + 1.0 / (RRF_K + rank + 1)
    order = sorted(fused, key=fused.get, reverse=True)[:limit]
    return {"hits": _make_hits(index, order, scored, cosines)}


def search_vector(user_id: str, query: str, limit: int = LIMIT) -> dict:
    query = retrieval.strip_filler(query)
    index = retrieval.get_index(user_id)
    scored = index.score(query)
    vec = index.vector_ranked(query, limit=limit)
    cosines = {pos: sim for sim, pos in vec}
    order = [pos for sim, pos in vec if sim >= retrieval._VEC_MIN_COS]
    return {"hits": _make_hits(index, order, scored, cosines)}


def run_mode(model, index, mode: str) -> dict:
    matrix = index.matrix
    detail = []
    top1 = useful = answered = 0
    for query in QUERIES:
        if mode == "bm25":
            index.matrix = None
            result = retrieval.search("u1", query, limit=LIMIT)
            index.matrix = matrix
        elif mode == "weighted":
            result = retrieval.search("u1", query, limit=LIMIT)
        elif mode == "rrf":
            result = search_rrf("u1", query)
        else:
            result = search_vector("u1", query)

        hits = result["hits"]
        scores = pj.judge(model, query, [h["content"] for h in hits])
        top1 += scores[0] if scores else 0
        useful += sum(scores)
        answered += 1 if any(scores) else 0
        detail.append(
            {
                "query": query,
                "judge": scores,
                "hits": [
                    {k: h[k] for k in ("chunk_id", "doc_title", "chapter", "page_start", "coverage", "cosine")}
                    for h in hits
                ],
            }
        )
    print(
        f"{mode:<9} 第一条能答 {top1}/{len(QUERIES)}   "
        f"六条里有用段 {useful}   至少一条能答 {answered}/{len(QUERIES)}"
    )
    return {"top1": top1, "useful": useful, "answered": answered, "detail": detail}


def main() -> None:
    documents.DB_PATH = CORPUS / "documents.sqlite3"
    documents.UPLOAD_DIR = CORPUS / "uploads"
    documents.ASSET_DIR = CORPUS / "assets"
    retrieval.VECTOR_DB = CORPUS / "vectors.sqlite3"

    index = retrieval.get_index("u1")
    model = pj.build_judge()
    print("裁判：", "温度 0 / 关推理；四模式 × ", len(QUERIES), "题\n")

    summary, all_detail = {}, {}
    for mode in ("bm25", "vector", "rrf", "weighted"):
        result = run_mode(model, index, mode)
        summary[mode] = {k: result[k] for k in ("top1", "useful", "answered")}
        all_detail[mode] = result["detail"]

    out = TMP / "fusion3-result.json"
    out.write_text(
        json.dumps({"summary": summary, "detail": all_detail}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("\n明细已写", out)


if __name__ == "__main__":
    main()
