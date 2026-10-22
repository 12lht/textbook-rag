"""端到端验证两个开关：真的跑一轮图，看事件流里有没有对应的东西。

上面那个探针只验到「装配对了」，但用户看到的是另一回事：
  - 关深度思考 → 思考过程那块不该再出现；
  - 关联网搜索 → 模型不该再去调 web_search。
这两件事只有真跑一遍才算数。

**先把 checkpoint 指到临时目录**：默认路径是生产库，探针跑一轮就会往里
写真实会话（踩过两次）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"D:/agent")
from dotenv import load_dotenv

load_dotenv(r"D:/agent/.env")

TMP = Path(r"D:/agent/.workbuddy/tmp/toggle-e2e")
TMP.mkdir(parents=True, exist_ok=True)

import agent_core

agent_core.CHECKPOINT_PATH = TMP / "checkpoints.sqlite"


def check(label, condition):
    print(f"{'通过' if condition else '不通过'}  {label}")
    return bool(condition)


async def run(thread: str, text: str, **flags) -> dict:
    kinds = {"reasoning": 0, "tools": [], "answer": ""}
    async for event in agent_core.stream_agent("probe-user", thread, text, **flags):
        if event["type"] == "reasoning_delta":
            kinds["reasoning"] += 1
        elif event["type"] == "tool_call":
            kinds["tools"].extend(call["name"] for call in event.get("calls", []))
        elif event["type"] == "assistant_delta":
            kinds["answer"] += event["content"]
    return kinds


async def main():
    ok = True

    QUESTION = "用一句话说明什么是可重构智能表面（RIS）。"

    print("--- 1. 深度思考：开（默认）---")
    on = await run("t-on", QUESTION)
    print(f"思维链分片 {on['reasoning']} 个，回答 {len(on['answer'])} 字")
    ok &= check("默认会吐思维链", on["reasoning"] > 0)

    print("\n--- 2. 深度思考：关（同一个问题）---")
    off = await run("t-off", QUESTION, reasoning=False)
    print(f"思维链分片 {off['reasoning']} 个，回答 {len(off['answer'])} 字")
    ok &= check("关掉后不再吐思维链", off["reasoning"] == 0)
    ok &= check("关掉后照样有正文回答", len(off["answer"]) > 0)
    ok &= check("开关确实造成了差别（不是问题本身没有思维链）", on["reasoning"] > 0)

    print("\n--- 3. 联网搜索：关（问一个明显该联网的问题）---")
    no_web = await run("t-noweb", "搜索一下今天最重要的科技新闻", web_search=False)
    print("调的工具：", no_web["tools"] or "（一个没调）")
    ok &= check("关掉后不会调 web_search", "web_search" not in no_web["tools"])
    ok &= check("关掉后也不会调 web_extract", "web_extract" not in no_web["tools"])

    await agent_core.shutdown()
    print("\n全部通过" if ok else "\n有未通过项")


if __name__ == "__main__":
    asyncio.run(main())
