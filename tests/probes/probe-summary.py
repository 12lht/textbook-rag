"""探针：验证记忆摘要化的裁剪与累积逻辑（不打真实模型，零成本）。

用假模型替换 agent_core.model，把注意力放在「裁剪决策 + 时机 + 摘要累积 +
降级路径」四件事上。摘要文案质量本身由另一支真实调用的小探针看。

**判据是 token，不是轮数**（2026-10-22 改）。所以这里最该看的三条是：
「同样 60 轮、聊得很轻的会话不该被裁」「同样 20 轮、检索很重的会话该被裁」
「到了预警线但任务正跑着 → 让这一轮跑完再压」。按轮裁的时候这三条都不存在。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import agent_core
from agent_core import (
    CONTEXT_LIMIT_TOKENS,
    CONTEXT_TARGET_TOKENS,
    CONTEXT_TRIGGER_TOKENS,
    MAX_KEPT_MESSAGES,
    _estimate_tokens,
    _messages_for_model,
    truncate_node,
)

# 一轮「重」聊天（一条消息 2400 字）：60 轮约 17 万 token，能压过预警线。
HEAVY_CHARS = 2400
# 一轮「轻」聊天：60 轮才两千多 token，怎么都不该被裁。
LIGHT_CHARS = 20


class StubModel:
    """替身：记录收到的提示词，可以按需失败。"""

    def __init__(self, fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.fail = fail

    async def ainvoke(self, prompt: str):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("摘要模型炸了")
        return AIMessage(content=f"第 {len(self.prompts)} 次摘要")


def build_state(
    turns: int, chars: int = LIGHT_CHARS, system: bool = True, summary: str | None = None
) -> dict:
    """纯聊天的会话：一轮 = 用户一条 + 助手一条 = 2 条。

    `chars` 控制每条消息的字数——按 token 裁之后，「同样轮数、体量不同」才是
    真正要区分的东西。
    """
    filler = "字" * chars
    messages = []
    if system:
        messages.append(SystemMessage(content=agent_core.SYSTEM_PROMPT, id="sys"))
    for index in range(turns):
        messages.append(HumanMessage(content=f"第 {index} 问 {filler}", id=f"h{index}"))
        messages.append(AIMessage(content=f"第 {index} 答 {filler}", id=f"a{index}"))
    state = {"messages": messages}
    if summary is not None:
        state["summary"] = summary
    return state


def build_tool_state(
    turns: int,
    calls_per_turn: int = 1,
    chars: int = 300,
    *,
    next_turn: bool = True,
) -> dict:
    """工具密集的会话。

    一轮 = 用户一条 + `calls_per_turn` 组「AI 调工具 + 工具返回」，
    也就是 `1 + 2 * calls_per_turn` 条消息。收尾的答复这里不补，不影响分布。

    `next_turn=True` 会在末尾补一条用户消息，模拟「上一轮答完、用户又问了」。
    这不是凑数：真实的裁剪就发生在那个瞬间（图入口），此时最后一条是人类消息，
    属于轮边界；不补的话最后一条是工具结果，按设计会被判定成「任务中途」而不压。
    """
    chunk = "循" * chars
    messages = [SystemMessage(content=agent_core.SYSTEM_PROMPT, id="sys")]
    for index in range(turns):
        messages.append(HumanMessage(content=f"第 {index} 问", id=f"h{index}"))
        for step in range(calls_per_turn):
            messages.append(
                AIMessage(
                    content="",
                    id=f"a{index}-{step}",
                    tool_calls=[
                        {"name": "search_textbook", "args": {"query": "循环前缀"}, "id": f"c{index}-{step}"}
                    ],
                )
            )
            messages.append(
                ToolMessage(
                    content=chunk,
                    id=f"t{index}-{step}",
                    tool_call_id=f"c{index}-{step}",
                    name="search_textbook",
                )
            )
    if next_turn:
        messages.append(HumanMessage(content="下一轮开始", id="h-next"))
    return {"messages": messages}


def total_tokens(state) -> int:
    """整段会话的估算总量，口径和 `truncate_node` 一致（含固定开销）。"""
    total = agent_core._prompt_overhead_tokens() + _estimate_tokens(state.get("summary") or "")
    for message in state["messages"]:
        total += agent_core._message_tokens(message)
    return total


def human_count(messages) -> int:
    return sum(1 for m in messages if isinstance(m, HumanMessage))


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return condition


def dropped_of(state, update) -> list:
    gone = {item.id for item in update.get("messages", [])}
    return [m for m in state["messages"] if m.id in gone]


async def main() -> int:
    ok = True
    print(f"   预警线 {CONTEXT_TRIGGER_TOKENS} / 硬顶 {CONTEXT_LIMIT_TOKENS}"
          f" / 目标线 {CONTEXT_TARGET_TOKENS} / 条数硬顶 {MAX_KEPT_MESSAGES}")

    print()
    print("== 1. 没到预警线就什么都不做（轮数多也不算数）==")
    agent_core.model = StubModel()
    state = build_state(turns=60, chars=LIGHT_CHARS)  # 按轮的旧逻辑：60 轮 > 30，早裁了
    tokens = total_tokens(state)
    update = await truncate_node(state)
    print(f"   60 轮纯聊天，约 {tokens} token")
    ok &= check("返回空更新", update == {}, f"update={update}")
    ok &= check("没调摘要模型", agent_core.model.prompts == [])

    print()
    print("== 2. 轮数一样，但一条消息特别长 —— 该裁 ==")
    agent_core.model = StubModel()
    heavy = build_state(turns=60, chars=HEAVY_CHARS)
    tokens = total_tokens(heavy)
    update = await truncate_node(heavy)
    dropped = dropped_of(heavy, update)
    kept = [m for m in heavy["messages"] if m not in dropped]
    kept_tokens = total_tokens({"messages": kept, "summary": update.get("summary") or ""})
    print(f"   60 轮 × 一条 {HEAVY_CHARS} 字，约 {tokens} token -> 裁后约 {kept_tokens} token"
          f"（留 {human_count(kept)} 轮）")
    ok &= check("确实裁了", len(dropped) > 0)
    ok &= check("裁后落进目标线", kept_tokens <= CONTEXT_TARGET_TOKENS,
                f"{kept_tokens} <= {CONTEXT_TARGET_TOKENS}")
    ok &= check("只调了一次摘要模型", len(agent_core.model.prompts) == 1)
    ok &= check("写入了 summary 字段", update.get("summary") == "第 1 次摘要")

    print()
    print("== 3. 轮数少但检索很重 —— 也该裁（按轮的旧逻辑会漏掉）==")
    agent_core.model = StubModel()
    tool_state = build_tool_state(turns=20, calls_per_turn=3, chars=4500)
    tokens = total_tokens(tool_state)
    update = await truncate_node(tool_state)
    kept_tool = [m for m in tool_state["messages"] if m not in dropped_of(tool_state, update)]
    print(f"   20 轮 × 3 次检索，约 {tokens} token -> 保留 {human_count(kept_tool)} 轮")
    ok &= check("确实裁了", len(dropped_of(tool_state, update)) > 0)
    ok &= check("保留的轮数比 20 少（没有按轮数放行）", human_count(kept_tool) < 20)

    print()
    print("== 4. 迟滞：压完不会再压一次 ==")
    agent_core.model = StubModel()
    restored = {"messages": kept, "summary": update.get("summary")}
    again = await truncate_node(restored)
    ok &= check("紧接着再跑一次是空更新", again == {})
    ok &= check("没有再次调用摘要模型", agent_core.model.prompts == [])

    print()
    print("== 5. 条数硬顶：token 没到顶但条数太多时，还要往前丢整轮 ==")
    agent_core.model = StubModel()
    lots = build_tool_state(turns=250, calls_per_turn=10, chars=5)
    tokens = total_tokens(lots)
    update = await truncate_node(lots)
    kept = [m for m in lots["messages"] if m not in dropped_of(lots, update)]
    tokens_after = total_tokens({"messages": kept, "summary": update.get("summary") or ""})
    print(f"   250 轮 × 10 次检索（{tokens} token，{len(lots['messages'])} 条）"
          f"-> 保留 {len(kept)} 条 / {human_count(kept)} 轮 / 约 {tokens_after} token")
    ok &= check("保留条数落到硬顶之内", len(kept) <= MAX_KEPT_MESSAGES, f"{len(kept)} 条")
    ok &= check("同时也在目标线以内", tokens_after <= CONTEXT_TARGET_TOKENS)
    ok &= check("token 明显低于目标线（说明是条数把它压下来的）",
                tokens_after < CONTEXT_TARGET_TOKENS * 0.8, f"{tokens_after}")
    ok &= check("至少还留着一轮", human_count(kept) >= 1)

    print()
    print("== 6. 摘要要累积：第二次把上一次的摘要一起传进去 ==")
    agent_core.model = StubModel()
    state = build_state(turns=60, chars=HEAVY_CHARS, summary="上一次的摘要")
    await truncate_node(state)
    ok &= check("提示词里带了上一次的摘要", agent_core.model.prompts[0].find("上一次的摘要") >= 0)

    print()
    print("== 7. 摘要失败：照常裁剪，但不动旧摘要 ==")
    agent_core.model = StubModel(fail=True)
    state = build_state(turns=60, chars=HEAVY_CHARS, summary="旧的摘要")
    update = await truncate_node(state)
    ok &= check("仍然裁掉了消息", "messages" in update and len(update["messages"]) > 0)
    ok &= check("没有覆盖 summary 字段", "summary" not in update)

    print()
    print("== 8. 摘要模型的输入里，工具返回的 content-block 被摊平了 ==")
    agent_core.model = StubModel()
    # 这三条要放在**最前面**，才会落进被裁掉的那一批里（裁剪是从前面丢的）。
    head = [
        SystemMessage(content=agent_core.SYSTEM_PROMPT, id="sys"),
        HumanMessage(content="查一下循环前缀", id="hx"),
        AIMessage(
            content="",
            id="ax",
            tool_calls=[{"name": "search_textbook", "args": {}, "id": "c1"}],
        ),
        ToolMessage(
            content=[{"type": "text", "text": "循环前缀要大于最大时延扩展"}],
            id="tx",
            tool_call_id="c1",
            name="search_textbook",
        ),
    ]
    filler = build_state(turns=60, chars=HEAVY_CHARS)["messages"][1:]
    update = await truncate_node({"messages": head + filler})
    dropped_ids = {item.id for item in update["messages"]}
    ok &= check("那三条工具消息确实被裁掉了", {"hx", "ax", "tx"} <= dropped_ids)
    prompt = agent_core.model.prompts[0]
    ok &= check("看到了工具结果原文", "循环前缀要大于最大时延扩展" in prompt)
    ok &= check("没有混进 content-block 的 JSON 包装", '"type": "text"' not in prompt)

    print()
    print("== 9. 发消息时摘要并进系统消息，且不写回 state ==")
    state = build_state(turns=2, summary="用户在研究 OFDM，偏好中文回答")
    original_first = state["messages"][0]
    payload = _messages_for_model(state)
    ok &= check("第一条是 SystemMessage", isinstance(payload[0], SystemMessage))
    ok &= check("内容里带上了摘要", "用户在研究 OFDM" in str(payload[0].content))
    ok &= check("原 state 的第一条没被改", state["messages"][0] is original_first)
    ok &= check("消息条数不变", len(payload) == len(state["messages"]))

    print()
    print("== 10. 没有摘要时不动系统消息 ==")
    bare = build_state(turns=2)
    payload = _messages_for_model(bare)
    ok &= check("第一条还是原来那条", payload[0] is bare["messages"][0])

    print()
    print("== 11. 切点必须落在人类消息上（不能留下孤立的 ToolMessage） ==")
    agent_core.model = StubModel()
    big = build_tool_state(turns=60, calls_per_turn=2, chars=2400)
    update = await truncate_node(big)
    kept = [m for m in big["messages"] if m not in dropped_of(big, update)]
    ok &= check("保留部分以 HumanMessage 开头", isinstance(kept[1], HumanMessage))
    ok &= check("不是孤立的 ToolMessage 打头", not isinstance(kept[1], ToolMessage))

    print()
    print("== 12. 时机：任务中途不压，等轮边界；撞硬顶才中途压 ==")
    # 12a：过预警线但最后一条是工具结果 = 任务正跑着，应该等
    agent_core.model = StubModel()
    running = build_tool_state(turns=20, calls_per_turn=3, chars=4500, next_turn=False)
    tokens = total_tokens(running)
    update = await truncate_node(running)
    print(f"   任务中途，约 {tokens} token（预警 {CONTEXT_TRIGGER_TOKENS}，硬顶 {CONTEXT_LIMIT_TOKENS}）")
    ok &= check("过了预警线但因任务未结束而不压", update == {}, f"update={update}")
    ok &= check("也没调摘要模型", agent_core.model.prompts == [])
    # 12b：同一条会话，下一轮开始（补一条用户消息）→ 这时才压
    running["messages"].append(HumanMessage(content="这一轮问完了，再问一个", id="h-next-2"))
    update = await truncate_node(running)
    ok &= check("到了轮边界就压了", len(update.get("messages", [])) > 0)
    ok &= check("压完落进目标线",
                total_tokens({"messages": [m for m in running["messages"] if m not in dropped_of(running, update)]})
                <= CONTEXT_TARGET_TOKENS)
    # 12c：超过硬顶，即使任务中途也压（安全阀，正常不该走到）
    agent_core.model = StubModel()
    urgent = build_tool_state(turns=32, calls_per_turn=3, chars=4500, next_turn=False)
    tokens = total_tokens(urgent)
    update = await truncate_node(urgent)
    print(f"   任务中途，约 {tokens} token（已过硬顶 {CONTEXT_LIMIT_TOKENS}）")
    ok &= check("超过硬顶就立刻压，不等轮边界", len(update.get("messages", [])) > 0)

    print()
    print("结果:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
