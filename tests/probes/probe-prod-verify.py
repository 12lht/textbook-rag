"""生产改动落地验证：新 TARGET=250 + rerank 复合分 + 邻居扩展，走真实 search() 路径。

步骤：ab-corpus 重切（新切分参数）→ 清向量重嵌 → 16 题裁判（和生产完全同一条代码路径）。
对照基线（此前实验的最优）：有用段 28~29，能答 11/16。
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
_spec.loader.exec_module(pj)

import documents
import retrieval

CORPUS = pj.CORPUS
QUERIES = pj.QUERIES
LIMIT = 6


def main() -> None:
    documents.DB_PATH = CORPUS / "documents.sqlite3"
    documents.UPLOAD_DIR = CORPUS / "uploads"
    documents.ASSET_DIR = CORPUS / "assets"
    retrieval.VECTOR_DB = CORPUS / "vectors.sqlite3"

    docs = documents.list_documents("u1")
    for d in docs:
        info = documents.rechunk(d["doc_id"])
        print(f"重切 {d['title'][:14]:<16} {d['chunk_count']} -> {info['chunk_count']} 段")
    retrieval.clear_cache()

    import sqlite3
    total = sum(d["chunk_count"] for d in docs)
    vdb = CORPUS / "vectors.sqlite3"
    have = 0
    if vdb.exists():
        v = sqlite3.connect(vdb)
        try:
            have = v.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        except sqlite3.OperationalError:
            have = 0
        v.close()
    if have != total:
        if vdb.exists():
            vdb.unlink()
        stat = retrieval.ensure_vectors("u1")
        print("向量：", stat)
    else:
        print(f"向量已就绪（{have}/{total}），跳过重嵌")

    probe = retrieval._rerank_scores("递归什么时候栈溢出", ["递归是函数调用自身。", "冒泡排序每次交换相邻元素。"])
    print("rerank 连通：", "OK" if probe is not None else "!! 失败，将全程走融合降级")

    index = retrieval.get_index("u1")
    model = pj.build_judge()
    top1 = useful = answered = 0
    rows = []
    for query in QUERIES:
        hits = retrieval.search("u1", query, limit=LIMIT)["hits"]
        scores = pj.judge(model, query, [h["content"] for h in hits])
        top1 += scores[0] if scores else 0
        useful += sum(scores)
        answered += 1 if any(scores) else 0
        rows.append({"query": query, "judge": scores,
                     "hits": [{"id": h["chunk_id"], "len": len(h["content"]),
                               "rr": h.get("score")} for h in hits]})
        lens = [h["content"].__len__() for h in hits]
        print(f"  {query[:18]:<20} 判1 {sum(scores)}  命中长度 {lens}")
    print(f"\n生产路径（250+rerank+扩展）  第一条能答 {top1}/16   有用段 {useful}   至少一条能答 {answered}/16")
    out = TMP / "prod-verify.json"
    out.write_text(json.dumps({"top1": top1, "useful": useful, "answered": answered,
                               "detail": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("明细已写", out)


if __name__ == "__main__":
    main()
