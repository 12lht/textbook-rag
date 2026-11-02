"""向量新鲜度回归：改过 Markdown 之后，向量必须跟着变（全离线，不调外部服务）。

守的是这条链：
    改 markdown → rechunk（chunk_id 不变、正文变了）→ 向量要重算 → 内存索引要失效

以前三个地方会断在这条链上：
1. `ensure_vectors` 只按 `chunk_id` 判「有没有」→ 同 id 换了内容永远不重算；
2. `fill-concepts` 走 `describe_placeholders`（改 markdown + rechunk）但从不补向量；
3. `_signature` 不含内容指纹，同数量重切后内存索引不失效。

跑法：python tests/probes/probe-vectors.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

TMP = Path(tempfile.mkdtemp(prefix="vectors-probe-"))

import documents

from documents import db as documents_db  # noqa: E402
import retrieval  # noqa: E402

# 全部指到临时目录：绝不能碰真库
documents_db.DB_PATH = TMP / "documents.sqlite3"
documents_db.UPLOAD_DIR = TMP / "uploads"
documents_db.ASSET_DIR = TMP / "assets"
retrieval.VECTOR_DB = TMP / "vectors.sqlite3"

os.environ["EMBEDDING_API_KEY"] = "probe-key"  # 只过 enabled 检查，_embed 被替换

failures: list[str] = []
embed_calls: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def fake_embed(texts: list[str]) -> list[list[float]]:
    """确定性「向量」：内容一变向量就变，长度固定 8 维。"""
    embed_calls.extend(texts)
    out = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        out.append([byte / 255 for byte in digest[:8]])
    return out


def vector_of(chunk_id: str) -> bytes | None:
    conn = retrieval._connect_vectors()
    try:
        row = conn.execute("SELECT vec, chunk_hash FROM vectors WHERE chunk_id = ?", (chunk_id,)).fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


def main() -> int:
    retrieval._embed = fake_embed

    record = documents.create_upload("u-probe", "样书.pdf", b"%PDF-1.4 fake")
    doc_id = str(record["doc_id"])
    markdown = "\n".join([
        "<!-- page:1 -->",
        "<!-- chapter:第 1 章 -->",
        "第一章讲的是抽样定理。采样频率必须大于两倍最高频率，这是奈奎斯特定理的核心。",
        "",
        "<!-- page:2 -->",
        "<!-- chapter:第 2 章 -->",
        "第二章讲量化误差。均匀量化的信噪比与位数成正比，每增加一位约提升 6dB。",
    ])
    with documents._connect() as conn:
        conn.execute(
            "UPDATE documents SET status = ?, markdown = ?, page_count = 2 WHERE doc_id = ?",
            (documents.STATUS_READY, markdown, doc_id),
        )
        conn.commit()
    documents.rechunk(doc_id)

    print("== 1. 首次补向量 ==\n")
    result = retrieval.ensure_vectors("u-probe")
    chunk_ids = [str(chunk["chunk_id"]) for chunk in documents.chunks_of(doc_id)]
    check("每条 chunk 都有向量", all(vector_of(cid) for cid in chunk_ids), str(result))
    check("没有多余的旧向量（数量一致）", len(chunk_ids) == result["total"], str(result))

    print("\n== 2. 内容没变时不该重复花钱 ==\n")
    embed_calls.clear()
    result = retrieval.ensure_vectors("u-probe")
    check("第二次 0 条重算", result.get("embedded") == 0, str(result))
    check("一次网络调用都没有", embed_calls == [], f"{len(embed_calls)} 次")

    print("\n== 3. 同 chunk_id、等长换内容：必须重算 ==\n")
    chunks = documents.chunks_of(doc_id)
    target = chunks[0]
    before = vector_of(str(target["chunk_id"]))
    check("改前有向量", before is not None)

    # 等长改一个字：段数不变、chunk_id 不变，只有正文变——这正是补图注/包公式的形状
    original = str(target["content"])
    mutated = original.replace("抽样定理", "抽样定理".replace("抽", "采"), 1)
    check("改动是等长的", len(mutated) == len(original), f"{len(original)} → {len(mutated)}")
    with documents._connect() as conn:
        conn.execute(
            "UPDATE chunks SET content = ? WHERE chunk_id = ?", (mutated, target["chunk_id"])
        )
        conn.commit()

    result = retrieval.ensure_vectors("u-probe")
    check("只重算变了的那一条", result.get("embedded") == 1, str(result))
    after = vector_of(str(target["chunk_id"]))
    check("向量确实换了", after is not None and after[0] != before[0])
    check("chunk_hash 跟着更新", after is not None and after[1] == retrieval._chunk_hash(
        {**target, "content": mutated}))

    print("\n== 4. rechunk 自己会把向量带上（fill-concepts 的漏）==\n")
    new_markdown = markdown.replace("均匀量化的信噪比与位数成正比", "均匀量化的信噪比随位数线性提升")
    with documents._connect() as conn:
        conn.execute(
            "UPDATE documents SET markdown = ? WHERE doc_id = ?", (new_markdown, doc_id)
        )
        conn.commit()
    embed_calls.clear()
    documents.rechunk(doc_id)
    check("rechunk 内部触发了补向量", len(embed_calls) > 0, f"{len(embed_calls)} 段送去算向量")

    stale = [
        chunk for chunk in documents.chunks_of(doc_id)
        if vector_of(str(chunk["chunk_id"])) is None
        or vector_of(str(chunk["chunk_id"]))[1] != retrieval._chunk_hash(chunk)
    ]
    check("重切后没有一条向量是旧的", stale == [], f"{len(stale)} 条旧向量")

    print("\n== 5. 内存索引签名要跟着变 ==\n")
    signature_before = retrieval._signature("u-probe")
    with documents._connect() as conn:
        conn.execute(
            "UPDATE documents SET markdown = ? WHERE doc_id = ?",
            (new_markdown.replace("第二章讲量化误差", "第二章讨论量化误差"), doc_id),
        )
        conn.commit()
    documents.rechunk(doc_id)
    check("重切后签名变了", retrieval._signature("u-probe") != signature_before)

    # 同一秒内两次重切：updated_at 可能一模一样，靠语料版本号兜住
    retrieval.clear_cache("u-probe")
    with documents._connect() as conn:
        conn.execute("UPDATE documents SET markdown = ? WHERE doc_id = ?", (new_markdown, doc_id))
        conn.commit()
    documents.rechunk(doc_id)
    first = retrieval._signature("u-probe")
    documents.rechunk(doc_id)  # 同一秒内再来一次
    check("同一秒两次重切签名也不同", retrieval._signature("u-probe") != first)

    print("\n== 6. 老库：chunk_hash 为空时补哈希而不是全量重算 ==\n")
    conn = retrieval._connect_vectors()
    conn.execute("UPDATE vectors SET chunk_hash = ''")
    conn.commit()
    conn.close()
    embed_calls.clear()
    result = retrieval.ensure_vectors("u-probe")
    check("空哈希被当成新鲜（只补哈希）", result.get("embedded") == 0, str(result))
    hashes = [vector_of(str(chunk["chunk_id"]))[1] for chunk in documents.chunks_of(doc_id)]
    check("哈希都补上了", all(hashes), str(hashes[:2]))

    print()
    if failures:
        print(f"{len(failures)} 项失败：" + "、".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
