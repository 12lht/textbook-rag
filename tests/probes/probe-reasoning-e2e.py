"""探针：思维链从模型一直到前端的完整链路。

验证四件事：
1. `stream_agent` 能实时吐出 `reasoning_delta`（前端边生成边显示靠它）；
2. 思维链出现在正文**之前**（顺序错了会变成「先答题后思考」）；
3. `get_history` 能把思维链补回来，且与实时收到的**逐字相同**
   （重开页面靠它；两者不一致就说明落盘和实时是两条不同的数据）；
4. 摘要节点（truncate）的思维链**没有**被混进来。

第 4 点的做法（换过一次判据，记一下为什么）：一开始想用「生造词只在摘要里出现」
来判，失败了——因为那个词也躺在**保留下来**的历史里，主模型自己就会说它，
分不清是泄漏还是正常。现在的判据是长度对比：`get_history` 只可能读到主模型的
消息（摘要那次调用的结果只留在 `state["summary"]`，从不作为 AIMessage 进 state），
所以「实时长度 == 历史长度」等价于「摘要一个 token 都没漏」。再配一条「摘要确实
被触发」的正向对照，避免摘要没跑时用例空过。
"""
import asyncio
import os
import sys

os.environ.setdefault("no_proxy", "*")
sys.path.insert(0, r"D:\agent")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

import agent_core  # noqa: E402

PLAIN_THREAD = "t-probe-reasoning-plain"
SEEDED_THREAD = "t-probe-reasoning-seeded"
MARKER = "鲸落协议"

failures = 0


def check(condition: bool, label: str, extra: str = "") -> None:
    global failures
    if condition:
        print(f"  [ok]   {label}")
    else:
        print(f"  [FAIL] {label}{(' —— ' + extra) if extra else ''}")
        failures += 1


def split(events: list[dict]) -> tuple[str, str]:
    reasoning = "".join(e["content"] for e in events if e["type"] == "reasoning_delta")
    content = "".join(e["content"] for e in events if e["type"] == "assistant_delta")
    return reasoning, content


def history_reasoning(history: list[dict]) -> str:
    return "".join(
        op["text"]
        for message in history
        if message["role"] == "assistant"
        for op in (message.get("operations") or [])
        if op["kind"] == "reasoning"
    )


async def collect(thread_id: str, question: str) -> list[dict]:
    return [event async for event in agent_core.stream_agent("1", thread_id, question)]


async def main() -> int:
    print("用例 1：实时思维链 + 与历史一致")
    events = await collect(PLAIN_THREAD, "用一句话说明什么是可重构智能表面（RIS）。")
    reasoning, content = split(events)
    kinds = [e["type"] for e in events]

    check("reasoning_delta" in kinds, "收到了 reasoning_delta 事件")
    check(bool(reasoning), "思维链内容非空", f"长度 {len(reasoning)}")
    check(bool(content), "正文内容非空", f"长度 {len(content)}")

    first_reasoning = next((i for i, k in enumerate(kinds) if k == "reasoning_delta"), -1)
    first_content = next((i for i, k in enumerate(kinds) if k == "assistant_delta"), -1)
    check(
        -1 < first_reasoning < first_content,
        "思维链排在正文之前",
        f"首次思维链 #{first_reasoning} / 首次正文 #{first_content}",
    )

    check(
        not reasoning or reasoning not in content,
        "正文里没有混进思维链",
    )

    history = await agent_core.get_history(PLAIN_THREAD)
    restored = history_reasoning(history)
    check(bool(restored), "历史里读得到思维链", f"长度 {len(restored)}")
    check(
        restored.strip() == reasoning.strip(),
        "历史里的思维链与实时收到的逐字相同",
        f"实时 {len(reasoning)} 字 / 历史 {len(restored)} 字",
    )

    print(f"  实时思维链前 60 字：{reasoning[:60]!r}")
    print(f"  正文前 60 字：{content[:60]!r}")

    print("\n用例 2：摘要节点的思维链必须被过滤掉")
    graph = await agent_core._ensure_graph()
    config = agent_core._config(SEEDED_THREAD)
    seeded = [SystemMessage(content=agent_core._system_prompt())]
    for i in range(40):
        seeded.append(HumanMessage(content=f"第 {i} 个问题，都在聊{MARKER}的部署细节。"))
        seeded.append(AIMessage(content=f"第 {i} 个回答，围绕{MARKER}展开。"))
    await graph.aupdate_state(config, {"messages": seeded})
    print(f"  灌入 {len(seeded)} 条历史（40 轮 > 阈值 30，会触发裁剪与摘要）")

    events = await collect(SEEDED_THREAD, "那再补一个简单的问题：一加一等于几？")
    reasoning, content = split(events)

    state = (await graph.aget_state(config)).values
    summary = (state.get("summary") or "").strip()
    check(bool(summary), "摘要确实被触发（否则本用例是空过）", f"{len(summary)} 字")

    check(bool(reasoning), "摘要场景下仍然收到思维链", f"长度 {len(reasoning)}")

    restored = history_reasoning(await agent_core.get_history(SEEDED_THREAD))
    check(
        restored.strip() == reasoning.strip(),
        "实时思维链与历史逐字相同 —— 摘要的思维链没有混进来",
        f"实时 {len(reasoning)} 字 / 历史 {len(restored)} 字",
    )
    print(f"  实时思维链：{reasoning[:120]!r}")

    history = await agent_core.get_history(SEEDED_THREAD)
    last_assistant = [m for m in history if m["role"] == "assistant"][-1]
    stored_content = last_assistant["content"]
    leaked = len(content) - len(stored_content)
    print(f"  [诊断] 实时正文 {len(content)} 字 / 历史正文 {len(stored_content)} 字"
          f" → 差值 {leaked} 字")
    if leaked > 0:
        print(f"  [诊断] 摘要正文泄漏样例：{content[:leaked + 20]!r}")

    for thread_id in (PLAIN_THREAD, SEEDED_THREAD):
        try:
            await agent_core.delete_checkpoint_thread(thread_id)
        except Exception as error:  # noqa: BLE001
            print(f"  清理 {thread_id} 失败（不影响结论）：{error}")
    await agent_core.shutdown()

    print(f"\n失败项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
