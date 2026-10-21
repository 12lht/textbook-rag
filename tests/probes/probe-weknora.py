# -*- coding: utf-8 -*-
"""WeKnora 配置平移实验：把它的公开默认参数搬到我们的语料上跑一遍。

它的配置（文档站 03-document-parsing / 02-architecture/04-rag-pipeline）：
- 切分：chunk_size=512 字符、overlap=80、递归切分（\\n → 。→ 空格 → 字符兜底）、
  保护片段（代码围栏/表格/公式/图片不切断）
- 召回：每路深召回 max(5*TopK, 50) = 50
- 融合：加权 RRF，0.7 向量 : 0.3 关键词，rrfK=60，归一化到 [0,1]
- 重排：复合分 = 0.6*模型分 + 0.3*检索基础分 + 0.1*来源权重（来源全 1.0）
- 合并：短块邻居扩展（<350 字符扩到约 850）

两档对照：A = RRF 直接出 top6；B = RRF + rerank 复合分出 top6。
两档都做邻居扩展（它的合并阶段不区分有无 rerank）。
chunk 元数据里的 page/chapter 从 markdown 锚点解析。
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

TMP = Path(r"D:/agent/.workbuddy/tmp")
sys.path.insert(0, r"D:/agent")

_spec = importlib.util.spec_from_file_location("probe_judge", TMP / "probe-judge.py")
pj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pj)

_spec2 = importlib.util.spec_from_file_location("probe_rerank", TMP / "probe-rerank.py")
pr = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(pr)

import documents
import retrieval

SRC = pj.CORPUS
DST = TMP / "ab-corpus-wkn"
CHUNK_SIZE = 512
OVERLAP = 80
POOL = 50
RRF_K = 60
W_VEC, W_KEY = 0.7, 0.3
NEIGHBOR_MIN, NEIGHBOR_TARGET = 350, 850
QUERIES = pj.QUERIES
LIMIT = 6

RE_PAGE = re.compile(r"<!--\s*page:(\d+)\s*-->")
RE_CHAPTER = re.compile(r"<!--\s*chapter:(.*?)\s*-->")


def build_units(markdown: str) -> list[tuple[str, int, str]]:
    """把 markdown 拆成原子单元：(文本, 页码, 章节)。锚点只更新状态不进内容。

    保护片段：代码围栏、表格行、`$$公式$$`——整块不可切断。
    """
    units: list[tuple[str, int, str]] = []
    page, chapter = 0, ""
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m_p, m_c = RE_PAGE.search(line), RE_CHAPTER.search(line)
        if m_p:
            page = int(m_p.group(1))
            i += 1
            continue
        if m_c and line.strip().startswith("<!--"):
            chapter = m_c.group(1)
            i += 1
            continue
        stripped = line.lstrip()
        if stripped.startswith("```"):
            block = [line]
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if lines[i].lstrip().startswith("```"):
                    i += 1
                    break
                i += 1
            units.append(("\n".join(block), page, chapter))
            continue
        if stripped.startswith("|") or stripped.startswith("$$"):
            block = [line]
            i += 1
            while i < len(lines):
                nxt = lines[i].lstrip()
                if nxt.startswith("|") or nxt.startswith("$$") or nxt.startswith(":---") or nxt.startswith("---"):
                    block.append(lines[i])
                    i += 1
                else:
                    break
            units.append(("\n".join(block), page, chapter))
            continue
        if not line.strip():
            i += 1
            continue
        # 普通行按句号再切（它的递归第二级），超长兜底按字符
        for piece in re.split(r"(?<=[。！？])", line):
            piece = piece.strip()
            if piece:
                while len(piece) > CHUNK_SIZE:
                    units.append((piece[:CHUNK_SIZE], page, chapter))
                    piece = piece[CHUNK_SIZE:]
                if piece:
                    units.append((piece, page, chapter))
        i += 1
    return units


def weknora_chunks(markdown: str) -> list[tuple[str, int, str]]:
    """512/80 合并：满了就出块，新块用上一块末尾 80 字符做重叠。"""
    units = build_units(markdown)
    chunks: list[tuple[str, int, str]] = []
    buffer: list[str] = []
    buf_page, buf_chapter = 0, ""
    buf_len = 0

    def flush(tail_for_overlap: str):
        nonlocal buffer, buf_len
        if buffer:
            chunks.append(("\n".join(buffer), buf_page, buf_chapter))
        buffer, buf_len = [], 0
        if tail_for_overlap:
            buffer.append(tail_for_overlap)
            buf_len = len(tail_for_overlap)

    for text, page, chapter in units:
        if buf_len + len(text) + 1 > CHUNK_SIZE and buf_len > 0:
            prev = "\n".join(buffer)
            flush(prev[-OVERLAP:] if len(prev) > OVERLAP else prev)
            if page:
                buf_page, buf_chapter = page, chapter
        if buf_len == 0:
            buf_page, buf_chapter = page, chapter
        buffer.append(text)
        buf_len += len(text) + 1
        # 单元本身就超限（大代码块/表格）：整块单独出，不硬切
        if buf_len >= CHUNK_SIZE:
            prev = "\n".join(buffer)
            flush(prev[-OVERLAP:] if len(prev) > OVERLAP else prev)
            if page:
                buf_page, buf_chapter = page, chapter
    if buffer:
        chunks.append(("\n".join(buffer), buf_page, buf_chapter))
    return chunks


def install_chunks() -> None:
    if not DST.exists():
        DST.mkdir()
        shutil.copy2(SRC / "documents.sqlite3", DST / "documents.sqlite3")
    conn = sqlite3.connect(DST / "documents.sqlite3")
    conn.row_factory = sqlite3.Row
    docs = conn.execute("SELECT doc_id, markdown, title FROM documents").fetchall()
    for d in docs:
        pieces = weknora_chunks(d["markdown"] or "")
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (d["doc_id"],))
        for seq, (content, page, chapter) in enumerate(pieces, start=1):
            conn.execute(
                "INSERT INTO chunks (chunk_id, doc_id, seq, kind, content, page_start,"
                " page_end, chapter, asset_id, char_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"{d['doc_id']}-{seq:04d}", d["doc_id"], seq, "text", content,
                 page, page, chapter or None, None, len(content)),
            )
        conn.execute(
            "UPDATE documents SET chunk_count = ? WHERE doc_id = ?",
            (len(pieces), d["doc_id"]),
        )
        print(f"  {d['title'][:16]:<18} {len(pieces)} 段（512/80）")
    conn.commit()
    conn.close()


def rrf_ranked(index, query: str) -> list[tuple[float, int]]:
    """加权 RRF 归一化：score = (0.7/(k+vRank) + 0.3/(k+kRank)) * (k+1)。"""
    scored = index.score(query)
    vec = index.vector_ranked(query, limit=POOL)
    norm = (RRF_K + 1) / (W_VEC + W_KEY)
    fused: dict[int, float] = {}
    for rank, (_, _, pos) in enumerate(scored[:POOL]):
        fused[pos] = fused.get(pos, 0.0) + W_KEY / (RRF_K + rank + 1)
    for rank, (sim, pos) in enumerate(vec[:POOL]):
        if sim < retrieval._VEC_MIN_COS:
            continue
        fused[pos] = fused.get(pos, 0.0) + W_VEC / (RRF_K + rank + 1)
    return sorted(((s * norm, p) for p, s in fused.items()), reverse=True)


def expand(index, pos: int) -> str:
    """短块邻居扩展：<350 字符往同书后邻扩到约 850。"""
    content = str(index.docs[pos].meta.get("content") or "")
    if len(content) >= NEIGHBOR_MIN:
        return content
    doc_id = index.docs[pos].meta.get("doc_id")
    seq = int(str(index.docs[pos].meta.get("chunk_id", "")).rsplit("-", 1)[1])
    out = content
    nxt = seq + 1
    while len(out) < NEIGHBOR_TARGET:
        cid = f"{doc_id}-{nxt:04d}"
        row = index.docs_by_id.get(cid) if hasattr(index, "docs_by_id") else None
        if row is None:
            break
        out += "\n" + str(row.meta.get("content") or "")
        nxt += 1
    return out


def run(model, index, label: str, use_rerank: bool) -> dict:
    top1 = useful = answered = 0
    rows = []
    for query in QUERIES:
        q2 = retrieval.strip_filler(query)
        ranked = rrf_ranked(index, q2)
        order = [p for _, p in ranked[:LIMIT]]
        if use_rerank:
            cand_scored = ranked[:POOL]
            cands = [p for _, p in cand_scored]
            texts = [str(index.docs[p].meta.get("content") or "") for p in cands]
            rr = dict(pr.rerank(query, texts, top_n=len(cands)))
            composite = sorted(
                (0.6 * rr.get(i, 0.0) + 0.3 * score + 0.1, pos)
                for i, (score, pos) in enumerate(cand_scored)
            )
            order = [pos for _, pos in composite[::-1][:LIMIT]]
        contents = [expand(index, p)[: retrieval.MAX_HIT_CHARS * 2] for p in order]
        scores = pj.judge(model, query, contents)
        top1 += scores[0] if scores else 0
        useful += sum(scores)
        answered += 1 if any(scores) else 0
        rows.append({"query": query, "judge": scores,
                     "hits": [str(index.docs[p].meta.get("chunk_id")) for p in order]})
        print(f"  {query[:18]:<20} 判1 {sum(scores)}")
    print(f"{label:<22} 第一条能答 {top1}/16   有用段 {useful}   至少一条能答 {answered}/16\n")
    return {"top1": top1, "useful": useful, "answered": answered, "detail": rows}


def main() -> None:
    install_chunks()
    documents.DB_PATH = DST / "documents.sqlite3"
    documents.UPLOAD_DIR = DST / "uploads"
    documents.ASSET_DIR = DST / "assets"
    retrieval.VECTOR_DB = DST / "vectors.sqlite3"
    stat = retrieval.ensure_vectors("u1")
    print("向量：", stat)
    index = retrieval.get_index("u1")
    print(f"索引 {len(index.docs)} 段\n")
    model = pj.build_judge()
    result = {
        "A_rrf": run(model, index, "A: 512/80+RRF", use_rerank=False),
        "B_rrf_rerank": run(model, index, "B: +rerank复合分", use_rerank=True),
    }
    out = TMP / "weknora-config-result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("明细已写", out)


if __name__ == "__main__":
    main()
