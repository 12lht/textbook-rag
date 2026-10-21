"""验证 agent_core 的 MCP + 全量异步改造。

重点验四件事：
1. MCP 会话能起来，工具名与改造前一致
2. 跑图能正常流式输出、工具能真正执行
3. AsyncSqliteSaver 的 checkpoint 读写（含 aget_state / aupdate_state）可用
4. rewind / 回退后空会话重跑 这些边界还能过
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core

EXPECTED_TOOLS = [
    "get_weather_for_location",
    "get_user_location",
    "get_current_time",
    "web_search",
    "web_extract",
]


async def drain(thread_id: str, query: str) -> list[dict]:
    return [e async for e in agent_core.stream_agent("probe-user", thread_id, query)]


async def main() -> int:
    ok = True

    print("=== 1. 从 MCP 加载工具 ===")
    tools = await agent_core.load_tools()
    names = [t.name for t in tools]
    print(f"  工具数={len(names)}  {names}")
    same = names == EXPECTED_TOOLS
    print(f"  与改造前顺序/名字一致：{'是' if same else '否'}")
    ok = ok and same

    print("\n=== 2. 跑一次带工具的对话 ===")
    thread_id = "probe-async-0002"
    events = await drain(thread_id, "现在几点了？")
    kinds = [e["type"] for e in events if e["type"] != "assistant_delta"]
    print(f"  非 delta 事件：{kinds}")
    answer = "".join(e["content"] for e in events if e["type"] == "assistant_delta")
    print(f"  回答：{answer[:80]}")
    calls = [e["tool"] for e in events if e["type"] == "tool_call"]
    results = [e["tool"] for e in events if e["type"] == "tool_result"]
    print(f"  tool_call={calls} tool_result={results}")
    two_ok = calls == results and calls == ["get_current_time"]
    print(f"  调用与执行一一对应且无重复：{'是' if two_ok else '否'}")
    ok = ok and two_ok

    print("\n=== 3. get_history（异步 checkpoint 读）===")
    history = await agent_core.get_history(thread_id)
    print(f"  条数={len(history)}  首条 role={history[0]['role'] if history else '无'}")
    print(f"  assistant operations={history[-1].get('operations') if history else '无'}")
    ok = ok and len(history) >= 2

    print("\n=== 4. rewind_thread（异步 aupdate_state）===")
    try:
        result = await agent_core.rewind_thread(thread_id, 0)
        print(f"  removed={result['removed']} emptied={result['emptied']} text={result['text']!r}")
        after = await agent_core.get_history(thread_id)
        print(f"  回退后条数={len(after)}")
        ok = ok and result["removed"] >= 2 and len(after) == 0
    except Exception as exc:
        print(f"  失败：{type(exc).__name__}: {exc}")
        ok = False

    print("\n=== 5. 回退后的空会话再跑一次（验证 should_continue 容错）===")
    try:
        events2 = await drain(thread_id, "你好")
        kinds2 = [e["type"] for e in events2 if e["type"] != "assistant_delta"]
        answer2 = "".join(e["content"] for e in events2 if e["type"] == "assistant_delta")
        print(f"  事件：{kinds2}  回答：{answer2[:50]}")
        ok = ok and "done" in kinds2
    except Exception as exc:
        print(f"  失败：{type(exc).__name__}: {exc}")
        ok = False

    print("\n=== 6. 多工具用例（定位 + 天气）===")
    thread_id2 = "probe-async-0003"
    try:
        events3 = await drain(thread_id2, "我这里今天的天气怎么样？")
        calls3 = [e["tool"] for e in events3 if e["type"] == "tool_call"]
        answer3 = "".join(e["content"] for e in events3 if e["type"] == "assistant_delta")
        print(f"  调用={calls3}")
        print(f"  回答：{answer3[:70]}")
        ok = ok and len(calls3) >= 1
    except Exception as exc:
        print(f"  失败：{type(exc).__name__}: {exc}")
        ok = False
    await agent_core.delete_checkpoint_thread(thread_id2)

    await agent_core.delete_checkpoint_thread(thread_id)
    await agent_core.shutdown()
    print(f"\n全部通过：{ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
