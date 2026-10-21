"""探针：在真实图里跑一遍摘要化（端到端，会调真模型）。

把窗口临时收到 2 轮，让摘要在几轮内就触发，然后问一个**只在被裁掉的那部分里
出现过**的事实，看模型答不答得出来。这是「摘要没有白做」的最小证明。

不是评测——评测需要正式的用例集和 before/after 对比。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core

# 收紧阈值，让摘要一轮内就触发：两条线都压到 1，于是任何「第二轮」一进来就会把
# 前一轮压成摘要，而且只保留当前这一轮（目标线太低，没有更少的丢法）。
agent_core.CONTEXT_TRIGGER_TOKENS = 1
agent_core.CONTEXT_TARGET_TOKENS = 1

TURNS = [
    "请记住一个事实：我的猫叫豆豆，今年 3 岁。",
    "它是一只橘猫。",
    "我下周要去西安出差。",
    "今天天气不错。",
]


async def say(thread_id: str, text: str) -> str:
    answer = []
    async for event in agent_core.stream_agent("probe", thread_id, text):
        if event["type"] == "assistant_delta":
            answer.append(event["content"])
    return "".join(answer).strip()


async def main() -> int:
    thread_id = f"summary-probe-{uuid.uuid4().hex[:8]}"
    ok = True
    try:
        for index, text in enumerate(TURNS, 1):
            reply = await say(thread_id, text)
            state = (await agent_core.get_graph()).aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            snapshot = await state
            messages = snapshot.values.get("messages", [])
            summary = snapshot.values.get("summary") or ""
            print(f"第 {index} 轮  消息数 {len(messages):>2}  摘要 {len(summary):>3} 字")
            print(f"           用户：{text}")
            print(f"           助手：{reply[:60]!r}")

        print()
        print("===== 被裁掉那部分的摘要 =====")
        print(summary or "（还没生成）")
        print("==============================\n")

        kept = [str(m.content)[:16] for m in messages]
        print(f"留在窗口里的消息：{kept}\n")

        if "豆豆" not in summary:
            print("✗ 摘要里没记住「豆豆」——它已经被裁出窗口，之后再也查不到了")
            ok = False
        else:
            print("✓ 摘要里留住了「豆豆」")

        print()
        print("----- 现在问一个只在被裁掉的部分里出现过的事实 -----")
        question = "我的猫叫什么名字？今年几岁？"
        answer = await say(thread_id, question)
        print(f"   问：{question}")
        print(f"   答：{answer}")
        print()
        hit_name = "豆豆" in answer
        hit_age = "3" in answer or "三" in answer
        ok &= hit_name and hit_age
        print(f"   {'✓' if hit_name else '✗'} 答出了名字")
        print(f"   {'✓' if hit_age else '✗'} 答出了年龄")

        print()
        print("结果:", "摘要确实把早期事实带过来了" if ok else "有信息丢失")
    finally:
        await agent_core.delete_checkpoint_thread(thread_id)
        await agent_core.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
