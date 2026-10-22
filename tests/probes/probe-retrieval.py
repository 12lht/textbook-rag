"""检索层探针：BM25 单用 vs BM25 + 向量 的对照。

两部分：
  1. 术语提问（书里原话）——向量不该让这部分变差；
  2. 换个说法提问（口语/同义词）——向量应该救回一批。

真书入库、临时库、临时目录，不碰 data/（向量库也换成临时路径）。
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, r"D:/agent")
from dotenv import load_dotenv

load_dotenv(r"D:/agent/.env")

import documents
import retrieval

TERM_QUERIES = [
    ("尾递归和普通递归的区别", "尾递归"),
    ("递归的空间复杂度怎么算", "空间复杂度"),
    ("线性阶的时间复杂度", "线性阶"),
    ("图 2-10 讲了什么", "2‑10"),
    ("常数阶", "常数阶"),
    ("对数阶出现在什么场合", "对数阶"),
    ("平时成绩占多少", "平时成绩"),
    ("课程目标", "教学目标"),
    ("5G 的空口时延", "时延"),
    ("什么是提示工程", "提示工程"),
    ("思维链", "思维链"),
]

PARAPHRASE = [
    ("算法跑得快不快", "时间复杂度"),
    ("程序为什么越跑越慢", "时间复杂度"),
    ("怎么让程序少占点内存", "空间复杂度"),
    ("把递归改写成循环", "迭代"),
    ("移动通信这门课怎么算成绩", "期末考试"),
    ("这课学完能干什么", "教学目标"),
]

OUT_BOOK = [
    "今天北京天气怎么样",
    "帮我写一首关于秋天的诗",
    "2026 年诺贝尔物理学奖颁给了谁",
]


def ingest(sources: list[Path], user: str) -> None:
    for path in sources:
        record = documents.create_upload(user, f"{path.stem}.pdf", path.read_bytes())
        documents.parse_document(record["doc_id"])


def evaluate(label: str, queries: list[tuple[str, str]]) -> None:
    hit1 = hit3 = 0
    print(f"\n--- {label} ---")
    for query, marker in queries:
        result = retrieval.search("u1", query, limit=5)
        contents = [hit["content"] for hit in result["hits"]]
        rank = next((i + 1 for i, text in enumerate(contents) if marker in text), None)
        hit1 += rank == 1
        hit3 += rank is not None and rank <= 3
        top = result["hits"][0] if result["hits"] else None
        print(
            f"{'✓' if rank == 1 else ('·' if rank else '✗')} [{rank or '-'}] {query!r}"
            f"  cov={top['coverage'] if top else 0:.2f}"
            f"  cos={top['cosine'] if top else 0:.2f}"
            f"  {(top['content'][:38] if top else '')!r}"
        )
    print(f"命中率：top1 {hit1}/{len(queries)}，top3 {hit3}/{len(queries)}")


def reject_rate() -> None:
    ok = 0
    print("\n--- 书里没有的问题（都该 confident=False）---")
    for query in OUT_BOOK:
        result = retrieval.search("u1", query, limit=3)
        top = result["hits"][0] if result["hits"] else None
        ok += not result["confident"]
        print(
            f"{'✓' if not result['confident'] else '✗'} {query!r}"
            f"  confident={result['confident']}"
            f"  cov={top['coverage'] if top else 0:.2f}"
            f"  cos={top['cosine'] if top else 0:.2f}"
            f"  {(top['content'][:30] if top else '')!r}"
        )
    print(f"拒绝率：{ok}/{len(OUT_BOOK)}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="probe-retrieval-"))
    documents.DB_PATH = tmp / "documents.sqlite3"
    documents.UPLOAD_DIR = tmp / "uploads"
    documents.ASSET_DIR = tmp / "assets"
    documents.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    documents.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    retrieval.VECTOR_DB = tmp / "vectors.sqlite3"

    print("向量服务：", "已配置" if retrieval.embedding_enabled() else "未配置（只能跑 BM25）")
    if not os.getenv("SILICON_API_KEY"):
        print("  SILICON_API_KEY 没读到")

    base = Path("D:/agent/.workbuddy/tmp/marker-compare/pdf")
    ingest([base / "algo.pdf", base / "agents.pdf", base / "mobile.pdf"], "u1")

    started = time.time()
    stats = retrieval.ensure_vectors("u1")
    print(f"补向量：{stats}  耗时 {time.time() - started:.1f}s")
    if stats.get("error"):
        print("  向量失败，后面只有 BM25")

    index = retrieval.get_index("u1")
    print(
        f"索引：{len(index.docs)} 段，向量 "
        f"{0 if index.matrix is None else len(index.vector_positions)} 条"
    )

    matrix = index.matrix
    index.matrix = None
    evaluate("BM25 单用 · 术语提问", TERM_QUERIES)
    evaluate("BM25 单用 · 换个说法", PARAPHRASE)
    reject_rate()

    if matrix is None:
        print("\n没有向量，结束。")
        return

    index.matrix = matrix
    evaluate("BM25 + 向量 · 术语提问", TERM_QUERIES)
    evaluate("BM25 + 向量 · 换个说法", PARAPHRASE)
    reject_rate()

    lengths = [len(str(doc.meta.get("content") or "")) for doc in index.docs]
    print(f"\n语料：{len(index.docs)} 段，中位 {statistics.median(lengths):.0f} 字")


if __name__ == "__main__":
    main()
