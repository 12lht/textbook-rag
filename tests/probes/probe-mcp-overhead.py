"""微基准：把「MCP 边界」的开销从模型行为里剥离出来。

为什么需要这个：整轮评测（eval/run.py）里 token 和端到端时延被两个不可控变量污染了——
  1. 模型同一轮里重复调用同一个工具的次数（随机）
  2. Tavily 返回的网页正文长度（随机，实测 1.11x ~ 2.49x 波动）
所以全量评测**测不出** MCP 的净开销。这里换一个确定性工具 get_current_time
（纯本地、返回长度固定、不发网络请求、不调模型），只测边界本身：

  A. 直接 Python 调用 tools.get_current_time()
  B. 经 langchain MCP 适配器调用（agent 实际走的路：ToolNode -> tool.ainvoke）
  C. 经原始 mcp session.call_tool（纯 JSON-RPC + 序列化，不含 langchain 包装）

B - A 是「站在 agent 视角看 MCP 值多少」，C - A 是「纯 IPC/序列化的下限」。

本脚本不产生任何 LLM token 消耗。
"""

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agent_core  # noqa: E402
import tools  # noqa: E402

N = 30


def _stats(samples: list[float]) -> str:
    samples = sorted(samples)
    return (
        f"中位 {statistics.median(samples):7.3f} ms   "
        f"均值 {statistics.mean(samples):7.3f} ms   "
        f"P95 {samples[int(len(samples) * 0.95) - 1]:7.3f} ms   "
        f"最小 {samples[0]:7.3f} ms"
    )


async def main() -> None:
    print(f"每个路径采样 {N} 次，工具 = get_current_time（确定性输出）\n")

    for _ in range(3):  # 预热
        tools.get_current_time()
    direct: list[float] = []
    for _ in range(N):
        start = time.perf_counter()
        tools.get_current_time()
        direct.append((time.perf_counter() - start) * 1000)

    load_start = time.perf_counter()
    mcp_tools = await agent_core.load_tools()
    cold = (time.perf_counter() - load_start) * 1000
    by_name = {t.name: t for t in mcp_tools}

    for _ in range(3):
        await by_name["get_current_time"].ainvoke({})
    via_adapter: list[float] = []
    for _ in range(N):
        start = time.perf_counter()
        await by_name["get_current_time"].ainvoke({})
        via_adapter.append((time.perf_counter() - start) * 1000)

    raw: list[float] = []
    async with agent_core._mcp_client.session(agent_core.MCP_SERVER_NAME) as session:
        for _ in range(3):
            await session.call_tool("get_current_time", {})
        for _ in range(N):
            start = time.perf_counter()
            await session.call_tool("get_current_time", {})
            raw.append((time.perf_counter() - start) * 1000)

    print(f"A 直接调用        {_stats(direct)}")
    print(f"B 适配器 -> MCP   {_stats(via_adapter)}")
    print(f"C 原始 session    {_stats(raw)}")
    print()
    print(f"MCP 子进程冷启动（仅首次）：{cold:.1f} ms")
    print()
    print("=== 结论 ===")
    d = statistics.median(direct)
    b = statistics.median(via_adapter)
    c = statistics.median(raw)
    print(f"站在 agent 视角，MCP 一跳净开销（B-A）：{b - d:.3f} ms")
    print(f"纯 JSON-RPC + 序列化下限（C-A）：        {c - d:.3f} ms")
    print(f"langchain 包装额外成本（B-C）：           {b - c:.3f} ms")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(agent_core.shutdown())
