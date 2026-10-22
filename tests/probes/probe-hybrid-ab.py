"""看得见差距的对照：BM25 单用 vs BM25 + 向量（严格判据版）。

上一版（285 段 + 「命中段里含有某个词」）看不出差别，两处判据都不严：
语料小，top3 相当于 1%；标记词在整本书里出现几百次，随手撞上就算命中。

这一版两处都改掉：

1. **语料是整本书**（Hello-Algo 348 页 + Hello-Agents 前 140 页 + 课件），1676 段。
2. **标准答案是「某一段」而不是「含某个词」**：
   先用术语提问（这类提问 BM25 的 MRR 实测是 1.000，可信）拿到它认为最相关的
   那一段，把这一段的 chunk_id 当成标准答案；再拿「换个说法」的提问去查，
   看那一段被排到第几名。同一段，同一个标准，两个检索方式各查一次。

另外每条问题都会量一个「字面重合率」——问题里的两字片段有多少出现在标准答案
里。这个数越低，越说明 BM25 在瞎猜。

语料目录固定（`.workbuddy/tmp/ab-corpus`），重跑只重跑查询，不重复解析。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:/agent")
from dotenv import load_dotenv

load_dotenv(r"D:/agent/.env")

import pymupdf

import documents
import retrieval

CORPUS = Path(r"D:/agent/.workbuddy/tmp/ab-corpus")

CASES = [
    ("递归", "函数在自己里面又调用自己，什么时候会栈溢出"),
    ("迭代", "用循环代替一层层往下调用，写法上有什么不一样"),
    ("时间复杂度", "输入规模翻倍之后，跑的时间会怎么变"),
    ("空间复杂度", "程序会吃掉多少内存"),
    ("冒泡排序", "相邻两个比大小、大的往后挪，这种排序叫什么"),
    ("二分查找", "排好序的数组里，每次砍掉一半来定位"),
    ("哈希表", "用键值对存东西、查找只要常数时间的那种结构"),
    ("贪心算法", "每一步都挑当前看起来最好的那个"),
    ("教学目标", "这门课想让我学会什么"),
    ("平时成绩", "期末之外的分数怎么算"),
    ("时延", "信号从这头传到那头要多久"),
    ("蜂窝", "基站为什么要排成一片一片的格子"),
    ("思维链", "让模型先把推理步骤写出来再给答案"),
    ("提示工程", "把要求写清楚，让模型照着做"),
    ("词元", "模型把一句话切成一小块一小块再处理"),
    ("知识库", "把自己准备的资料喂给模型再让它回答"),
]


def subset(source: Path, start: int, end: int) -> bytes:
    src = pymupdf.open(source)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=start, to_page=min(end, src.page_count - 1))
    data = out.tobytes()
    src.close()
    out.close()
    return data


def build_corpus() -> None:
    documents.DB_PATH = CORPUS / "documents.sqlite3"
    documents.UPLOAD_DIR = CORPUS / "uploads"
    documents.ASSET_DIR = CORPUS / "assets"
    documents.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    documents.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    retrieval.VECTOR_DB = CORPUS / "vectors.sqlite3"

    desktop = Path("C:/Users/13526/Desktop")
    compare = Path("D:/agent/.workbuddy/tmp/marker-compare/pdf")
    sources = [
        (desktop / "Hello-Algo.pdf", None, None, "Hello-Algo"),
        (desktop / "Hello-Agents.pdf", 0, 139, "Hello-Agents"),
        (compare / "mobile.pdf", None, None, "移动通信"),
    ]
    for path, start, end, label in sources:
        data = path.read_bytes() if start is None else subset(path, start, end)
        record = documents.create_upload("u1", f"{label}.pdf", data)
        if not record.get("duplicated"):
            documents.parse_document(record["doc_id"])
            print(f"  解析 {label}")


def overlap(query: str, text: str) -> float:
    """问题里的两字片段有多少出现在答案段落里。低 = 真的换了说法。"""
    grams = [token[2:] for token, _ in retrieval.tokenize(query) if token.startswith("b:")]
    if not grams:
        return 0.0
    flat = retrieval.flat(text)
    return sum(1 for gram in grams if gram in flat) / len(grams)


def mrr(ranks: list[int | None]) -> float:
    return sum(1.0 / rank if rank else 0.0 for rank in ranks) / max(len(ranks), 1)


def main() -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    build_corpus()

    stats = retrieval.ensure_vectors("u1")
    if stats.get("embedded"):
        print(f"补向量 {stats}")

    index = retrieval.get_index("u1")
    lengths = [len(str(doc.meta.get("content") or "")) for doc in index.docs]
    print(f"语料 {len(index.docs)} 段，中位 {statistics.median(lengths):.0f} 字\n")

    matrix = index.matrix
    index.matrix = None
    ground: list[tuple[str, str, str, float]] = []  # (术语, 换说法, chunk_id, 重合率)
    skipped = []
    for term, paraphrase in CASES:
        hits = retrieval.search("u1", term, limit=1)["hits"]
        if not hits or not hits[0]["content"].strip():
            skipped.append(term)
            continue
        top = hits[0]
        ground.append((term, paraphrase, str(top["chunk_id"]), overlap(paraphrase, top["content"])))
    index.matrix = matrix

    print("标准答案（术语提问的第一条）：")
    for term, _para, chunk_id, ratio in ground:
        doc = next(d for d in index.docs if str(d.meta.get("chunk_id")) == chunk_id)
        print(
            f"  {term:<6} 段 {chunk_id}  换说法字面重合 {ratio:.0%}"
            f"  {doc.meta['content'][:34]!r}"
        )
    if skipped:
        print("  无效测例（术语都搜不到）：", skipped)

    def run(label: str) -> None:
        print(f"\n=== {label} ===")
        ranks: list[int | None] = []
        for term, paraphrase, chunk_id, _ratio in ground:
            hits = retrieval.search("u1", paraphrase, limit=10)["hits"]
            rank = next(
                (i + 1 for i, hit in enumerate(hits) if str(hit["chunk_id"]) == chunk_id),
                None,
            )
            ranks.append(rank)
            top = hits[0] if hits else None
            mark = "✓" if rank == 1 else ("·" if rank else "✗")
            print(
                f"{mark} [{rank or '-'}] {paraphrase!r}"
                f"  cov={top['coverage'] if top else 0:.2f}"
                f"  cos={top['cosine'] if top else 0:.2f}"
            )
        top1 = sum(1 for rank in ranks if rank == 1)
        hit5 = sum(1 for rank in ranks if rank and rank <= 5)
        print(f"top1 {top1}/{len(ground)}  前5名内 {hit5}/{len(ground)}   MRR {mrr(ranks):.3f}")

    index.matrix = None
    run("BM25 单用 · 换个说法")
    index.matrix = matrix
    run("BM25 + 向量 · 换个说法")


if __name__ == "__main__":
    main()
