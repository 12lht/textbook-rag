"""探针：`stream_mode="messages"` 播出来的 token，到底是哪个节点说的。

要回答的问题：除了 `model` 节点，图里还有没有**别的节点**在调模型？
有的话，它的输出会不会也被当成「助手的回答」播出去？

为什么必须查：`truncate` 节点是图的入口，**每一轮都会跑**。它内部在需要裁剪时
会 `await model.ainvoke(...)` 生成摘要。如果这个调用的 token 也被 messages 通道
捕获，那么摘要就会混进用户看到的回答里——而且思维链上线后，摘要的思维链还会
混进「思考过程」面板。

做法：不用真聊几十轮（太贵）。先把阈值压到地板、把 20 轮假历史写进 checkpoint，
再跑一轮，让 `truncate` 真的触发一次。整场只花 2 次模型调用。

注意：这里**直接驱动 `graph.astream`**，不走 `stream_agent`——因为要读的是
`metadata` 里的 `langgraph_node`，而 `stream_agent` 还没有把它透出来。
"""
import asyncio
import os
import sys

os.environ.setdefault("no_proxy", "*")
sys.path.insert(0, r"D:\agent")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)

import agent_core  # noqa: E402

THREAD = "t-probe-node-attribution"


async def main() -> int:
    graph = await agent_core._ensure_graph()
    config = agent_core._config(THREAD)

    agent_core.CONTEXT_TRIGGER_TOKENS = 1
    agent_core.CONTEXT_TARGET_TOKENS = 1
    seeded = [SystemMessage(content=agent_core._system_prompt())]
    for i in range(20):
        seeded.append(HumanMessage(content=f"第 {i} 个问题，随便问点什么。"))
        seeded.append(AIMessage(content=f"第 {i} 个回答，随便答点什么。"))
    await graph.aupdate_state(config, {"messages": seeded})
    print(f"灌入历史条数 = {len(seeded)}，轮数 = 20（阈值临时压到 1，必触发裁剪）")

    by_node: dict[str, dict[str, int]] = {}

    try:
        async for mode, chunk in graph.astream(
            {"messages": [HumanMessage(content="那再补一个简单的问题：一加一等于几？")]},
            config=config,
            context=agent_core.Context(user_id="1", thread_id=THREAD, memory_enabled=True),
            stream_mode=["messages", "updates"],
        ):
            if mode != "messages":
                continue
            message_chunk, metadata = chunk
            if not isinstance(message_chunk, AIMessageChunk):
                continue
            node = str(metadata.get("langgraph_node", "?"))
            slot = by_node.setdefault(node, {"chunks": 0, "content": 0, "reasoning": 0})
            slot["chunks"] += 1
            if isinstance(message_chunk.content, str):
                slot["content"] += len(message_chunk.content)
            slot["reasoning"] += len(
                (message_chunk.additional_kwargs or {}).get("reasoning_content") or ""
            )
    finally:
        try:
            await agent_core.delete_checkpoint_thread(THREAD)
        except Exception as error:  # noqa: BLE001
            print(f"清理线程失败（不影响结论）：{error}")
        await agent_core.shutdown()

    print("\n各节点播出的分片与字符数：")
    leaked = []
    for node, slot in sorted(by_node.items(), key=lambda kv: -kv[1]["chunks"]):
        print(
            f"  {node:10s} 分片 {slot['chunks']:4d}"
            f"  正文 {slot['content']:6d} 字  思维链 {slot['reasoning']:5d} 字"
        )
        if node != "model":
            leaked.append(node)

    print()
    if leaked:
        print(f"[发现] 非 model 节点也在播 token：{leaked}")
        print("       不按节点过滤的话，摘要会被当成助手的回答 / 思考过程。")
    else:
        print("[结论] 只有 model 节点在播 token，不需要按节点过滤。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
