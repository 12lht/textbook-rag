"""`documents.wrap_inline_math` 的回归测试：裸 LaTeX 包裹的边界。

守三个「会把正文改坏」的场景：
1. 表格行整行不动——`|` 既是列分隔又是 `\\left| x \\right|` 的一部分，包错就把表格毁了；
2. 代码块 / `$$..$$` / 已有 `$..$` / 图片引用都不许动；
3. 正常正文里的裸公式要包成 `$..$`。

原 `tests/probes/probe-inline-math.py`，2026-11-03 迁到 pytest。
"""
from __future__ import annotations

import pytest

import documents

TABLE_ROWS = [
    "| \\left\\| x \\right\\| | 范数 |",
    "|   \\frac{a}{b} | 说明 |",
    "|\t\\sqrt{x} | 说明 |",
    "| \\left| \\frac{1}{2} \\right| | 一半 |",
]

PROTECTED = {
    "代码围栏": "```python\nx = \\frac{1}{2}  # 代码里的反斜杠\n```",
    "图片引用": "![图 1-2 \\alpha 示意](as_1234abcd.png)",
    "行间公式": "$$\\frac{a}{b}$$",
}


def wrap(markdown: str) -> tuple[str, int]:
    return documents.wrap_inline_math(markdown)


@pytest.mark.parametrize("row", TABLE_ROWS)
def test_table_row_is_left_untouched(row: str) -> None:
    """表格首格是公式时，整行必须原样不动。"""
    out, wrapped = wrap(row)
    assert out == row and wrapped == 0, f"→ {out!r}（包了 {wrapped} 处）"


def test_whole_table_is_left_untouched() -> None:
    """整块表格（含表头分隔行）不该被改动。"""
    table = "\n".join([
        "| \\left\\| x \\right\\| | 范数 |",
        "|---|---|",
        "| \\|x\\| | 例子 \\alpha |",
    ])
    out, wrapped = wrap(table)
    assert out == table and wrapped == 0, f"包了 {wrapped} 处"


@pytest.mark.parametrize("kind", list(PROTECTED))
def test_protected_region_is_untouched(kind: str) -> None:
    """代码围栏、图片引用、行间公式都属于保护区域。"""
    source = PROTECTED[kind]
    out, wrapped = wrap(source)
    assert out == source and wrapped == 0, f"{kind}：→ {out!r}（包了 {wrapped} 处）"


def test_bare_formula_in_prose_gets_wrapped() -> None:
    """正文里的裸公式要包成 `$..$`，已有的 `$A$` 不能重复包。"""
    out, wrapped = wrap("若 $A$ 满足 \\frac{z}{z-1} = 1 + i，则 z 的值是…")
    assert wrapped == 1 and "$\\frac{z}{z-1} = 1 + i$" in out, out
    assert "$$A$$" not in out and "$A$" in out, out


def test_table_and_prose_mixed() -> None:
    """表格行保持不动，表格上下的正文各自被包一次。"""
    mixed = "\n".join([
        "下面给出 \\alpha 的定义：",
        "| \\left\\| v \\right\\| | 向量范数 |",
        "其中 \\beta 是常数。",
    ])
    out, wrapped = wrap(mixed)
    lines = out.splitlines()
    assert lines[1] == "| \\left\\| v \\right\\| | 向量范数 |", lines[1]
    assert wrapped == 2 and "$\\alpha$" in out and "$\\beta$" in out, f"包了 {wrapped} 处"
