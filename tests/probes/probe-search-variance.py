"""对照实验：把 web_search 结果的长度变化拆成「MCP 包装开销」和「网页内容波动」。

同一个查询各调两次：
- 直接调 tools.py（无包装）
- 经 MCP 调（有 content-block 包装）

如果两次直接调的长度就差几倍，说明真正的变量是 Tavily 返回的实时内容，
和 MCP 无关——那 baseline-v1/v2 的 token 对比在搜索类用例上就是不可比的。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core
import tools

QUERY = "近期 AI 领域的新闻"


def repr_overhead(s: str) -> int:
    """Python repr 会把 \\n 变成 \\\\n、' 变成 \\'，多出来的字符数。"""
    return len(repr(s)) - len(s)


async def main() -> None:
    print("=== 直接调 tools.py 两次（同一查询）===")
    direct = []
    for i in (1, 2):
        out = tools.web_search(QUERY)
        direct.append(out)
        print(f"  第 {i} 次：{len(out):>7} 字符   repr 开销 {repr_overhead(out):>6}")

    print("\n=== 经 MCP 调两次（同一查询）===")
    loaded = await agent_core.load_tools()
    search_tool = next(t for t in loaded if t.name == "web_search")
    via_mcp = []
    inner_lens = []
    for i in (1, 2):
        out = await search_tool.ainvoke({"query": QUERY})
        wrapped = str(out)
        via_mcp.append(wrapped)
        inner = out[0]["text"] if isinstance(out, list) and out else ""
        inner_lens.append(len(inner))
        print(f"  第 {i} 次：包装后 {len(wrapped):>7} 字符   内层 {len(inner):>7} 字符   "
              f"包装开销 {len(wrapped) - len(inner):>5}   repr 开销 {repr_overhead(inner):>6}")

    print("\n=== 结论 ===")
    d_lo, d_hi = min(map(len, direct)), max(map(len, direct))
    m_lo, m_hi = min(map(len, via_mcp)), max(map(len, via_mcp))
    print(f"  直接调两次之间波动：{d_lo} ~ {d_hi}   （倍数 {d_hi / max(d_lo, 1):.2f}x）")
    print(f"  MCP 调两次之间波动：{m_lo} ~ {m_hi}   （倍数 {m_hi / max(m_lo, 1):.2f}x）")
    wrappers = [len(via_mcp[i]) - inner_lens[i] for i in range(len(via_mcp))]
    print(f"  MCP 包装的固定开销：{wrappers} 字符（content-block 结构 + 每次新生成的 uuid）")
    print(f"  同一次内容下包装 + repr 转义的相对膨胀："
          f"{[round(wrappers[i] / max(inner_lens[i], 1) * 100, 2) for i in range(len(wrappers))]}%")

    await agent_core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
