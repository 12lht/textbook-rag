"""探针：一个会话里反复需要记忆时，模型会不会**重复读**——真模型，4 轮。

要回答的问题是：「记忆是不是该在上下文里？上一轮读过之后，这一轮还需要，
是不是又得重新读一遍？」

分两个层面回答，这一支探针管**事实**那一面：

  1. 上一轮 `recall_memory` 的结果，**还在不在**这一轮发给模型的上下文里？
     （不该丢——窗口按轮裁，20 轮以内整轮都留着）
  2. 既然还在，模型这一轮**还会不会再调一次** `recall_memory`？

第 1 条是设计约束，必须成立，所以断言；第 2 条是模型的策略，只报告、不断言——
它重叠调用也不算错（`recall_memory` 是零外部配额的本地读，一次约 0.8ms），
但如果不重叠，说明「记忆留在上下文里就能复用」这个假设在真实链路里站得住。

顺带回归：`save_memory` 的 docstring 刚精简过，确认它还照常记。

接线说明：`memories.DB_PATH` 指到 .workbuddy/tmp/ 下的临时库，不碰 data/。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core
import memories

_TMP = Path(__file__).with_name(f"probe-ctx-memories-{uuid.uuid4().hex[:6]}.sqlite3")
memories.DB_PATH = _TMP

MEMORY_TOOLS = {"save_memory", "recall_memory"}
_THREADS: list[str] = []
RESULTS: list[bool] = []


async def turn(thread_id: str, user_id: str, text: str) -> tuple[str, list[dict]]:
    """跑一轮，返回（助手回复，这一轮模型发起的工具调用）。从事件流里收，不读状态。"""
    answer: list[str] = []
    calls: list[dict] = []
    async for event in agent_core.stream_agent(user_id, thread_id, text):
        if event["type"] == "assistant_delta":
            answer.append(event["content"])
        elif event["type"] == "tool_call":
            calls.extend(event["calls"])
    return "".join(answer).strip(), calls


def names(calls: list[dict]) -> list[str]:
    return [c["name"] for c in calls if c["name"] in MEMORY_TOOLS]


async def window(thread_id: str) -> list:
    """当前 checkpoint 里存着的原始消息列表——也就是下一轮会发出去的东西。"""
    graph = await agent_core.get_graph()
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return list(snapshot.values.get("messages", []))


async def context_evidence(thread_id: str) -> dict[str, list[str]]:
    """窗口里现在**装着这条事实的三个来源**，分别数一下。

    这一支探针真正要回答的是「后一轮还需不需要重读」——所以得先看清楚，
    这条事实在上下文里到底有几份：
      - 用户自己说的话（HumanMessage）
      - 模型自己调 `save_memory` 时填的参数（AIMessage.tool_calls）
      - 模型调 `recall_memory` 读回来的结果（ToolMessage）
    前两份**天然就带着**，所以窗口以内其实根本不需要重读。
    """
    evidence: dict[str, list[str]] = {"用户说过": [], "save 的参数": [], "recall 的结果": []}
    for message in await window(thread_id):
        kind = type(message).__name__
        if kind == "HumanMessage":
            evidence["用户说过"].append(str(message.content))
        elif kind == "AIMessage":
            for call in getattr(message, "tool_calls", []) or []:
                if call.get("name") == "save_memory":
                    evidence["save 的参数"].append(str(call.get("args", {}).get("text", "")))
        elif kind == "ToolMessage":
            if getattr(message, "name", "") == "recall_memory":
                evidence["recall 的结果"].append(str(message.content))
    return evidence


def check(label: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append(bool(condition))
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return bool(condition)


async def main() -> int:
    ok = True
    import service  # noqa: F401 —— 让它注入后端

    user_id = f"probe-ctx-{uuid.uuid4().hex[:6]}"
    thread_id = f"probe-ctx-thread-{uuid.uuid4().hex[:6]}"
    _THREADS.append(thread_id)

    rounds = [
        ("告知偏好", "记一下，我特别讨厌吃香菜，闻到都不行。"),
        ("第一次需要用到它", "我今天想点外卖，帮我推荐几个菜吧。"),
        ("第二次需要用到同一件事", "那明天中午呢，也帮我想想。"),
        ("第三次需要用到同一件事", "周末想请客，有什么建议？"),
    ]

    per_round: list[tuple[str, list[str]]] = []
    replies: list[str] = []
    for index, (label, text) in enumerate(rounds, start=1):
        before = await context_evidence(thread_id)
        reply, calls = await turn(thread_id, user_id, text)
        called = names(calls)
        replies.append(reply)
        per_round.append((label, called))
        after = await context_evidence(thread_id)
        print(f"== 第 {index} 轮（{label}） ==")
        print(f"   用户：{text}")
        print(f"   助手：{reply[:70]!r}")
        print(f"   记忆工具调用：{called or '（无）'}")
        print(
            f"   上下文里的证据：用户说过 {len(after['用户说过'])} 条 / "
            f"save 参数 {len(after['save 的参数'])} 条 / recall 结果 {len(after['recall 的结果'])} 条"
            f"（进这一轮前 recall 结果 {len(before['recall 的结果'])} 条）"
        )
        print()

    print("== 汇总 ==")
    for label, called in per_round:
        print(f"   {label:22s} {called or '（无）'}")

    saved = await asyncio.to_thread(memories.memories_of, user_id)
    print(f"\n   最终落盘：{[e['text'] for e in saved]}")

    print()
    ok &= check("docstring 精简后照样会记", len(saved) >= 1, f"{len(saved)} 条")
    ok &= check(
        "第 1 轮就调了 save_memory",
        "save_memory" in per_round[0][1],
        f"{per_round[0][1]}",
    )

    recall_rounds = [i for i, (_, c) in enumerate(per_round, start=1) if "recall_memory" in c]
    print(f"   调过 recall_memory 的轮次：{recall_rounds or '（一次都没有）'}")

    # 关键结论：事实在窗口里天然有两份，所以「需要用到」的那几轮不该重复读
    tail = await context_evidence(thread_id)
    ok &= check(
        "事实在上下文里有用户原话这份",
        any("香菜" in t for t in tail["用户说过"]),
        f"{len(tail['用户说过'])} 条用户消息",
    )
    ok &= check(
        "模型自己 save 的参数也留在上下文里",
        any("香菜" in t for t in tail["save 的参数"]),
        f"{tail['save 的参数']}",
    )
    ok &= check(
        "recall 的结果也保留在窗口里（这条是 save 之前读的，所以是空结果，符合预期）",
        len(tail["recall 的结果"]) >= 1,
        f"{len(tail['recall 的结果'])} 条：{tail['recall 的结果'][0][:20]!r}",
    )
    ok &= check(
        "后续用到它的那几轮里，回复确实体现了这条偏好",
        sum("香菜" in r for r in replies[1:]) >= 2,
        f"{sum('香菜' in r for r in replies[1:])}/3 轮提到",
    )
    ok &= check(
        "需要用到记忆的那几轮**没有**重复读（窗口内不需要重读）",
        all(i not in recall_rounds for i in (2, 3, 4)),
        f"重复读的轮次：{[i for i in recall_rounds if i != 1] or '无'}",
    )

    print()
    print("结论:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


async def run() -> int:
    try:
        return await main()
    finally:
        for thread_id in _THREADS:
            try:
                await agent_core.delete_checkpoint_thread(thread_id)
            except Exception:
                pass
        await agent_core.shutdown()
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(_TMP) + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
