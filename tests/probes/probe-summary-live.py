"""探针：用真实模型跑一次 summarize_history，看摘要有没有留住早期事实。

只发**一次**小调用（几百 token），不是完整评测——完整的长对话准确率对比按用户要求
留到后面单独做。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent_core

FACTS = {
    "城市": "深圳",
    "专业": "信息工程",
    "目标": "保研",
    "婚期": "明年三月",
    "忌口": "不吃香菜",
}
NOISE = ["今天天气不错", "嗯嗯", "好的谢谢", "哈哈哈"]

TRANSCRIPT = [
    HumanMessage(content="我在深圳，最近想换一台机械键盘，预算 800 左右。"),
    AIMessage(content="深圳的话可以看看本地的实体店，800 预算选择不少。"),
    HumanMessage(content="我是信息工程专业的，平时写代码比较多，想要 87 键的。"),
    AIMessage(content="87 键适合写代码，省地方。"),
    HumanMessage(content="对了我不吃香菜，等下顺便帮我看看附近有没有推荐的餐厅。"),
    AIMessage(content="记下了，你不吃香菜。"),
    HumanMessage(content="我明年三月结婚，想在那之前把保研的事情搞定。"),
    AIMessage(content="恭喜！那时间上要提前规划。"),
    HumanMessage(content="嗯嗯"),
    AIMessage(content="好的谢谢"),
    HumanMessage(content="哈哈哈"),
    AIMessage(content="还有什么想聊的？"),
    HumanMessage(content="帮我查下今天深圳的天气吧。"),
    AIMessage(
        content="",
        tool_calls=[{"name": "get_weather_for_location", "args": {"city": "深圳"}, "id": "c1"}],
    ),
    ToolMessage(
        content="广东省深圳市当前天气：多云，气温 26°C，湿度 70%。",
        tool_call_id="c1",
        name="get_weather_for_location",
    ),
    AIMessage(content="深圳今天多云，26 度，挺舒服的。"),
]


async def main() -> int:
    print(f"喂进去 {len(TRANSCRIPT)} 条消息，里面埋了 {len(FACTS)} 个事实和 {len(NOISE)} 条寒暄\n")

    summary = await agent_core.summarize_history(None, TRANSCRIPT)
    if not summary:
        print("✗ 摘要是空的")
        return 1

    print("===== 模型产出的摘要 =====")
    print(summary)
    print("==========================\n")

    ok = True
    print("该留住的：")
    for label, value in FACTS.items():
        hit = value in summary
        ok &= hit
        print(f"   {'✓' if hit else '✗'} {label}：{value}")

    print("\n该扔掉的（寒暄，留着也不算错，只是看看）：")
    for text in NOISE:
        print(f"   {'○' if text not in summary else '●'} {text}")

    print(f"\n摘要长度：{len(summary)} 字符（上限 {agent_core.MAX_SUMMARY_CHARS}）")
    print("\n结果:", "事实都留住了" if ok else "有事实丢失")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
