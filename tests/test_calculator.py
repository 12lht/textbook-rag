"""calculator 工具：求值正确性 + 安全拒绝 + 一条真实 MCP 链路。

纯函数测试直接调 `tools.calculator`，零依赖零网络，是回归的主力。
MCP 链路测试走 `agent_core.load_tools()`，验证「tools.py → ALL_TOOLS →
mcp_server 自动注册 → agent 能调到」这条真实路径没断——注册是静态的，
光看代码不保险，得真连一次。MCP 会话和事件循环强绑定（Windows 上尤甚），
沿用 test_cassette 的做法：单测试函数内 asyncio.run，finally 里 shutdown。
"""
from __future__ import annotations

import asyncio

import pytest

import agent_core
import tools

# ---------------------------------------------------------------------------
# 纯函数层：直接调 tools.calculator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # 四则与优先级
        ("1 + 2 * 3", "7"),
        ("(1 + 2) * 3", "9"),
        ("7 / 2", "3.5"),
        ("7 // 2", "3"),
        ("7 % 3", "1"),
        # 乘方：** 与 ^ 两种写法都要通（^ 会被预处理成 **，防止异或静默错算）
        ("2 ** 10", "1024"),
        ("2 ^ 10", "1024"),
        # 负数（UnaryOp 分支）
        ("-5 + 3", "-2"),
        ("-(-4)", "4"),
        # 浮点毛刺清理：这些是「模型幻觉计算错误」的常见源头
        ("0.1 + 0.2", "0.3"),
        ("sqrt(2) ** 2", "2"),
        # 函数与常量
        ("sqrt(2)", "1.4142135623731"),
        ("log(1000, 10)", "3"),
        ("ln(e)", "1"),
        ("factorial(5)", "120"),
        ("round(pi, 4)", "3.1416"),
        ("abs(-3.5)", "3.5"),
        ("gcd(12, 18)", "6"),
        ("hypot(3, 4)", "5"),
        ("sin(pi / 6)", "0.5"),
    ],
)
def test_calculator_valid(expression: str, expected: str) -> None:
    assert tools.calculator(expression) == expected


@pytest.mark.parametrize(
    ("expression", "message_part"),
    [
        ("1 / 0", "除数"),
        ("9 % 0", "除数"),
        ("sqrt(-1)", "定义域"),
        ("log(0)", "定义域"),
        ("abc + 1", "不认识的名字"),
        ("", "请提供一个数学表达式"),
        ("   ", "请提供一个数学表达式"),
        ("9 ** 9 ** 9", "指数太大"),
        ("factorial(200000)", "阶乘的参数太大"),
        ("x" * 3000, "表达式太长"),
        # 注入拒绝：这些在 eval 版计算器里全是任意代码执行
        ("__import__('os').system('echo hi')", "只支持计算器内置的数学函数"),
        # @ 是合法的矩阵乘运算符，能过解析，在二元运算白名单处被拒
        ("1 @ 2", "不支持的运算符"),
        ("().__class__.__bases__[0].__subclasses__()", "只支持计算器内置的数学函数"),
        ("open('/etc/passwd')", "只支持计算器内置的数学函数"),
        ("(lambda x: x)(2)", "只支持计算器内置的数学函数"),
        # 列表推导式能通过解析，在求值器的最终兜底 raise 处被拒
        ("[x for x in range(3)]", "不支持的"),
        ("'a' * 100", "只支持数字"),
        ("True + 1", "只支持数字"),
        # (-8) ** 0.5 在 Python 里返回复数，必须报错而不是把复数字符串交出去
        ("(-8) ** 0.5", "不是实数"),
    ],
)
def test_calculator_rejects(expression: str, message_part: str) -> None:
    result = tools.calculator(expression)
    assert message_part in result, f"{expression!r} -> {result!r}"


def test_calculator_no_silent_none() -> None:
    """任何输入都不能返回 None 或空串——静默假结果对 LLM 工具是毒药。"""
    for expression in ["", "abc", "__import__('os')", "sqrt(2) ** 2", "!!", "factorial(3)"]:
        result = tools.calculator(expression)
        assert isinstance(result, str) and result.strip(), f"{expression!r} -> {result!r}"


# ---------------------------------------------------------------------------
# MCP 链路层：tools.py 的注册要真能被 agent 调到
# ---------------------------------------------------------------------------


def test_calculator_reachable_through_mcp() -> None:
    try:
        asyncio.run(_invoke_via_mcp())
    finally:
        try:
            asyncio.run(agent_core.shutdown())
        except Exception:  # noqa: BLE001 — 清理失败不该盖住断言失败
            pass


async def _invoke_via_mcp() -> None:
    mcp_tools = {tool.name: tool for tool in await agent_core.load_tools()}
    assert "calculator" in mcp_tools, "MCP server 没把 calculator 注册出来"
    result = await mcp_tools["calculator"].ainvoke({"expression": "(1 + 2) ** 4"})
    if isinstance(result, list):
        # MCP 适配器把字符串包成 content-block 列表，剥掉包装
        result = "".join(
            block.get("text", "")
            for block in result
            if isinstance(block, dict) and block.get("type") == "text"
        )
    assert "81" in str(result), f"MCP 链路算错了：(1 + 2) ** 4 -> {result!r}"
