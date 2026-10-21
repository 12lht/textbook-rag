"""探针：摘要会不会被模型原样复述？做个有效对照。

两组给模型的是**同样的事实**，唯一变量是「事实从哪里来」：
  A 组（对照）：事实躺在正常的消息历史里
  B 组：事实被压成摘要，挂在系统提示后面

如果两组都在开头复述，说明这只是这个模型「先复述再说」的习惯，
不是摘要块独有的毛病；如果只有 B 组复述，那就是摘要的放法有问题。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent_core

SUMMARY = "用户的猫叫豆豆，今年3岁，是一只橘猫。用户下周要去西安出差。"
SMALL_TALK = "今天天气不错。"


async def ask(summary: str | None, history: bool) -> str:
    messages = [SystemMessage(content=agent_core.SYSTEM_PROMPT)]
    if history:
        messages += [
            HumanMessage(content="我的猫叫豆豆，今年3岁，是一只橘猫。"),
            AIMessage(content="好的，记住了。"),
            HumanMessage(content="我下周要去西安出差。"),
            AIMessage(content="好的，西安出差。"),
        ]
    messages.append(HumanMessage(content=SMALL_TALK))
    state = {"messages": messages}
    if summary:
        state["summary"] = summary
    payload = agent_core._messages_for_model(state)
    response = await agent_core.model.ainvoke(payload)
    return agent_core._plain_text(response.content).strip()


def leaks(text: str) -> list[str]:
    return [word for word in ("豆豆", "橘猫", "西安") if word in text]


async def main() -> None:
    print(f"两组问的都是同一句：{SMALL_TALK!r}\n")

    print("===== A 组：事实在正常历史里 =====")
    plain = await ask(None, history=True)
    print(plain[:220])
    print()

    print("===== B 组：事实被压成摘要 =====")
    with_summary = await ask(SUMMARY, history=False)
    print(with_summary[:220])
    print()

    a_hits, b_hits = leaks(plain), leaks(with_summary)
    print(f"A 组复述的词：{a_hits or '（无）'}")
    print(f"B 组复述的词：{b_hits or '（无）'}")
    print()
    if a_hits and b_hits:
        print("结论：这个模型本来就会『先把上下文念一遍再回答』，摘要只是换了素材来源。")
    elif not a_hits and b_hits:
        print("结论：只有摘要组复述 —— 是摘要的放法有问题，要改呈现方式。")
    elif a_hits and not b_hits:
        print("结论：摘要反而更不容易被复述，说明现在的措辞是有效的。")
    else:
        print("结论：两组都没复述，之前那次应该是采样噪声。")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
