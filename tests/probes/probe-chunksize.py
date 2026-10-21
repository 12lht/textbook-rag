# -*- coding: utf-8 -*-
"""chunk 尺寸对照：TARGET_CHARS 500（现行） vs 250（切半）。

在语料副本上重切（rechunk 只读 Markdown，不碰 PDF）→ 清空向量重嵌 →
同样的 16 题 × 温度 0 裁判，跑 bm25 / vector / weighted 三种模式。
和 fusion3-result.json 里的 500 基线对比。

注意 chunk_id 会变（段数变多、seq 重排），跨尺寸比较只看判分指标。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

TMP = Path(r"D:/agent/.workbuddy/tmp")
sys.path.insert(0, r"D:/agent")

_spec = importlib.util.spec_from_file_location("probe_judge", TMP / "probe-judge.py")
pj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pj)

import documents
import retrieval

SRC = pj.CORPUS
DST = TMP / "ab-corpus-250"
TARGET = 250
QUERIES = pj.QUERIES
LIMIT = pj.LIMIT


def main() -> None:
    if not DST.exists():
        DST.mkdir()
        shutil.copy2(SRC / "documents.sqlite3", DST / "documents.sqlite3")

    documents.DB_PATH = DST / "documents.sqlite3"
    documents.UPLOAD_DIR = DST / "uploads"   # 重切不碰 assets，目录占位即可
    documents.ASSET_DIR = DST / "assets"
    retrieval.VECTOR_DB = DST / "vectors.sqlite3"
    documents.TARGET_CHARS = TARGET

    docs = documents.list_documents("u1")
    print(f"TARGET_CHARS={TARGET}，重切 {len(docs)} 本书：")
    for d in docs:
        info = documents.rechunk(d["doc_id"])
        print(f"  {d['title'][:16]:<18} {d['chunk_count']} -> {info['chunk_count']} 段")

    # 向量库指向新文件，逐本补算
    print("补算向量……")
    for d in docs:
        stat = retrieval.ensure_vectors("u1")
        print(" ", stat)
        break  # ensure_vectors 是全量补，一次就够
    index = retrieval.get_index("u1")
    print(f"索引就绪：{len(index.docs)} 段，向量 {'有' if index.matrix is not None else '无'}\n")

    model = pj.build_judge()
    summary, detail = {}, {}
    for mode in ("bm25", "vector", "weighted"):
        top1 = useful = answered = 0
        rows = []
        for query in QUERIES:
            if mode == "bm25":
                matrix = index.matrix
                index.matrix = None
                result = retrieval.search("u1", query, limit=LIMIT)
                index.matrix = matrix
            else:
                result = retrieval.search("u1", query, limit=LIMIT)
            hits = result["hits"]
            scores = pj.judge(model, query, [h["content"] for h in hits])
            top1 += scores[0] if scores else 0
            useful += sum(scores)
            answered += 1 if any(scores) else 0
            rows.append({"query": query, "judge": scores,
                         "hits": [h["chunk_id"] for h in hits]})
        summary[mode] = {"top1": top1, "useful": useful, "answered": answered}
        detail[mode] = rows
        print(f"{mode:<9} 第一条能答 {top1}/16   六条里有用段 {useful}   至少一条能答 {answered}/16")

    out = TMP / "fusion3-250.json"
    out.write_text(json.dumps({"summary": summary, "detail": detail}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n明细已写", out)


if __name__ == "__main__":
    main()
