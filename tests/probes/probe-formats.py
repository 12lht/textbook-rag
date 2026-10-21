"""探针：三种格式（PDF / PPTX / DOCX）都能走完摄取，且坏文件不会把状态挂死。

PDF 路径在 `probe-documents.py` 里用真教材测过了，这里补 PPTX / DOCX 两条，
外加几个「必须失败得明白」的用例——A1 的验收标准是「三种格式都能传成功、
失败有原因提示」，后半句同样要测。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import documents

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def build_pptx(path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    for index, (title, body) in enumerate([
        ("第三章 抽样定理", "低通抽样定理：对带限信号以不低于两倍最高频率的速率抽样，可以无失真恢复原信号。"),
        ("3.2 量化", "量化是把连续幅度离散化的过程。量化误差不超过半个量化台阶。"),
        ("3.3 编码", "PCM 编码把量化后的电平映射为二进制码字，常用折叠二进制码。"),
    ], start=1):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        if index == 1:
            # 造一张纯色 PNG 当插图，验证图片路径
            from PIL import Image  # 可能没装，退化则跳过插图
            buffer = path.with_suffix(".tmp.png")
            Image.new("RGB", (320, 200), (220, 235, 225)).save(buffer)
            slide.shapes.add_picture(str(buffer), Inches(1), Inches(4), width=Inches(3))
            buffer.unlink(missing_ok=True)
    prs.save(path)


def build_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("第 2 章 随机过程", level=1)
    doc.add_paragraph(
        "随机过程是一族依赖于某个参数（通常是时间）的随机变量。"
        "如果对任意有限个时刻取样的联合分布都是高斯的，就称它为高斯过程。"
    )
    doc.add_heading("2.1 平稳性", level=2)
    doc.add_paragraph(
        "严平稳要求任意有限维分布对时间平移不变；"
        "广义平稳只要求均值为常数、自相关函数只与时间差有关。"
    )
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "类型"
    table.cell(0, 1).text = "条件"
    table.cell(1, 0).text = "严平稳"
    table.cell(1, 1).text = "有限维分布平移不变"
    doc.save(path)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="probe-formats-"))
    documents.DB_PATH = tmp / "documents.sqlite3"
    documents.UPLOAD_DIR = tmp / "uploads"
    documents.ASSET_DIR = tmp / "assets"

    # --- PPTX ---
    pptx_path = tmp / "chapter3.pptx"
    try:
        build_pptx(pptx_path)
    except ImportError as exc:
        print(f"SKIP  PPTX 样本生成（缺依赖：{exc}）")
    else:
        record = documents.create_upload("u1", "chapter3.pptx", pptx_path.read_bytes())
        parsed = documents.parse_document(record["doc_id"])
        check("PPTX 解析成功", parsed["status"] == "ready", str(parsed["error"]))
        check("PPTX 页数 = 幻灯片数", parsed["page_count"] == 3, str(parsed["page_count"]))
        chunks = documents.chunks_of(record["doc_id"])
        check("PPTX 切出段落", len(chunks) >= 3, str(len(chunks)))
        check("PPTX 段落含正文", any("低通抽样定理" in c["content"] for c in chunks))
        check(
            "PPTX 一张幻灯片一条 chunk",
            all(c["page_start"] == c["page_end"] for c in chunks if c["kind"] == "text"),
        )
        check("PPTX 章节取幻灯片标题", any(c["chapter"] == "第三章 抽样定理" for c in chunks))

    # --- DOCX ---
    docx_path = tmp / "chapter2.docx"
    try:
        build_docx(docx_path)
    except ImportError as exc:
        print(f"SKIP  DOCX 样本生成（缺依赖：{exc}）")
    else:
        record = documents.create_upload("u1", "chapter2.docx", docx_path.read_bytes())
        parsed = documents.parse_document(record["doc_id"])
        check("DOCX 解析成功", parsed["status"] == "ready", str(parsed["error"]))
        chunks = documents.chunks_of(record["doc_id"])
        joined = "\n".join(c["content"] for c in chunks)
        check("DOCX 正文完整", "高斯过程" in joined)
        check("DOCX 表格转成文本", "严平稳 | 有限维分布平移不变" in joined, joined[-60:])
        check("DOCX 页数记为 0（Word 拿不到真实页数）", parsed["page_count"] == 0, str(parsed["page_count"]))
        check("DOCX 认出了章标题", any(c["chapter"] == "第 2 章 随机过程" for c in chunks))

    # --- 失败路径要有明确原因 ---
    try:
        documents.create_upload("u1", "note.txt", b"hello")
        check("不支持的类型被拒", False, "居然收下了")
    except documents.DocumentError as exc:
        check("不支持的类型被拒", "不支持" in str(exc), str(exc))

    try:
        documents.create_upload("u1", "empty.pdf", b"")
        check("空文件被拒", False, "居然收下了")
    except documents.DocumentError as exc:
        check("空文件被拒", "空" in str(exc), str(exc))

    # 伪造一个坏 PDF：后缀对、内容是垃圾
    broken = documents.create_upload("u1", "broken.pdf", b"%PDF-1.4 not really a pdf")
    broken_parsed = documents.parse_document(broken["doc_id"])
    check("坏 PDF 落成 failed 而不是抛异常", broken_parsed["status"] == "failed", broken_parsed["status"])
    check("坏 PDF 有原因", bool(broken_parsed["error"]), str(broken_parsed["error"])[:60])

    # --- 崩溃恢复 ---
    zombie = documents.create_upload("u1", "zombie.pptx", pptx_path.read_bytes() if pptx_path.exists() else b"x")
    with documents._connect() as conn:
        conn.execute(
            "UPDATE documents SET status = ?, updated_at = ? WHERE doc_id = ?",
            (documents.STATUS_PARSING, "2020-01-01T00:00:00", zombie["doc_id"]),
        )
        conn.commit()
    fixed = documents.reconcile_pending()
    with documents._connect() as conn:
        row = conn.execute(
            "SELECT status, error FROM documents WHERE doc_id = ?", (zombie["doc_id"],)
        ).fetchone()
    check("僵尸 parsing 被置为 failed", row["status"] == "failed", f"{fixed} 条 / {row['status']}")
    check("僵尸失败了有原因", bool(row["error"]), str(row["error"]))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'全部通过' if not failures else '失败：' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
