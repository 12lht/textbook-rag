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

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")  # MinerU 的 key 在 .env 里

import pymupdf  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import documents
from documents import db as documents_db  # noqa: E402
from _sample import sample_pdf  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="probe-md-"))
    documents_db.DB_PATH = tmp / "documents.sqlite3"
    documents_db.UPLOAD_DIR = tmp / "uploads"
    documents_db.ASSET_DIR = tmp / "assets"

    src = pymupdf.open(str(sample_pdf("Hello-Algo")))
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
        # 行数不等本身就是这里要诊断的情况之一，所以必须用非 strict 的 zip：
        # 加 strict 会在行数不一致时直接抛错，反而看不到具体是哪几行不同。
        for index, (a, b) in enumerate(zip(roundtrip.splitlines(), markdown.splitlines())):  # noqa: B905
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
    # **存原值再还原**：以前这里写死还原成 500/900，而模块默认早就是 250/900，
    # 还原后参数不等于原参数，chunk 数当然回不去（探针自己过时了，不是代码错）。
    saved_target, saved_max = documents.core.TARGET_CHARS, documents.core.MAX_CHARS
    documents.core.TARGET_CHARS = 200
    documents.core.MAX_CHARS = 320
    result = documents.rechunk(doc_id)
    after_markdown = documents.markdown_of(doc_id, "u1")
    after_chunks = len(documents.chunks_of(doc_id))
    print(f"\n重切：TARGET {saved_target} → 200，chunk {before_chunks} → {after_chunks}")
    check("重切后 chunk 变多", after_chunks > before_chunks, f"{before_chunks} → {after_chunks}")
    check("重切没有动 markdown（说明没重新解析）", after_markdown == before_markdown)
    check("重切返回的条数一致", result["chunk_count"] == after_chunks, str(result["chunk_count"]))
    check("重切后仍能引用图（asset 没被清）", any(c["asset_id"] for c in documents.chunks_of(doc_id)))

    documents.core.TARGET_CHARS, documents.core.MAX_CHARS = saved_target, saved_max
    documents.rechunk(doc_id)
    check("参数还原后 chunk 数回到原值", len(documents.chunks_of(doc_id)) == before_chunks)

    print("\n--- markdown 片段（第 1 页开头）---")
    print("\n".join((markdown or "").splitlines()[:14]))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'全部通过' if not failures else '失败：' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
