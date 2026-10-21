"""解析质量评测：把「我觉得还行」变成一排可以对比的数字。

为什么单独一支脚本，不并进 `eval/run.py`：
    run.py 评的是**对话与工具选择**（要调模型、有金标集）。解析评测不看模型，
    只吃 PDF，十秒跑完、结果确定。混在一起会让「解析退化」和「模型波动」
    在同一条失败里分不清。

## 口径先说清：这里没有 ground truth

解析没有标准答案（同一本书，人抄一遍也不会和机器逐字一致）。所以这套指标分两类：

- **结构类**：完全机器可判，改动前后能直接比（页锚点覆盖、chunk 长度分布、脏段率）。
  它们的作用不是「打分」，是**在答案变差时告诉你该怀疑谁**——是解析丢了这一段，
  还是检索没找它。
- **近似类**：需要一个分母，分母是自己数出来的（图注召回就是），
  所以绝对值仅供参考，**只看趋势和突变**。

刻意复用 `documents._CAPTION_RE` / `documents._pdf_junk_lines`：评测必须和解析器
用同一把尺子。自己另写一份正则，等于用更宽松的标准给同一个东西判高分。

用法：
    python eval/parse.py --pdf C:/.../Hello-Algo.pdf --pages 31-60
    python eval/parse.py --pdf ... --tag v2 --compare eval/baselines/parse-v1.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 存储全部重定向到临时目录：评的是解析器，不是生产库。
_TMP = Path(tempfile.mkdtemp(prefix="parse-eval-"))

import documents  # noqa: E402

documents.DB_PATH = _TMP / "documents.sqlite3"
documents.UPLOAD_DIR = _TMP / "uploads"
documents.ASSET_DIR = _TMP / "assets"

BASELINE_DIR = ROOT / "eval" / "baselines"

# --- 阈值 -------------------------------------------------------------------
# 只有「过了/没过」有意义的才给阈值。近似类指标不给，免得把噪声当结论。
TARGET_SHORT_RATIO = 0.10  # <80 字残段占比；_merge_short 之后应该很低
TARGET_LONG_RATIO = 0.15  # >900 字超长段占比；超了说明断点没找到
TARGET_DIRTY_RATIO = 0.15  # 脏段占比（A6 的机器版：抽 20 段 ≤3 段，即 15%）
TARGET_CAPTION_RECALL = 0.70  # A7 的机器版：抽 10 张对得上 ≥7
TARGET_PAGE_COVER = 0.95  # 有正文的页应该都有页码锚点
TARGET_TEXT_LAYER = 0.90  # 低于 90% 有文字层的页 = 这本书基本是扫描版

# 长度和脏段**只对 `kind='text'` 算**。第一版把 figure / table 也算进去，
# 跑出来「残段 32%、脏段 19%」，看着像解析出了问题——但脏段样例全是
# 「图 2‑5 尾递归过程」这种 8 个字的图注。
# 图注本来就只有一句话（它和截图合成一条 figure chunk，图注就是 content），
# 用「段落该有多长」去要求它，是尺子错了，不是东西坏了。

# 脏段规则（保守）。这里的每一条都对应一次真实踩坑，不是凭空想的：
#   - `Col\d`：pdfplumber 式的自动列名，出现在表格清洗没兜住的单元格里
#   - 以标点开头：被切断的句子尾巴
#   - 太短又不收尾：残段（正常段落不会 20 字就断）
DIRTY_RULES = [
    ("auto_col", re.compile(r"\bCol\d+\b")),
    ("punct_start", re.compile(r"^\s*[，。、；：,.;:）)】」』]")),
]


def dirty_reasons(text: str) -> list[str]:
    reasons = [name for name, rule in DIRTY_RULES if rule.search(text)]
    stripped = text.strip()
    if len(stripped) < 20 and stripped and stripped[-1] not in "。！？；：.!?;:）)":
        reasons.append("stub")
    if "\n\n\n" in text:
        reasons.append("blank_run")
    return reasons


def page_text_lines(pdf, page_index: int) -> list[str]:
    return [line.strip() for line in pdf[page_index].get_text("text").splitlines() if line.strip()]


def looks_like_caption(line: str) -> bool:
    """和解析器用同一把尺子（`documents._CAPTION_RE` 就是它的判据）。

    额外两个过滤也照抄解析器的思路：正文里的「图 2-7 展示了……」不是题注。
    """
    if len(line) > 120 or line.endswith("。"):
        return False
    return bool(documents._CAPTION_RE.match(line))


def evaluate(pdf_path: Path, pages: tuple[int, int] | None) -> dict[str, object]:
    pymupdf = documents._require("pymupdf", "pip install pymupdf")
    src = pymupdf.open(pdf_path)
    if pages is None:
        start, end = 0, src.page_count
    else:
        start, end = pages[0] - 1, min(pages[1], src.page_count)

    subset = pymupdf.open()
    subset.insert_pdf(src, from_page=start, to_page=end - 1)
    # **书签必须跟着复制**。`insert_pdf` 只搬页面、不搬目录，第一版漏了这步，
    # 结果 Hello-Agents（原书 449 条书签）被评成「目录 0 条」，等于逼它走
    # 「靠编号猜章节」的降级路径——用降级路径的结果去评判解析器，是评测在骗人。
    #
    # 还有一层坑：`set_toc` 要求**第一条是 level 1**。切出来的子集第一条往往是
    # 2.3.3 这种三级标题，直接塞进去会抛 `hierarchy level of item 0 must be 1`，
    # 而它抛在一个 try 里，于是静默失效——第一次修完还是「目录 0 条」。
    # 所以这里把层级重新归一化，并且**把成败记进报告**，失败不许悄悄发生。
    toc_copied = 0
    try:
        bookmarks = [
            [level, title, page - start]
            for level, title, page in src.get_toc()
            if start < page <= end
        ]
        if bookmarks:
            base = min(level for level, _, _ in bookmarks)
            normalized, previous = [], 0
            for level, title, page in bookmarks:
                level = min(level - base + 1, previous + 1)  # 不许跳级
                normalized.append([level, title, page])
                previous = level
            subset.set_toc(normalized)
            toc_copied = len(normalized)
    except Exception as exc:  # noqa: BLE001 - 畸形书签不该让整次评测失败
        print(f"  ! 书签复制失败（章节识别会走降级路径，结论不可比）：{exc}")
    sample = _TMP / "sample.pdf"
    subset.save(sample)
    total_pages = subset.page_count

    # ---- 直接对 PDF 数分母（不经过解析器），这是「近似类」指标的前提 ----
    # 图和表的题注分开数：混在一起算，召回率会被「表没被当成图」直接拉低，
    # 而表本来就该进 table chunk，不进 figure。第一版就混了，18/27 看着像丢图。
    text_layer_pages = 0
    figure_captions: set[str] = set()
    table_captions: set[str] = set()
    table_pages = 0
    for page_index in range(total_pages):
        page = subset[page_index]
        lines = page_text_lines(subset, page_index)
        if lines:
            text_layer_pages += 1
        for line in lines:
            # 直接调解析器自己的题注判定（三道过滤 + 45 字上限），不自己写一份。
            # 这不是偷懒：分母宽一点，召回率就低一点——用宽松的尺子量自己，
            # 结论只会是「我很好」或「我莫名其妙很差」，两种都没用。
            caption = documents._match_caption(line)
            if caption is None:
                continue
            label, _ = caption
            bucket = table_captions if label.startswith("表") else figure_captions
            bucket.add(re.sub(r"\s+", "", line))
        try:
            if page.find_tables().tables:
                table_pages += 1
        except Exception:  # noqa: BLE001 - 畸形页面不参与统计
            pass
    # 复用解析器自己的预检，拿到「是不是幻灯片」——残段指标对幻灯片不适用：
    # 一页 PPT 只有 20 个字也是完整主题，用「段落该有多长」量它是错的尺子。
    preflight = documents._pdf_preflight(subset, pymupdf)
    subset.close()
    src.close()

    # ---- 走真实解析链路 ----
    started = time.perf_counter()
    record = documents.create_upload("eval-user", pdf_path.name, sample.read_bytes())
    doc_id = str(record["doc_id"])
    parsed = documents.parse_document(doc_id)
    elapsed = time.perf_counter() - started

    markdown = documents.markdown_of(doc_id, "eval-user") or ""
    chunks = documents.chunks_of(doc_id)

    page_anchors = {int(m.group(1)) for m in re.finditer(r"<!--\s*page:(\d+)\s*-->", markdown)}
    chapter_anchors = re.findall(r"<!--\s*chapter:(.+?)\s*-->", markdown)
    figure_lines = re.findall(r"^!\[(.*?)\]\((as_[0-9a-f]+\.png)\)", markdown, re.M)

    lengths_all = [(len(str(chunk["content"])), str(chunk["kind"])) for chunk in chunks]
    text_lengths = sorted(length for length, kind in lengths_all if kind == "text")
    lengths = text_lengths
    n = len(lengths) or 1
    # 脏段只看正文段：图注/表格短是设计如此，不是脏。
    dirty = [
        chunk for chunk in chunks
        if chunk["kind"] == "text" and dirty_reasons(str(chunk["content"]))
    ]
    dirty_base = len(text_lengths) or 1
    table_blocks = len(re.findall(r"^\|\s*-{3,}", markdown, re.M))
    fence_count = markdown.count("```")

    asset_dir = documents.ASSET_DIR / doc_id
    on_disk = {p.name for p in asset_dir.glob("*")} if asset_dir.is_dir() else set()
    referenced = {name for _, name in figure_lines}
    missing = sorted(referenced - on_disk)

    # 目录条数用**复制进子集的书签数**，不查库：`parse_document` 返回的是列表行
    # （`_row_to_doc`），本来就不带 toc 列，第一版从那里取永远拿到 None，
    # 报告里的「目录 0 条」是个假数字。
    toc_leaves = toc_copied

    return {
        "pdf": pdf_path.name,
        "toc_copied": toc_copied,
        "slides": bool(preflight.get("slides")),
        "pages": f"{start + 1}-{end}",
        "elapsed_s": round(elapsed, 1),
        "doc": {
            "page_count": parsed.get("page_count"),
            "chunks": len(chunks),
            "assets": parsed.get("asset_count"),
            "toc_entries": toc_copied,
        },
        # 结构类
        "text_layer_ratio": round(text_layer_pages / total_pages, 3),
        "page_anchor_cover": round(len(page_anchors) / total_pages, 3),
        "chunk_len": {
            "p50": lengths[len(lengths) // 2] if lengths else 0,
            "p95": lengths[int(len(lengths) * 0.95)] if lengths else 0,
            "min": lengths[0] if lengths else 0,
            "max": lengths[-1] if lengths else 0,
        },
        "short_ratio": round(sum(1 for x in lengths if x < 80) / n, 3),
        "long_ratio": round(sum(1 for x in lengths if x > 900) / n, 3),
        "dirty_ratio": round(len(dirty) / dirty_base, 3),
        "kinds": {kind: sum(1 for _, k in lengths_all if k == kind)
                  for kind in ("text", "figure", "table")},
        "dirty_examples": [
            {"chunk_id": c["chunk_id"], "reasons": dirty_reasons(str(c["content"])),
             "head": str(c["content"])[:60]}
            for c in dirty[:5]
        ],
        "chapters": len(chapter_anchors),
        "chapter_list": chapter_anchors[:12],
        "toc_leaves": toc_leaves,
        # 近似类
        "figure_captions_in_pdf": len(figure_captions),
        "table_captions_in_pdf": len(table_captions),
        "figures_in_markdown": len(figure_lines),
        "caption_recall": (
            round(len(figure_lines) / len(figure_captions), 3) if figure_captions else None
        ),
        "table_caption_recall": (
            round(table_blocks / len(table_captions), 3) if table_captions else None
        ),
        "missing_asset_files": missing,
        "table_pages_in_pdf": table_pages,
        "tables_in_markdown": table_blocks,
        "code_fences": fence_count,
        "missing_figures": sorted(
            {re.sub(r"\s+", "", c) for c in figure_captions}
            - {re.sub(r"\s+", "", alt) for alt, _ in figure_lines}
        )[:10],
        "markdown_sha": hashlib.sha256(markdown.encode()).hexdigest()[:16],
        "chunk_sha": hashlib.sha256(
            "\n".join(str(c["content"]) for c in chunks).encode()
        ).hexdigest()[:16],
    }


def judge(report: dict[str, object]) -> list[tuple[str, str, str]]:
    """(指标, 值, 判定)。只判有阈值的；近似类给「—」。"""
    rows: list[tuple[str, str, str]] = []

    def verdict(value: float, ok: bool) -> str:
        return "ok" if ok else "FAIL"

    text_layer = float(report["text_layer_ratio"])
    rows.append(("文字层覆盖", f"{text_layer:.0%}",
                 verdict(text_layer, text_layer >= TARGET_TEXT_LAYER)))
    cover = float(report["page_anchor_cover"])
    rows.append(("页码锚点覆盖", f"{cover:.0%}", verdict(cover, cover >= TARGET_PAGE_COVER)))
    short = float(report["short_ratio"])
    if report.get("slides"):
        rows.append(("残段 <80 字", f"{short:.0%}", "— 幻灯片"))
    else:
        rows.append(("残段 <80 字", f"{short:.0%}", verdict(short, short <= TARGET_SHORT_RATIO)))
    long = float(report["long_ratio"])
    rows.append(("超长 >900 字", f"{long:.0%}", verdict(long, long <= TARGET_LONG_RATIO)))
    dirty = float(report["dirty_ratio"])
    rows.append(("脏段率（A6 机器版）", f"{dirty:.0%}", verdict(dirty, dirty <= TARGET_DIRTY_RATIO)))
    recall = report["caption_recall"]
    if recall is None:
        rows.append(("图注召回（A7 机器版）", "无题注", "—"))
    else:
        rows.append(("图注召回（A7 机器版）", f"{float(recall):.0%}",
                     verdict(float(recall), float(recall) >= TARGET_CAPTION_RECALL)))
    rows.append(("坏图引用", str(len(report["missing_asset_files"])),
                 verdict(0, not report["missing_asset_files"])))
    rows.append(("代码块围栏", str(report["code_fences"]), "—"))
    return rows


def print_report(report: dict[str, object], previous: dict[str, object] | None) -> None:
    print(f"\n{report['pdf']}  第 {report['pages']} 页  "
          f"（{report['elapsed_s']}s，{report['doc']['chunks']} 段，"
          f"{report['doc']['assets']} 图）")
    print("-" * 74)
    for name, value, flag in judge(report):
        print(f"  {name:<22} {value:>10}   {flag}")
    print("-" * 74)
    length = report["chunk_len"]
    print(f"  段落长度  正文 {length['p50']} / P95 {length['p95']} / "
          f"跨度 {length['min']}~{length['max']}")
    kinds = report["kinds"]
    print(f"  段落构成  正文 {kinds['text']} / 图 {kinds['figure']} / 表 {kinds['table']}")
    print(f"  章节      {report['chapters']} 个锚点 / 目录 {report['toc_leaves']} 条")
    print(f"  表格      PDF 有表 {report['table_pages_in_pdf']} 页 → "
          f"Markdown {report['tables_in_markdown']} 张")
    print(f"  图注      PDF 图题注 {report['figure_captions_in_pdf']} 行 / "
          f"表题注 {report['table_captions_in_pdf']} 行 → "
          f"Markdown {report['figures_in_markdown']} 个图引用")
    if report["dirty_examples"]:
        print("  脏段样例：")
        for example in report["dirty_examples"]:
            print(f"    [{','.join(example['reasons'])}] {example['head']}")
    if report["missing_figures"]:
        print(f"  未配上的题注  {' | '.join(report['missing_figures'])}")
    if report["chapter_list"]:
        print(f"  章节锚点  {' | '.join(report['chapter_list'][:8])}")

    if previous:
        print("-" * 74)
        print("  与基线对比：")
        for key in ("short_ratio", "long_ratio", "dirty_ratio", "figure_captions_in_pdf",
                    "figures_in_markdown", "tables_in_markdown", "code_fences"):
            old, new = previous.get(key), report.get(key)
            if old is None or new is None or old == new:
                continue
            mark = "↑" if new > old else "↓"
            print(f"    {key:<22} {old} → {new}  {mark}")
        if previous.get("markdown_sha") == report.get("markdown_sha"):
            print("    Markdown 逐字未变")


def main() -> None:
    parser = argparse.ArgumentParser(description="解析质量评测")
    parser.add_argument("--pdf", required=True, help="待评的 PDF")
    parser.add_argument("--pages", help="页码范围，如 31-60（默认全书）")
    parser.add_argument("--tag", help="存成 eval/baselines/parse-<tag>.json")
    parser.add_argument("--compare", help="与旧报告对比")
    args = parser.parse_args()

    pages = None
    if args.pages:
        first, _, last = args.pages.partition("-")
        pages = (int(first), int(last or first))

    report = evaluate(Path(args.pdf), pages)
    previous = None
    if args.compare:
        previous = json.loads(Path(args.compare).read_text(encoding="utf-8"))
    print_report(report, previous)

    if args.tag:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        target = BASELINE_DIR / f"parse-{args.tag}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {target.relative_to(ROOT)}")

    failures = [name for name, _, flag in judge(report) if flag == "FAIL"]
    print(f"\n{'全部达标' if not failures else '未达标：' + '、'.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
