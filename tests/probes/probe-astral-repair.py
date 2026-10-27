"""星平面字符回填探针：MinerU 把 𝑂/𝑛 这类字符吐成 U+FFFD，我们从 PDF 文字层按 bbox 取回。

跑法（仓库根目录）：
    python tests/probes/probe-astral-repair.py

全离线、不烧额度：直接拿 PDF 里的真字符造出「带 U+FFFD 的文本」，再断言 `_astral_repair`
能填回原字符并做 NFKC 归一化。

样本 PDF 走 `tests/probes/_sample.py`（textbook/ 已移出仓库，现在从 data/uploads 取）。
**找不到样本时报 FAIL**，不再 `exit(0)` 假装通过。
"""
from __future__ import annotations

import sys
import tempfile
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf  # noqa: E402

import documents  # noqa: E402
from _sample import find_sample  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def _first_astral_line(pdf):
    """找一条含星平面字符（U+1D400 起的数学字母）的行，返回 (页, 行文字, 该行 bbox 归一化)。

    **不要在函数里 `pdf.close()`**：页对象随之失效，`get_text(clip=...)` 会静默返回空，
    回填就变成了「什么都不做但也不报错」。
    """
    for page in pdf:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(span["text"] for span in line["spans"])
                if any(ord(ch) > 0xFFFF for ch in text):
                    x0, y0, x1, y1 = line["bbox"]
                    width, height = page.rect.width, page.rect.height
                    bbox = [x0 / width * 1000, y0 / height * 1000,
                            x1 / width * 1000, y1 / height * 1000]
                    return page, text, bbox
    return None, None, None


def _check_block_types_repaired() -> None:
    """表格 / 行间公式块也要走回填（以前只接了正文块，表格里的替换符留了下来）。"""
    payload = {
        "page_count": 1,
        "images_dir": Path(tempfile.mkdtemp(prefix="astral-")),
        "blocks": [
            {
                "type": "table",
                "page_idx": 0,
                "bbox": [10, 10, 900, 300],
                "content": {
                    "html": "<table><tr><td>\ufffd(\ufffd log \ufffd)</td><td>32</td></tr></table>",
                    "table_caption": [],
                },
            },
            {
                "type": "equation_interline",
                "page_idx": 0,
                "bbox": [10, 320, 900, 400],
                "content": {"math_content": "\ufffd(\ufffd)"},
            },
        ],
    }

    def repair(_page_index: int, _bbox, text: str) -> str:
        return text.replace("\ufffd", "Z")  # 只验接线：替换符确实被问到、被写回

    parsed = documents._elements_from_mineru(payload, Path(tempfile.mkdtemp(prefix="astral-assets-")),
                                             repair=repair)
    table = next((e for e in parsed.elements if e.kind == "table"), None)
    formula = next((e for e in parsed.elements if e.kind == "formula"), None)
    check("表格块走了回填", table is not None and "\ufffd" not in table.content,
          (table.content if table else "没有 table 元素")[:40])
    check("行间公式块走了回填", formula is not None and "\ufffd" not in formula.content,
          (formula.content if formula else "没有 formula 元素")[:40])


def main() -> None:
    source = find_sample("Hello-Algo")
    if source is None:
        print("FAIL  找不到样本 PDF（textbook/ 已移出仓库，库里也没有 Hello-Algo）")
        print("\n失败：找不到样本")
        sys.exit(1)

    pdf = pymupdf.open(source)
    page, original, bbox = _first_astral_line(pdf)
    if page is None:
        print(f"FAIL  {source.name} 里没找到星平面字符，无法验证回填")
        sys.exit(1)

    print(f"样本来自 {source}")
    print(f"  原文: {original!r}")

    # 1. 把星平面字符换成 U+FFFD，模拟 MinerU 的输出
    broken = "".join("\ufffd" if ord(ch) > 0xFFFF else ch for ch in original)
    check("构造出含 U+FFFD 的文本", "\ufffd" in broken, repr(broken[:30]))

    # 2. 回填
    repaired = documents._astral_repair(broken, page, bbox)
    check("U+FFFD 已填回", "\ufffd" not in repaired, repr(repaired[:30]))

    # 3. 回填结果是原文的 NFKC 归一化形态（𝑂 → O、𝑛 → n），其余字符逐字不变
    #    只对星平面字符做 NFKC：整行 NFKC 会顺手把「，」变「,」，那不是回填的责任
    expected = "".join(
        unicodedata.normalize("NFKC", ch) if ord(ch) > 0xFFFF else ch for ch in original
    )
    check("回填内容与原文一致（星平面字符 NFKC 后）", repaired == expected,
          f"{repaired!r} vs {expected!r}")
    check("数学斜体字母已归一化成 ASCII", all(ord(ch) <= 0xFFFF for ch in repaired))

    # 4. 没有 bbox / 页面为空时不能崩，也不能乱改
    check("无 bbox 时原样返回", documents._astral_repair(broken, page, None) == broken)
    check("文本里没有 U+FFFD 时不改动", documents._astral_repair(original, page, bbox) == original)

    # 5. 表格 / 公式块也覆盖
    _check_block_types_repaired()

    pdf.close()

    print()
    print("全部通过" if not failures else "失败：" + "、".join(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
