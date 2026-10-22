"""追查 baseline-v2 的 token 翻倍：MCP 是否把工具结果包装变长了？

如果工具返回的字符串经 MCP 回来后变长（被包成 content-block 结构或加了元数据），
那么每个回合里后续每次模型调用都要重发这段更长内容，prompt token 会被放大。

这里只直接调工具（不经过模型），所以零 API 成本。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core
import tools


async def main() -> None:
    print("=== 直接调用 tools.py（改造前的原始返回）===")
    direct_time = tools.get_current_time()
    direct_loc = tools.get_user_location()
    print(f"  get_current_time  {len(direct_time):>6} 字符  {direct_time!r}")
    print(f"  get_user_location {len(direct_loc):>6} 字符  {direct_loc!r}")

    print("\n=== 经 MCP 调用（Agent 实际拿到的 ToolMessage 内容）===")
    loaded = await agent_core.load_tools()
    by_name = {t.name: t for t in loaded}
    mcp_time = await by_name["get_current_time"].ainvoke({})
    mcp_loc = await by_name["get_user_location"].ainvoke({})
    print(f"  get_current_time  {len(mcp_time):>6} 字符  type={type(mcp_time).__name__}")
    print(f"    {mcp_time!r}")
    print(f"  get_user_location {len(mcp_loc):>6} 字符  type={type(mcp_loc).__name__}")
    print(f"    {mcp_loc!r}")

    print("\n=== 放大倍数 ===")
    print(f"  get_current_time   {len(direct_time)} → {len(mcp_time)}   x{len(mcp_time)/max(len(direct_time),1):.2f}")
    print(f"  get_user_location  {len(direct_loc)} → {len(mcp_loc)}   x{len(mcp_loc)/max(len(direct_loc),1):.2f}")

    print("\n=== web_search（长 JSON，成本较高，只跑一次）===")
    direct_search = tools.web_search("LangGraph 是什么")
    mcp_search = await by_name["web_search"].ainvoke({"query": "LangGraph 是什么"})
    print(f"  tools.py  {len(direct_search):>6} 字符")
    print(f"  MCP       {len(mcp_search):>6} 字符   x{len(mcp_search)/max(len(direct_search),1):.2f}")
    print(f"  MCP 返回开头 120 字符：{str(mcp_search)[:120]!r}")

    await agent_core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
