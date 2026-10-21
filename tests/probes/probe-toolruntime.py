"""探针：验证「进程内工具 + ToolRuntime[Context] 注入」在当前 langgraph 版本下能不能用。

要验证的问题很具体：
  ToolNode 是否会把 ToolRuntime 注入到工具函数里，让工具能读到 Context.location？

如果这条不通，方案 B 就不成立，得退回「把位置拼进参数」之类的写法。
本探针不碰 MCP、不碰模型、不花钱。
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode


@dataclass
class Context:
    user_id: str
    location: str | None = None


SEEN = {}


@tool
def get_user_location(runtime: ToolRuntime[Context]) -> str:
    """获取用户授权提供的当前位置。"""
    context = getattr(runtime, "context", None)
    SEEN["context"] = context
    SEEN["location"] = getattr(context, "location", "NO_ATTR")
    SEEN["user_id"] = getattr(context, "user_id", "NO_ATTR")
    loc = getattr(context, "location", None)
    if not loc:
        return "用户没有提供位置。"
    return f"用户授权提供的当前位置：{loc}。"


class State(dict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


node = ToolNode([get_user_location])


async def call_tool(state, runtime):
    return await node.ainvoke(state, runtime=runtime)


builder = StateGraph(State)
builder.add_node("tools", call_tool)
builder.set_entry_point("tools")
builder.add_edge("tools", END)
graph = builder.compile()


async def main() -> int:
    ok = True

    ai = AIMessage(
        content="",
        tool_calls=[{"name": "get_user_location", "args": {}, "id": "call_1"}],
    )
    state = {"messages": [HumanMessage(content="我在哪？"), ai]}

    print("== 1. 通过 graph.astream 传 context ==")
    result = None
    async for _mode, chunk in graph.astream(
        state, context=Context(user_id="u1", location="北京市"), stream_mode=["updates"]
    ):
        result = chunk
    print("   graph 返回:", result)

    print("\n== 2. 工具里实际看到的 runtime ==")
    for key in ("user_id", "location"):
        print(f"   runtime.context.{key} = {SEEN.get(key)!r}")
    print(f"   runtime.context 本身 = {SEEN.get('context')!r}")

    if SEEN.get("location") != "北京市":
        print("\n   ✗ FAIL：工具没拿到 location")
        ok = False
    else:
        print("\n   ✓ 工具拿到了 Context 里的 location")

    print("\n== 3. context 缺省（location=None）时是否优雅 ==")
    SEEN.clear()
    async for _mode, chunk in graph.astream(
        state, context=Context(user_id="u1"), stream_mode=["updates"]
    ):
        result = chunk
    message = result["tools"]["messages"][0]
    print("   工具返回:", message.content)
    if "没有提供" not in str(message.content):
        print("   ✗ FAIL：缺省时没有走降级分支")
        ok = False
    else:
        print("   ✓ 缺省时优雅降级")

    print("\n结果:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
