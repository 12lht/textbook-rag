"""探针：真跑几轮对话，看模型**会不会自己想起来**调 save_memory。

这是「去掉每轮自动抽取、把触发权交给模型」之后唯一的风险点：
如果它根本不调，这个功能就是死的。所以必须打真模型验一次。

三件事：
  1. 用户明确告知长期信息 -> 应该调 save_memory
  2. 用户改主意           -> 应该用 replaces 作废旧条目（或至少存下新事实）
  3. 一次性的临时问题      -> **不该**调，防止它为了记而记

存储指向临时文件，不碰 data/memories.json；直接跑 agent_core.stream_agent
（不是 service.chat_events），免得顺带污染会话元数据——注入后端的那三行
由本探针照抄 service 的写法。
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

_TMP = Path(__file__).with_name(f"probe-memories-{uuid.uuid4().hex[:6]}.sqlite3")
memories.DB_PATH = _TMP

MEMORY_TOOLS = {"save_memory", "recall_memory"}

_THREADS: list[str] = []


async def turn(thread_id: str, user_id: str, text: str) -> tuple[str, list[dict]]:
    """跑一轮，返回（助手回复，**这一轮**模型发起的工具调用列表）。

    工具调用从事件流里收，不要事后去读状态——读状态拿到的是整个会话的历史，
    上一轮的调用会被算到这一轮头上（第一版就写错了，把第 3 轮误判成「乱调工具」）。
    """
    answer: list[str] = []
    calls: list[dict] = []
    async for event in agent_core.stream_agent(user_id, thread_id, text):
        if event["type"] == "assistant_delta":
            answer.append(event["content"])
        elif event["type"] == "tool_call":
            calls.extend(event["calls"])
    return "".join(answer).strip(), calls


def names(calls: list[dict]) -> list[str]:
    return [call["name"] for call in calls if call["name"] in MEMORY_TOOLS]


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return condition


async def main() -> int:
    ok = True

    import service  # noqa: F401

    ok &= check(
        "service 导入时已把后端注入给 agent_core",
        agent_core._memory_write is memories.remember
        and agent_core._memory_read is memories.memories_of,
    )

    user_id = f"probe-user-{uuid.uuid4().hex[:6]}"
    thread_id = f"probe-thread-{uuid.uuid4().hex[:6]}"
    _THREADS.append(thread_id)

    print()
    print("== 第 1 轮：用户告知两条长期信息 ==")
    text = "跟你说个事，我不吃香菜。另外我家猫叫豆豆，今年三岁了。"
    reply, calls = await turn(thread_id, user_id, text)
    called = names(calls)
    print(f"   用户：{text}")
    print(f"   助手：{reply[:80]!r}")
    print(f"   记忆工具调用：{called or '（无）'}")
    for call in calls:
        if call["name"] in MEMORY_TOOLS:
            print(f"     -> {call['name']} {call['args']}")
    stored = memories.memories_of(user_id)
    print(f"   落盘：{[e['text'] for e in stored]}")
    ok &= check("模型自己调了 save_memory", bool(called))
    ok &= check("确实写进去了", len(stored) >= 1)
    ok &= check("记住了猫的名字", any("豆豆" in e["text"] for e in stored))

    old_spicy = [e["text"] for e in stored if "香菜" in e["text"]]
    print(f"   第 1 轮关于香菜的原文：{old_spicy}")

    print()
    print("== 第 2 轮：用户改主意 ==")
    text = "我改主意了，现在能吃香菜了。"
    reply, calls = await turn(thread_id, user_id, text)
    called = names(calls)
    print(f"   用户：{text}")
    print(f"   助手：{reply[:80]!r}")
    for call in calls:
        if call["name"] in MEMORY_TOOLS:
            print(f"     -> {call['name']} {call['args']}")
    after = memories.memories_of(user_id)
    print(f"   落盘：{[e['text'] for e in after]}")
    texts = [e["text"] for e in after]
    ok &= check("用了 replaces 作废旧条目", any("replaces" in (c.get("args") or {}) for c in calls if c["name"] == "save_memory"))
    ok &= check(
        "第 1 轮那条原文已经作废（精确比对，不是子串）",
        all(t not in texts for t in old_spicy),
        f"旧原文 {old_spicy} / 现在 {texts}",
    )
    ok &= check("新事实存下来了", any("香菜" in t for t in texts))

    print()
    print("== 第 3 轮（对照）：一次性的临时问题，不该记 ==")
    text = "帮我算一下 37 乘以 12 等于多少？"
    reply, calls = await turn(thread_id, user_id, text)
    called = names(calls)
    print(f"   用户：{text}")
    print(f"   助手：{reply[:80]!r}")
    print(f"   记忆工具调用：{called or '（无）'}")
    ok &= check("没有为了记而记", not called, f"{called}")
    ok &= check("存储条数没变", len(memories.memories_of(user_id)) == len(after))

    print()
    print("最终落盘：")
    for entry in memories.memories_of(user_id):
        print(f"   [{entry['kind']}] {entry['text']}")

    print()
    print("结果:", "全部通过" if ok else "有失败")
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
