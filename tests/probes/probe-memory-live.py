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

# 换到临时库，别把真实记忆写坏。
# **放在项目内而不是系统 temp**：沙箱不允许往 Temp 目录写，实测会抛 PermissionError
# （这个坑正好顺带暴露了工具层没兜异常的问题，见 agent_core.save_memory）。
#
# 注意变量名是 `DB_PATH`（2026-10-10 从 `MEMORY_PATH` 改成 SQLite 后改的名）。
# 曾经写成 `MEMORY_PATH`，赋值**不报错但毫无作用**（只是给模块加了个没人看的属性），
# 于是探针悄悄地往真实的 data/memories.sqlite3 里写——「改到临时文件」这句注释变成谎话。
_TMP = Path(__file__).with_name(f"probe-memories-{uuid.uuid4().hex[:6]}.sqlite3")
memories.DB_PATH = _TMP

MEMORY_TOOLS = {"save_memory", "recall_memory"}

# 探针自己造的会话 id，收尾时清掉，别留在 checkpoint 里
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

    # service 是唯一该注入后端的层——确认它真的注入了，而不是只在探针里手动接上
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

    # 记下第 1 轮存的那条香菜偏好**原文**，第 2 轮作废之后要按它来比对
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
    # **按原文精确比对，不要子串匹配**。踩过：原来写的是
    # `not any("不吃香菜" in t ...)`，而模型很自然会把新条目写成
    # 「用户**以前不吃香菜**，2026 年 10 月起已改口」——旧条目确实删干净了，
    # 却被新条目的措辞误判成「还在」。断言要盯「那一条原文还在不在」，
    # 不是「这几个字还在不在」。
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
        # 探针造出来的会话别留在 checkpoint 里
        for thread_id in _THREADS:
            try:
                await agent_core.delete_checkpoint_thread(thread_id)
            except Exception:
                pass
        await agent_core.shutdown()
        # SQLite 还可能留下 -wal / -shm 边车文件，一起收
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(_TMP) + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
