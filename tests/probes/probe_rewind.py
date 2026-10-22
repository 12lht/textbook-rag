"""验证：能否用 update_state + RemoveMessage 截断 thread 的消息（不调用任何 LLM）。"""
import json
import sys

sys.path.insert(0, "D:/agent")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, RemoveMessage

import service
from agent_core import graph

tid = service.create_conversation()["thread_id"]
config = {"configurable": {"thread_id": tid}}

try:
    graph.update_state(
        config,
        {"messages": [
            SystemMessage(id="s0", content="系统提示"),
            HumanMessage(id="h1", content="问题1"),
            AIMessage(id="a1", content="回答1"),
            HumanMessage(id="h2", content="问题2"),
            AIMessage(id="a2", content="回答2"),
            HumanMessage(id="h3", content="问题3"),
            AIMessage(id="a3", content="回答3"),
        ]},
        as_node="truncate",
    )

    def show(label):
        msgs = graph.get_state(config).values.get("messages", [])
        print(f"{label}: " + " | ".join(f"{type(m).__name__}({getattr(m, 'id', '?')})={m.content!r}" for m in msgs))

    show("初始")

    msgs = graph.get_state(config).values["messages"]
    humans = [i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)]
    print("人类消息位置:", humans, "-> 第 2 条(索引1)在列表位置", humans[1])

    cut = humans[1]
    to_remove = msgs[cut:]
    print("将删除:", [getattr(m, "id", "?") for m in to_remove])

    graph.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in to_remove]})
    show("截断后")

    msgs = graph.get_state(config).values["messages"]
    humans = [i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)]
    cut = humans[0]
    graph.update_state(config, {"messages": [RemoveMessage(id=m.id) for m in msgs[cut:]]})
    show("再截断到第一条之前")

    print("\n结论：update_state + RemoveMessage 可以正确截断 thread 消息")
finally:
    service.delete_conversation(tid)
    print("已清理测试会话", tid)
