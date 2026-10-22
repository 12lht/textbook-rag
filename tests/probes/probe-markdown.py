"""探针：Markdown 中间态。

要证明三件事，少一件这个重构就白做：

1. **往返无损**：`render(markdown_elements(md))` 要还原出一模一样的 md。
   做不到就说明有信息只在内存的元素流里存在过，切分仍然依赖解析器。
2. **重切不重解析**：改切分参数 → `rechunk()` → markdown 字节不变。
   这是「解耦」唯一的硬证据。
3. **表格真的成了表格**：不是一排 `a | b | c` 的纯文本行，而是带分隔线、
   列头对得上的 markdown 表格。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pymupdf

import documents

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="probe-md-"))
    documents.DB_PATH = tmp / "documents.sqlite3"
    documents.UPLOAD_DIR = tmp / "uploads"
    documents.ASSET_DIR = tmp / "assets"

    src = pymupdf.open("C:/Users/13526/Desktop/Hello-Algo.pdf")
    out = pymupdf.open()
    out.insert_pdf(src, from_page=30, to_page=59)
    sample = tmp / "sample.pdf"
    out.save(sample)
    out.close()
    src.close()

    record = documents.create_upload("u1", "sample.pdf", sample.read_bytes())
    doc_id = record["doc_id"]
    parsed = documents.parse_document(doc_id)
    check("解析成功", parsed["status"] == "ready", str(parsed["error"]))

    markdown = documents.markdown_of(doc_id, "u1")
    check("markdown 落库了", bool(markdown), f"{len(markdown or '')} 字符")
    check("有页码锚点", "<!-- page:1 -->" in (markdown or ""))
    check("有章节锚点", "<!-- chapter:" in (markdown or ""))
    check("有图片引用", "](as_" in (markdown or ""))

    elements = documents.markdown_elements(markdown)
    roundtrip = documents.render_markdown(elements)
    check(
        "往返无损（render → parse → render 完全一致）",
        roundtrip.strip() == markdown.strip(),
        f"{len(roundtrip)} vs {len(markdown)}",
    )
    if roundtrip.strip() != markdown.strip():
        for index, (a, b) in enumerate(zip(roundtrip.splitlines(), markdown.splitlines())):
            if a != b:
                print(f"  第 {index + 1} 行不同：\n    render   : {a[:90]}\n    original : {b[:90]}")
                break

    tables = [c for c in documents.chunks_of(doc_id) if c["kind"] == "table"]
    print(f"\n表格 chunk {len(tables)} 条")
    for chunk in tables:
        head = chunk["content"].splitlines()
        print(f"  p{chunk['page_start']} {len(head)} 行 :: {head[0][:70]}")
    check("至少切出一条表格", len(tables) >= 1, str(len(tables)))
    if tables:
        check("表格有分隔线（真的是 markdown 表格）", "|---|" in tables[0]["content"])
        check(
            "表格内容完整（迭代/递归对比表）",
            any("循环结构" in c["content"] and "|" in c["content"] for c in tables),
        )

    before_markdown = documents.markdown_of(doc_id, "u1")
    before_chunks = len(documents.chunks_of(doc_id))
    documents.TARGET_CHARS = 200
    documents.MAX_CHARS = 320
    result = documents.rechunk(doc_id)
    after_markdown = documents.markdown_of(doc_id, "u1")
    after_chunks = len(documents.chunks_of(doc_id))
    print(f"\n重切：TARGET 500 → 200，chunk {before_chunks} → {after_chunks}")
    check("重切后 chunk 变多", after_chunks > before_chunks, f"{before_chunks} → {after_chunks}")
    check("重切没有动 markdown（说明没重新解析）", after_markdown == before_markdown)
    check("重切返回的条数一致", result["chunk_count"] == after_chunks, str(result["chunk_count"]))
    check("重切后仍能引用图（asset 没被清）", any(c["asset_id"] for c in documents.chunks_of(doc_id)))

    documents.TARGET_CHARS = 500
    documents.MAX_CHARS = 900
    documents.rechunk(doc_id)
    check("参数还原后 chunk 数回到原值", len(documents.chunks_of(doc_id)) == before_chunks)

    print("\n--- markdown 片段（第 1 页开头）---")
    print("\n".join((markdown or "").splitlines()[:14]))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'全部通过' if not failures else '失败：' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
