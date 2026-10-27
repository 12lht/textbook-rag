"""裸 LaTeX 包裹（`documents.wrap_inline_math`）的回归探针，全离线。

重点守三个「会把正文改坏」的场景：
1. **表格行整行不动**——`|` 既是列分隔又是 `\\left| x \\right|` 的一部分，包错就把表格毁了；
2. 代码块 / `$$..$$` / 已有 `$..$` / 图片引用都不许动；
3. 正常正文里的裸公式要包成 `$..$`。

跑法：python tests/probes/probe-inline-math.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import documents  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def wrap(markdown: str) -> tuple[str, int]:
    return documents.wrap_inline_math(markdown)


def main() -> int:
    print("== 1. 表格首格是公式：整行必须原样不动 ==\n")
    cases = [
        "| \\left\\| x \\right\\| | 范数 |",
        "|   \\frac{a}{b} | 说明 |",
        "|\t\\sqrt{x} | 说明 |",
        "| \\left| \\frac{1}{2} \\right| | 一半 |",
    ]
    for row in cases:
        out, wrapped = wrap(row)
        check(f"不动：{row}", out == row and wrapped == 0, f"→ {out!r}（包了 {wrapped} 处）")

    print("\n== 2. 表格整块（含表头分隔行）==\n")
    table = "\n".join([
        "| \\left\\| x \\right\\| | 范数 |",
        "|---|---|",
        "| \\|x\\| | 例子 \\alpha |",
    ])
    out, wrapped = wrap(table)
    check("整表原样返回", out == table and wrapped == 0, f"包了 {wrapped} 处")

    print("\n== 3. 正文里的裸公式照旧要包 ==\n")
    text = "若 $A$ 满足 \\frac{z}{z-1} = 1 + i，则 z 的值是…"
    out, wrapped = wrap(text)
    check("裸公式被包成 $..$", wrapped == 1 and "$\\frac{z}{z-1} = 1 + i$" in out, out)
    check("已有的 $A$ 没被重复包", "$$A$$" not in out and "$A$" in out, out)

    print("\n== 4. 保护区域不许动 ==\n")
    fenced = "```python\nx = \\frac{1}{2}  # 代码里的反斜杠\n```"
    out, wrapped = wrap(fenced)
    check("代码围栏不动", out == fenced and wrapped == 0, f"包了 {wrapped} 处")

    image = "![图 1-2 \\alpha 示意](as_1234abcd.png)"
    out, wrapped = wrap(image)
    check("图片引用不动", out == image and wrapped == 0, f"包了 {wrapped} 处")

    block = "$$\\frac{a}{b}$$"
    out, wrapped = wrap(block)
    check("行间公式不动", out == block and wrapped == 0, f"包了 {wrapped} 处")

    print("\n== 5. 表格行与正文混排 ==\n")
    mixed = "\n".join([
        "下面给出 \\alpha 的定义：",
        "| \\left\\| v \\right\\| | 向量范数 |",
        "其中 \\beta 是常数。",
    ])
    out, wrapped = wrap(mixed)
    lines = out.splitlines()
    check("表格行没被包", lines[1] == "| \\left\\| v \\right\\| | 向量范数 |", lines[1])
    check("表格上下的正文都被包了", wrapped == 2 and "$\\alpha$" in out and "$\\beta$" in out,
          f"包了 {wrapped} 处")

    print()
    if failures:
        print(f"{len(failures)} 项失败：" + "、".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
