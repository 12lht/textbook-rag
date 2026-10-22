"""探针：`chat_events` 到底在第几轮才推 title 事件（走完整生产链路）。

`probe-title-schedule.py` 只证明了纯函数 `should_generate_title` 对；
但「对不对」和「接没接上」是两件事——真正要验的是 `chat_events` 把**轮号**传对了。

做法：把 `service._build_title`（标题模型）和 `agent_core.stream_agent`（主模型）
都换成桩，所以**一次模型调用都不发**；再直接把 N 轮假历史灌进 checkpoint 当种子，
就等价于「这是第 N+1 轮」。于是 9 个用例全免费，且完全确定。

两处桩都做了「确实生效」的检查——这个项目踩过一次：
`memories.MEMORY_PATH = ...` 赋值不报错也不生效，只是给模块加了个没人读的属性，
探针于是静默地一直往真实库里写。**改到临时文件的赋值，一定要验证它真的生效。**
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402
import conversations  # noqa: E402
import service  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

SUFFIX = uuid.uuid4().hex[:6]
conversations.SUMMARY_PATH = Path(__file__).with_name(f"probe-tt-summaries-{SUFFIX}.json")
conversations.RENAME_PATH = Path(__file__).with_name(f"probe-tt-renames-{SUFFIX}.json")
conversations.OWNER_PATH = Path(__file__).with_name(f"probe-tt-owners-{SUFFIX}.json")
TMP_FILES = [
    conversations.SUMMARY_PATH,
    conversations.RENAME_PATH,
    conversations.OWNER_PATH,
]

FAILURES = 0
THREADS: list[str] = []

TITLE_CALLS: list[tuple[list[str], str]] = []
STREAM_CALLS: list[str] = []


async def fake_build_title(previous_messages: list[str], user_input: str) -> str:
    TITLE_CALLS.append((list(previous_messages), user_input))
    return f"桩标题{TITLE_CALLS.__len__()}"


async def fake_stream_agent(*args, **kwargs):
    STREAM_CALLS.append(str(args[2]) if len(args) > 2 else "?")
    yield {"type": "assistant_delta", "content": "（假回答）"}


async def guard_generate_title(_context: str):
    raise AssertionError("有东西绕过了 _build_title 直接调标题模型——桩没生效")


def check(condition: bool, label: str, extra: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  [ok]   {label}")
    else:
        print(f"  [FAIL] {label}{(' —— ' + extra) if extra else ''}")
        FAILURES += 1


async def run_turn(seed_turns: int) -> list[str]:
    """把 seed_turns 轮假历史灌进去，再跑「第 seed_turns+1 轮」，返回事件类型序列。"""
    thread_id = service.create_conversation("u-tt")["thread_id"]
    THREADS.append(thread_id)
    graph = await agent_core._ensure_graph()
    seeded = [SystemMessage(content="（探针用的系统提示）")]
    for i in range(seed_turns):
        seeded.append(HumanMessage(content=f"第 {i + 1} 轮的问题"))
        seeded.append(AIMessage(content=f"第 {i + 1} 轮的回答"))
    await graph.aupdate_state(agent_core._config(thread_id), {"messages": seeded})
    return [
        event["type"]
        async for event in service.chat_events(
            "u-tt", thread_id, f"第 {seed_turns + 1} 轮的问题"
        )
    ]


async def main() -> int:
    service._build_title = fake_build_title
    agent_core.stream_agent = fake_stream_agent
    agent_core.generate_title = guard_generate_title

    print("桩的生效检查")
    check(service._build_title is fake_build_title, "service._build_title 已换成桩")
    check(agent_core.stream_agent is fake_stream_agent, "agent_core.stream_agent 已换成桩")
    check(
        conversations.SUMMARY_PATH.name.startswith("probe-tt-"),
        "会话元数据已指向临时文件",
        conversations.SUMMARY_PATH.name,
    )

    print("\n第几轮会推 title 事件")
    cases = [
        (0, 1, True),
        (1, 2, False),
        (9, 10, False),
        (10, 11, True),
        (11, 12, False),
        (19, 20, False),
        (20, 21, True),
        (30, 31, True),
        (40, 41, True),
        (41, 42, False),
        (49, 50, False),
        (50, 51, False),
        (59, 60, False),
    ]
    for seed, turn, expected in cases:
        TITLE_CALLS.clear()
        STREAM_CALLS.clear()
        types = await run_turn(seed)
        got = "title" in types
        check(
            got == expected,
            f"第 {turn:3d} 轮 {'推' if expected else '不推'} title",
            f"实际 {'推' if got else '不推'}，事件 {types}",
        )
        if got != expected:
            continue
        if "assistant_delta" not in types:
            check(False, f"第 {turn} 轮主模型事件缺失", str(types))
        if not expected:
            check(
                not TITLE_CALLS,
                f"第 {turn:3d} 轮没白调标题模型",
                f"调了 {len(TITLE_CALLS)} 次",
            )

    print("\n手动改过名字的会话")
    thread_id = service.create_conversation("u-tt")["thread_id"]
    THREADS.append(thread_id)
    conversations.rename_conversation(thread_id, "我自己起的名字")
    graph = await agent_core._ensure_graph()
    await graph.aupdate_state(
        agent_core._config(thread_id),
        {"messages": [SystemMessage(content="（探针用的系统提示）")]},
    )
    TITLE_CALLS.clear()
    types = [
        event["type"]
        async for event in service.chat_events("u-tt", thread_id, "第 1 轮的问题")
    ]
    check("title" not in types, "第 1 轮也不覆盖用户手改的名字", str(types))
    check(not TITLE_CALLS, "被手改过就不调标题模型", f"调了 {len(TITLE_CALLS)} 次")

    for tid in THREADS:
        try:
            await agent_core.delete_checkpoint_thread(tid)
        except Exception as error:  # noqa: BLE001
            print(f"  清理 {tid} 失败（不影响结论）：{error}")
    await agent_core.shutdown()
    for path in TMP_FILES:
        path.unlink(missing_ok=True)

    print(f"\n失败项：{FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
