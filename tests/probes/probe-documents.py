"""探针：教材摄取（M1）在真实 PDF 上的表现。

刻意用**临时库 + 临时目录**，不碰 data/documents.sqlite3——这个模块的任何探针
都必须先换路径，否则跑一次测试就往生产库里塞一本假书。
"""
from __future__ import annotations

import random
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pymupdf

import documents

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def make_subset(source: Path, target: Path, start: int, end: int) -> None:
    """从真教材里切一段出来当样本，页数可控、跑得快。"""
    src = pymupdf.open(source)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=start, to_page=end - 1)
    out.save(target)
    out.close()
    src.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="probe-doc-"))
    documents.DB_PATH = tmp / "documents.sqlite3"
    documents.UPLOAD_DIR = tmp / "uploads"
    documents.ASSET_DIR = tmp / "assets"

    sample = tmp / "sample.pdf"
    source = REPO_ROOT / "textbook/Hello-Algo.pdf"
    make_subset(source, sample, 30, 60)  # 第 31-60 页，有正文有图

    data = sample.read_bytes()
    print(f"样本：{len(data)/1024:.0f}KB, {source.name} 第 31-60 页\n")

    record = documents.create_upload("u1", "Hello-Algo.pdf", data)
    check("上传后是 pending", record["status"] == "pending", record["status"])
    doc_id = record["doc_id"]

    duplicate = documents.create_upload("u1", "换个名字.pdf", data)
    check("同内容再传返回同一条", duplicate["doc_id"] == doc_id and duplicate.get("duplicated") is True)

    parsed = documents.parse_document(doc_id)
    print(
        f"\n解析结果：status={parsed['status']} 页数={parsed['page_count']}"
        f" chunk={parsed['chunk_count']} 图/式={parsed['asset_count']}"
    )
    check("状态推进到 ready", parsed["status"] == "ready", str(parsed.get("error")))
    check("页数正确", parsed["page_count"] == 30, str(parsed["page_count"]))

    chunks = documents.chunks_of(doc_id)
    texts = [c for c in chunks if c["kind"] == "text"]
    print(f"正文 chunk {len(texts)} 条，平均 {sum(len(c['content']) for c in texts)/max(1,len(texts)):.0f} 字")

    print("\n--- 抽查 20 段 ---")
    sample_chunks = random.Random(42).sample(texts, min(20, len(texts)))
    dirty = 0
    for c in sample_chunks:
        content = c["content"]
        problems = []
        if len(content) < 40:
            problems.append("碎片")
        if len(content) > 1000:
            problems.append(f"超长{len(content)}")
        if any(line.strip().isdigit() for line in content.splitlines()[-1:]):
            problems.append("段尾是页码")
        if problems:
            dirty += 1
        mark = "!" if problems else " "
        print(f"{mark} p{c['page_start']}-{c['page_end']} [{len(content)}字] {'/'.join(problems) or 'ok'} :: {content[:60]}".replace("\n", " "))
    print(f"有问题的段落 {dirty}/{len(sample_chunks)}（验收线：≤3）")
    check("A6 段落干净度 ≤3", dirty <= 3, f"{dirty}")

    with_chapter = [c for c in texts if c["chapter"]]
    print(f"\n带章节的段落 {len(with_chapter)}/{len(texts)}")
    if with_chapter:
        print(f"  例：{with_chapter[0]['chapter']}")

    print("\n--- 图/公式 ---")
    assets = [a for a in documents.chunks_of(doc_id) if a["kind"] in {"figure", "formula"}]
    with_conn = documents._connect()
    with with_conn as conn:
        rows = conn.execute("SELECT * FROM assets WHERE doc_id = ? ORDER BY page", (doc_id,)).fetchall()
    print(f"资产 {len(rows)} 个")
    for row in rows[:12]:
        print(f"  p{row['page']} {row['kind']} {row['label']} {row['width']}x{row['height']} :: {row['caption']}")
    check("A7 抽到图 ≥ 5", len(rows) >= 5, f"{len(rows)}")
    check("A7 都有题注编号", all(r["label"] for r in rows))

    print()
    check("别人查不到这本书", documents.get_document(doc_id, "u2") is None)
    check("别人删不掉这本书", documents.delete_document(doc_id, "u2") is False)
    if rows:
        check("别人取不到这张图", documents.asset_of(rows[0]["asset_id"], "u2") is None)
    check("删除成功", documents.delete_document(doc_id, "u1") is True)
    check("删完列表为空", documents.list_documents("u1") == [])

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'全部通过' if not failures else '失败：' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
