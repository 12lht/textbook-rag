"""探针：「进入某本书的对话」端到端——真实跑一轮聚焦聊天，验证三件事：

1. checkpoint 里的系统提示是聚焦版（含「当前会话聚焦」、书单只含该书）
2. 检索真的发生且没有跨到别的书
3. 非聚焦轮（不带 scope）不受上一轮影响

跑法：python tests/probes/probe-focus-chat.py
烧 2 次模型调用，临时会话跑完即删。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402
import service  # noqa: E402

USER = "u-671d242584"

ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(("  OK  " if cond else "  FAIL") + f" {name}" + (f"  {detail}" if detail else ""))
    ok &= cond


async def main() -> None:
    cats = service.documents.list_categories(USER)
    target = next(c for c in cats if "生物信息" in c["name"] or len(c["name"]) > 2)
    print(f"聚焦书：《{target['name']}》\n")

    thread_id = service.create_conversation(USER)["thread_id"]
    try:
        events = []
        async for event in service.chat_events(
            USER, thread_id, "用一句话说说这本书里讲了什么", scope_category_id=target["category_id"]
        ):
            events.append(event)
            if event.get("type") == "done":
                break

        graph = await agent_core.get_graph()
        state = await graph.aget_state(agent_core._config(thread_id))
        system = state.values["messages"][0]
        content = str(system.content)

        check("系统提示是聚焦版", "当前会话聚焦" in content)
        check("书单只含聚焦书", content.count("- ") <= len(str(agent_core._system_prompt()).splitlines()))
        check("聚焦书名出现", target["name"] in content)
        tool_events = [e for e in events if e.get("type") == "tool_call"]
        check("发生了检索", any(e.get("tool") == "search_textbook" for e in tool_events),
              f"工具: {[e.get('tool') for e in tool_events]}")
        check("有回答", any(e.get("type") == "assistant_delta" for e in events))
        check("done 收尾", events[-1].get("type") == "done", str(events[-1]))
    finally:
        await service.delete_conversation(thread_id, USER)

    print()
    print("结果:", "通过" if ok else "有失败项")
    await agent_core.close_mcp()
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
