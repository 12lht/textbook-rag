"""星平面字符回填：MinerU 把 𝑂/𝑛 这类字符吐成 U+FFFD，我们从 PDF 文字层按 bbox 取回。

全离线、不烧额度：直接拿 PDF 里的真字符造出「带 U+FFFD 的文本」，再断言
`_astral_repair` 能填回原字符并做 NFKC 归一化。

原 `tests/probes/probe-astral-repair.py`，2026-11-03 迁到 pytest。
"""
from __future__ import annotations

import unicodedata

import pymupdf
import pytest

import documents


def _first_astral_line(pdf):
    """找一条含星平面字符（U+1D400 起的数学字母）的行，返回 (页, 行文字, bbox 归一化)。

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
                    bbox = [
                        x0 / width * 1000, y0 / height * 1000,
                        x1 / width * 1000, y1 / height * 1000,
                    ]
                    return page, text, bbox
    return None, None, None


@pytest.fixture
def astral_line(find_sample):
    """(页对象, 原文字, bbox)，取自样本 PDF 里第一条含星平面字符的行。"""
    source = find_sample("Hello-Algo")
    assert source is not None, "找不到样本 PDF（textbook/ 已移出仓库，库里也没有 Hello-Algo）"

    pdf = pymupdf.open(source)
    page, original, bbox = _first_astral_line(pdf)
    assert page is not None, f"{source.name} 里没找到星平面字符，无法验证回填"
    yield page, original, bbox
    pdf.close()


@pytest.mark.requires_sample
def test_repairs_replacement_chars_from_text_layer(astral_line):
    page, original, bbox = astral_line
    broken = "".join("\ufffd" if ord(ch) > 0xFFFF else ch for ch in original)
    assert "\ufffd" in broken, f"构造失败：{broken[:30]!r}"

    repaired = documents._astral_repair(broken, page, bbox)
    assert "\ufffd" not in repaired, f"没填回：{repaired[:30]!r}"
    assert all(ord(ch) <= 0xFFFF for ch in repaired), "还有星平面字符没归一化"


@pytest.mark.requires_sample
def test_result_is_nfkc_of_original(astral_line):
    """回填结果是原文的 NFKC 形态（𝑂 → O、𝑛 → n），其余字符逐字不变。

    只对星平面字符做 NFKC：整行 NFKC 会顺手把「，」变「,」，那不是回填的责任。
    """
    page, original, bbox = astral_line
    broken = "".join("\ufffd" if ord(ch) > 0xFFFF else ch for ch in original)
    expected = "".join(
        unicodedata.normalize("NFKC", ch) if ord(ch) > 0xFFFF else ch for ch in original
    )
    repaired = documents._astral_repair(broken, page, bbox)
    assert repaired == expected, f"{repaired!r} != {expected!r}"


@pytest.mark.requires_sample
def test_noop_when_no_bbox_or_no_replacement_char(astral_line):
    """没有 bbox、或文本里没有 U+FFFD 时不能崩，也不能乱改。"""
    page, original, bbox = astral_line
    broken = "".join("\ufffd" if ord(ch) > 0xFFFF else ch for ch in original)

    assert documents._astral_repair(broken, page, None) == broken, "无 bbox 时改动了正文"
    assert documents._astral_repair(original, page, bbox) == original, "没有替换符时也改了"


def test_table_and_formula_blocks_are_repaired(tmp_path):
    """表格 / 行间公式块也要走回填（以前只接了正文块，表格里的替换符留了下来）。"""
    payload = {
        "page_count": 1,
        "images_dir": tmp_path / "images",
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

    parsed = documents._elements_from_mineru(payload, tmp_path / "assets", repair=repair)
    table = next((e for e in parsed.elements if e.kind == "table"), None)
    formula = next((e for e in parsed.elements if e.kind == "formula"), None)

    assert table is not None and "\ufffd" not in table.content, (
        (table.content if table else "没有 table 元素")[:40]
    )
    assert formula is not None and "\ufffd" not in formula.content, (
        (formula.content if formula else "没有 formula 元素")[:40]
    )
