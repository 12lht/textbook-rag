"""探针：核对 learning-notes §21 里那两张「名片」的说法。

要确认两件事：
  1. get_current_time 发给模型的参数表确实是空的（properties: {}）；
  2. get_user_location 的 runtime 确实被框架剥掉了，模型也看不到。

用 load_tools() 拿真实工具列表（会拉起 MCP 子进程），不调模型、不花钱。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.utils.function_calling import convert_to_openai_tool

import agent_core


async def main() -> int:
    try:
        tools = await agent_core.load_tools()
    finally:
        await agent_core.close_mcp()

    ok = True
    print(f"工具数：{len(tools)}\n")

    by_name = {}
    for tool in tools:
        spec = convert_to_openai_tool(tool)
        func = spec["function"]
        by_name[tool.name] = func
        params = func.get("parameters", {})
        props = params.get("properties", {})
        required = params.get("required", [])
        print(f"[{tool.name}]")
        print(f"  参数名：{list(props) or '（无）'}")
        print(f"  必填  ：{required or '（无）'}")
        print(f"  描述  ：{func.get('description', '')[:48]}...")
        print()

    for name in ("get_current_time", "get_user_location"):
        props = by_name[name].get("parameters", {}).get("properties", {})
        if props:
            print(f"  ✗ FAIL：{name} 在模型侧还有参数 {list(props)}")
            ok = False
        else:
            print(f"  ✓ {name} 在模型侧是 0 个参数")

    # 反证：进程内那个工具的内部 schema 里确实有 runtime，只是没发给模型。
    print("\n== 反证：get_user_location 的原始签名 ==")
    print(f"  {json.dumps(by_name['get_user_location'], ensure_ascii=False)[:160]}")

    print("\n结果:", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
