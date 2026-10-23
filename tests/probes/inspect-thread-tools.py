"""只读检查：某个会话里每一次工具调用的**参数和返回原文**。

为什么需要它：前端面板把所有 tool_result 都渲染成「工具完成」，`get_history`
也不带 `status` —— 于是「工具失败了」和「工具成功了」在界面上长得一模一样。
要看真相只能直接读 checkpoint。

用法：python inspect-thread-tools.py <thread_id>
**只读，不写任何东西。**
"""
import asyncio
import os
import sys

os.environ.setdefault("no_proxy", "*")

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

import agent_core  # noqa: E402

THREAD = sys.argv[1] if len(sys.argv) > 1 else "t-a546fb48"


def one_line(value: str, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


async def main() -> None:
    graph = await agent_core._ensure_graph()
    state = (await graph.aget_state(agent_core._config(THREAD))).values
    messages = state.get("messages", [])
    print(f"thread = {THREAD}，消息条数 = {len(messages)}\n")

    pending: dict[str, dict] = {}
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            if message.tool_calls:
                for call in message.tool_calls:
                    pending[call["id"]] = call
                    print(f"[{index:3d}] 调用 {call['name']}  args={one_line(call.get('args'), 200)}")
            reasoning = (message.additional_kwargs or {}).get("reasoning_content") or ""
            if reasoning:
                print(f"[{index:3d}] （这一轮思维链 {len(reasoning)} 字）")
        elif isinstance(message, ToolMessage):
            call = pending.get(message.tool_call_id, {})
            status = getattr(message, "status", "?")
            flag = "正常" if status == "success" else "★失败★"
            print(f"[{index:3d}] 返回 {message.name}  status={flag}")
            print(f"        args   = {one_line(call.get('args'), 200)}")
            print(f"        result = {one_line(message.content, 220)}")
    await agent_core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
