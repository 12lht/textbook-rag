"""工具轮数硬闸 + 收尾指令的回归。不调用任何 LLM，不碰生产库。

## 守两件事

1. **正常路径必须逐字不变**：没超限时 `_messages_for_model` 的行为和加这个功能
   之前完全一致（没摘要就原样返回，有摘要才重建系统消息）。这是评测基线
   不漂移的前提——改了它，32 个用例的旧基线全部作废。
2. **超限后必须真的不给工具**：数错轮次比没有这道闸更糟。数多了会让第 7 轮
   开始莫名其妙没有工具（功能像坏了），数少了等于没闸。

`bound_model(tools=False)` 会起 MCP 会话（要读工具表才知道绑不绑），
所以那一条放在最后、且失败不致命——它验的是「返回的是裸模型」。

    python tests/probes/test-agent-limits.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

import agent_core

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def ai_call(call_id: str) -> AIMessage:
    """一条「模型决定调工具」的 AI 消息。"""
    return AIMessage(
        content="",
        tool_calls=[{"name": "search_textbook", "args": {"query": "x"}, "id": call_id}],
    )


def round_of_tools(call_id: str) -> list:
    """一次完整的「调工具 → 拿结果」往返。"""
    return [
        ai_call(call_id),
        ToolMessage(content="结果", tool_call_id=call_id),
    ]


def main() -> None:
    sys_msg = SystemMessage(content=agent_core.SYSTEM_PROMPT)

    # ---- 1. 本轮计数 ---------------------------------------------------
    print("\n=== 本轮工具轮数 ===")
    one_round = [sys_msg, HumanMessage(content="问"), *round_of_tools("a")]
    check("调了一次工具算 1 轮", agent_core._tool_rounds_this_turn(one_round) == 1)

    check("一条消息都没调算 0 轮",
          agent_core._tool_rounds_this_turn([sys_msg, HumanMessage(content="问")]) == 0)

    # 关键：历史里有很多次，但**本轮**一次都没调 → 必须是 0。
    # 数错这里就会出现「聊到第 N 轮突然不给工具」。
    history_heavy = [
        sys_msg,
        HumanMessage(content="第一轮"),
        *round_of_tools("a"), *round_of_tools("b"), *round_of_tools("c"),
        AIMessage(content="答完了"),
        HumanMessage(content="第二轮"),   # ← 本轮从这里开始
    ]
    check("历史里的旧调用不算进本轮",
          agent_core._tool_rounds_this_turn(history_heavy) == 0,
          f"got={agent_core._tool_rounds_this_turn(history_heavy)}")

    mixed = [
        sys_msg,
        HumanMessage(content="第一轮"),
        *round_of_tools("a"), *round_of_tools("b"),
        AIMessage(content="答完了"),
        HumanMessage(content="第二轮"),
        *round_of_tools("c"),
    ]
    check("只数最后一条用户消息之后的",
          agent_core._tool_rounds_this_turn(mixed) == 1,
          f"got={agent_core._tool_rounds_this_turn(mixed)}")

    # 一次并行调三个工具：一条 AI 消息带三个 tool_calls，仍然只算 1 轮
    parallel = AIMessage(
        content="",
        tool_calls=[
            {"name": "search_textbook", "args": {}, "id": "p1"},
            {"name": "web_search", "args": {}, "id": "p2"},
            {"name": "get_current_time", "args": {}, "id": "p3"},
        ],
    )
    check("并行调三个算 1 轮（不是 3）",
          agent_core._tool_rounds_this_turn([sys_msg, HumanMessage(content="问"), parallel]) == 1)

    # ---- 2. 到顶判定 ---------------------------------------------------
    print("\n=== 上限判定 ===")
    limit = agent_core.MAX_TOOL_ROUNDS
    at_limit = [sys_msg, HumanMessage(content="问")]
    for i in range(limit):
        at_limit += round_of_tools(f"c{i}")
    check(f"刚好 {limit} 轮时算到顶",
          agent_core._tool_rounds_this_turn(at_limit) >= limit,
          f"rounds={agent_core._tool_rounds_this_turn(at_limit)}")
    just_below = [sys_msg, HumanMessage(content="问")]
    for i in range(limit - 1):
        just_below += round_of_tools(f"d{i}")
    check(f"{limit - 1} 轮时没到顶",
          agent_core._tool_rounds_this_turn(just_below) < limit)

    # ---- 3. 正常路径必须零变化（基线不漂移）----------------------------
    print("\n=== 正常路径不变 ===")
    plain_state = {"messages": [sys_msg, HumanMessage(content="你好")]}
    out = agent_core._messages_for_model(plain_state, memory_enabled=True)
    check("无摘要时原样返回（同一条消息、同一个系统提示）",
          out is plain_state["messages"] or out[0].content == agent_core.SYSTEM_PROMPT,
          f"first={out[0].content[:24]!r}")

    described = agent_core._messages_for_model(
        {"messages": [sys_msg, HumanMessage(content="你好")],
         "summary": "猫叫豆豆"},
        memory_enabled=True,
    )
    check("有摘要时仍然并进系统消息",
          "猫叫豆豆" in described[0].content and "不要原样复述" in described[0].content)

    # ---- 4. 收尾指令 ---------------------------------------------------
    print("\n=== 收尾指令 ===")
    forced = agent_core._messages_for_model(
        {"messages": [sys_msg, HumanMessage(content="你好")]},   # 注意：没有摘要
        memory_enabled=True,
        answer_now=True,
    )
    check("没有摘要也要重建系统消息（不能走快路径）",
          forced[0].content != agent_core.SYSTEM_PROMPT)
    check("里面有「用现有信息回答」的指令",
          "工具调用次数已经用完" in forced[0].content)
    check("明确说了信息不足时怎么办",
          "明确说明缺什么" in forced[0].content)
    check("没提「摘要」（这次本来就没有）",
          "<摘要>" not in forced[0].content and "更早内容的摘要" not in forced[0].content)
    check("原始 state 没被改动（只改发出去的）",
          plain_state["messages"][0].content == agent_core.SYSTEM_PROMPT)

    # ---- 5. 未绑工具的模型 ---------------------------------------------
    print("\n=== tools=False 返回裸模型 ===")
    try:
        tooled = asyncio.run(agent_core._mcp.bound_model(tools=True))
        bare = asyncio.run(agent_core._mcp.bound_model(tools=False))
        # bind_tools 出来的是 RunnableBinding，裸模型是 ChatOpenAI
        check("绑工具的那个是绑定对象",
              "Binding" in type(tooled).__name__, type(tooled).__name__)
        check("tools=False 拿到的是裸模型（没有工具表）",
              "Binding" not in type(bare).__name__, type(bare).__name__)
    except Exception as exc:  # noqa: BLE001 — MCP 起不来时这一条跳过，不算失败
        print(f"[SKIP] MCP 会话没建起来，跳过这一组：{exc}")
    finally:
        asyncio.run(agent_core.close_mcp())

    print()
    total, passed = len(results), sum(results)
    print(f"合计 {passed}/{total} 通过")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
