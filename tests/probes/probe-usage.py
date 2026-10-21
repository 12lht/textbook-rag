"""探针：确认能不能从 checkpoint 里读到真实的 token 用量。

如果 AIMessage 上带 usage_metadata，那 eval/run.py 就能在删 thread 之前把
token 数捞出来，评测报告就能多一行「本轮共消耗 N token」，而且零额外请求。
"""
import sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core
from langchain_core.messages import AIMessage, ToolMessage, HumanMessage

thread_id = "probe-usage-0001"
started = time.perf_counter()
events = []
for event in agent_core.stream_agent("probe-user", thread_id, "现在几点了？"):
    events.append(event)
elapsed = (time.perf_counter() - started) * 1000

print("事件流：", [e.get("type") for e in events])
print(f"耗时 {elapsed:.0f} ms")
print()

state = agent_core.graph.get_state({"configurable": {"thread_id": thread_id}}).values
print("checkpoint 里的消息：")
total_in = total_out = 0
for m in state.get("messages", []):
    usage = getattr(m, "usage_metadata", None)
    line = f"  {type(m).__name__:<14} chars={len(str(m.content)):<6}"
    if usage:
        total_in += usage.get("input_tokens", 0) or 0
        total_out += usage.get("output_tokens", 0) or 0
        line += f" in={usage.get('input_tokens')} out={usage.get('output_tokens')}"
    else:
        line += " usage_metadata=无"
    print(line)

print()
print(f"合计 input_tokens={total_in}  output_tokens={total_out}  总计={total_in + total_out}")

# 顺带看看 response_metadata 里有没有另一条路径
for m in state.get("messages", []):
    if isinstance(m, AIMessage):
        rm = getattr(m, "response_metadata", {}) or {}
        keys = [k for k in rm if "token" in k.lower() or "usage" in k.lower()]
        print("response_metadata 里与用量相关的 key：", keys)

agent_core.delete_checkpoint_thread(thread_id)
print("\n已清理探针会话。")
