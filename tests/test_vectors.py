"""向量新鲜度回归：改过 Markdown 之后，向量必须跟着变（全离线，不调外部服务）。

守的是这条链：

    改 markdown → rechunk（chunk_id 不变、正文变了）→ 向量要重算 → 内存索引要失效

以前三个地方会断在这条链上：
1. `ensure_vectors` 只按 `chunk_id` 判「有没有」→ 同 id 换了内容永远不重算；
2. `fill-concepts` 走 `describe_placeholders`（改 markdown + rechunk）但从不补向量；
3. `_signature` 不含内容指纹，同数量重切后内存索引不失效。

原 `tests/probes/probe-vectors.py`，2026-11-03 迁到 pytest。**每个用例独立建库**，
不再像探针那样靠执行顺序传递状态——原来第 6 段跑失败，前 5 段的结论就无从复核。
"""
from __future__ import annotations

import hashlib

import pytest

import documents
import retrieval

MARKDOWN = "\n".join([
    "<!-- page:1 -->",
    "<!-- chapter:第 1 章 -->",
    "第一章讲的是抽样定理。采样频率必须大于两倍最高频率，这是奈奎斯特定理的核心。",
    "",
    "<!-- page:2 -->",
    "<!-- chapter:第 2 章 -->",
    "第二章讲量化误差。均匀量化的信噪比与位数成正比，每增加一位约提升 6dB。",
])


def _fake_embed_factory(calls: list[str]):
    def fake_embed(texts: list[str]) -> list[list[float]]:
        """确定性「向量」：内容一变向量就变，长度固定 8 维。"""
        calls.extend(texts)
        return [
            [byte / 255 for byte in hashlib.sha256(text.encode("utf-8")).digest()[:8]]
            for text in texts
        ]

    return fake_embed


def _set_markdown(doc_id: str, markdown: str) -> None:
    with documents._connect() as conn:
        conn.execute(
            "UPDATE documents SET status = ?, markdown = ?, page_count = 2 WHERE doc_id = ?",
            (documents.STATUS_READY, markdown, doc_id),
        )
        conn.commit()


def vector_of(chunk_id: str) -> tuple[bytes, str] | None:
    conn = retrieval._connect_vectors()
    try:
        row = conn.execute(
            "SELECT vec, chunk_hash FROM vectors WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


@pytest.fixture
def book(tmp_store, monkeypatch):
    """一本只有 markdown、状态 ready 的书；向量库和 embedding 都指向临时/假实现。"""
    monkeypatch.setattr(retrieval, "VECTOR_DB", tmp_store / "vectors.sqlite3")
    monkeypatch.setenv("EMBEDDING_API_KEY", "probe-key")  # 只过 enabled 检查，_embed 被替换
    calls: list[str] = []
    monkeypatch.setattr(retrieval, "_embed", _fake_embed_factory(calls))

    record = documents.create_upload("u-probe", "样书.pdf", b"%PDF-1.4 fake")
    doc_id = str(record["doc_id"])
    _set_markdown(doc_id, MARKDOWN)
    documents.rechunk(doc_id)
    return doc_id, calls


def test_every_chunk_gets_vector(book):
    doc_id, _ = book
    result = retrieval.ensure_vectors("u-probe")
    chunk_ids = [str(c["chunk_id"]) for c in documents.chunks_of(doc_id)]

    missing = [cid for cid in chunk_ids if vector_of(cid) is None]
    assert missing == [], f"{len(missing)} 条没有向量：{result}"
    assert len(chunk_ids) == result["total"], f"向量数 ≠ chunk 数：{result}"


def test_no_recompute_when_content_unchanged(book):
    """内容没变时不该重复花钱：一次网络调用都不能有。"""
    doc_id, calls = book
    retrieval.ensure_vectors("u-probe")
    calls.clear()

    result = retrieval.ensure_vectors("u-probe")
    assert result.get("embedded") == 0, result
    assert calls == [], f"白算了 {len(calls)} 段"


def test_same_chunk_id_with_new_content_is_recomputed(book):
    """等长换内容：段数不变、chunk_id 不变，只有正文变——这正是补图注/包公式的形状。"""
    doc_id, _ = book
    retrieval.ensure_vectors("u-probe")

    target = documents.chunks_of(doc_id)[0]
    chunk_id = str(target["chunk_id"])
    before = vector_of(chunk_id)
    assert before is not None, "改之前就没有向量"

    original = str(target["content"])
    mutated = original.replace("抽样定理", "采样定理", 1)
    assert len(mutated) == len(original), f"改动不等长：{len(original)} → {len(mutated)}"

    with documents._connect() as conn:
        conn.execute("UPDATE chunks SET content = ? WHERE chunk_id = ?", (mutated, chunk_id))
        conn.commit()

    result = retrieval.ensure_vectors("u-probe")
    assert result.get("embedded") == 1, f"应该只重算 1 条：{result}"

    after = vector_of(chunk_id)
    assert after is not None and after[0] != before[0], "向量没变，说明没重算"
    assert after[1] == retrieval._chunk_hash({**target, "content": mutated}), "chunk_hash 没更新"


def test_rechunk_refreshes_vectors(book):
    """`rechunk` 自己要把向量带上——fill-concepts 走的就是这条路。"""
    doc_id, calls = book
    new_markdown = MARKDOWN.replace("均匀量化的信噪比与位数成正比", "均匀量化的信噪比随位数线性提升")
    _set_markdown(doc_id, new_markdown)
    calls.clear()

    documents.rechunk(doc_id)
    assert len(calls) > 0, "rechunk 内部没触发补向量"

    stale = [
        chunk for chunk in documents.chunks_of(doc_id)
        if vector_of(str(chunk["chunk_id"])) is None
        or vector_of(str(chunk["chunk_id"]))[1] != retrieval._chunk_hash(chunk)
    ]
    assert stale == [], f"{len(stale)} 条向量是旧的"


def test_signature_changes_after_rechunk(book):
    """内存索引的签名要跟着内容变，否则会一直吃旧索引。"""
    doc_id, _ = book
    before = retrieval._signature("u-probe")

    _set_markdown(doc_id, MARKDOWN.replace("第二章讲量化误差", "第二章讨论量化误差"))
    documents.rechunk(doc_id)
    assert retrieval._signature("u-probe") != before, "重切后签名没变"

    # 同一秒内两次重切：updated_at 可能一模一样，靠语料版本号兜住
    retrieval.clear_cache("u-probe")
    _set_markdown(doc_id, MARKDOWN)
    documents.rechunk(doc_id)
    first = retrieval._signature("u-probe")
    documents.rechunk(doc_id)
    assert retrieval._signature("u-probe") != first, "同一秒两次重切签名相同"


def test_empty_hash_is_backfilled_without_recomputing(book):
    """老库 chunk_hash 为空时要补哈希，而不是把全部向量重算一遍（那是白烧额度）。"""
    doc_id, calls = book
    retrieval.ensure_vectors("u-probe")

    conn = retrieval._connect_vectors()
    conn.execute("UPDATE vectors SET chunk_hash = ''")
    conn.commit()
    conn.close()

    calls.clear()
    result = retrieval.ensure_vectors("u-probe")
    assert result.get("embedded") == 0, f"空哈希被当成陈旧了：{result}"
    assert calls == [], f"白算了 {len(calls)} 段"

    hashes = [vector_of(str(c["chunk_id"]))[1] for c in documents.chunks_of(doc_id)]
    assert all(hashes), f"哈希没补上：{hashes[:2]}"
