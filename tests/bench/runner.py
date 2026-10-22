"""场景执行器：把一条用户提问真正跑过 service 层，边跑边打点。

走 `service.chat_events` 而不是 `agent_core.stream_agent`，因为要测的是
**用户真实路径**——标题模型、记忆注入、位置解析都在这一层，绕开它们
测出来的 TTFT 会好看但失真（eval/run.py 那种绕法是故意的，它测 Agent 本身，
bench 测整个系统，两边的口径必须分开）。

打点全部发生在**消费端**（这里），不改生产代码。代价是「工具执行时间」
只能用 tool_result 与 tool_call 两个事件的时间差来量，它把事件转发的
几毫秒也算进去了——量级远小于工具本身（网络请求几百毫秒起），可接受。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
import metrics  # noqa: E402
import service  # noqa: E402

BENCH_USER_ID = os.getenv("BENCH_USER_ID", "u-671d242584")
TURN_TIMEOUT_S = 180


@dataclass
class Scenario:
    id: str
    title: str
    turns: list[str]
    reasoning: bool = True
    web_search: bool = True
    expect_tools: list[str] = field(default_factory=list)


def load_scenarios(path: Path) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        scenarios.append(
            Scenario(
                id=raw["id"],
                title=raw.get("title", raw["id"]),
                turns=raw["turns"],
                reasoning=raw.get("reasoning", True),
                web_search=raw.get("web_search", True),
                expect_tools=raw.get("expect_tools", []),
            )
        )
    return scenarios


def _turn_messages(messages: list[Any]) -> list[Any]:
    """本轮产生的消息：从末尾往前扫，遇到 HumanMessage（本轮提问）就停。

    不用「记录跑之前的条数再切片」——摘要压缩会删旧消息，条数会偏；
    也不用消息 id 求差集——id 不保证非空。从末尾往回扫到 HumanMessage
    这个锚点，两种情况都不受影响。
    """
    out: list[Any] = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        out.append(message)
    return list(reversed(out))


async def run_turn(
    thread_id: str,
    text: str,
    *,
    reasoning: bool,
    web_search: bool,
) -> dict[str, Any]:
    """跑一轮提问，返回采集层的原始数字。"""
    events = service.chat_events(
        BENCH_USER_ID,
        thread_id,
        text,
        client_location=None,
        memory_enabled=True,
        reasoning=reasoning,
        web_search=web_search,
    )

    started = time.perf_counter()
    title_ms: float | None = None
    first_any_ms: float | None = None
    first_visible_ms: float | None = None
    tool_ms = 0.0
    pending_tools: list[tuple[str, float]] = []
    tool_results: list[dict[str, Any]] = []
    answer_chars = 0
    error: str | None = None

    async def _consume() -> None:
        nonlocal title_ms, first_any_ms, first_visible_ms, tool_ms, answer_chars, error
        async for event in events:
            now = (time.perf_counter() - started) * 1000
            kind = event.get("type")
            if kind == "title":
                if title_ms is None:
                    title_ms = now
            elif kind == "reasoning_delta":
                if first_any_ms is None:
                    first_any_ms = now
            elif kind == "assistant_delta":
                if first_any_ms is None:
                    first_any_ms = now
                if first_visible_ms is None:
                    first_visible_ms = now
                answer_chars += len(str(event.get("content") or ""))
            elif kind == "tool_call":
                for call in event.get("calls") or []:
                    pending_tools.append((str(call.get("name")), now))
            elif kind == "tool_result":
                if pending_tools:
                    name, call_at = pending_tools.pop(0)
                    tool_ms += now - call_at
                    tool_results.append(
                        {
                            "tool": event.get("tool", name),
                            "status": event.get("status"),
                            "ms": round(now - call_at, 1),
                        }
                    )
            elif kind == "error":
                error = str(event.get("message") or event)

    try:
        await asyncio.wait_for(_consume(), timeout=TURN_TIMEOUT_S)
    except asyncio.TimeoutError:
        error = f"超过 {TURN_TIMEOUT_S}s 未完成"
    total_ms = (time.perf_counter() - started) * 1000

    # usage 从 checkpoint 读：跑完后状态里最后一段（回扫到本轮 HumanMessage 为止）
    # 就是本轮的 AIMessage 序列，每条带一次模型调用的 usage_metadata。
    graph = await agent_core.get_graph()
    state = await graph.aget_state(agent_core._config(thread_id))
    messages = state.values.get("messages", [])
    usage = metrics.usage_of_messages(_turn_messages(messages))

    return {
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "model_calls": usage["model_calls"],
        "tool_calls": len(tool_results),
        "tool_results": tool_results,
        "tool_ms": round(tool_ms, 1),
        "title_ms": round(title_ms, 1) if title_ms is not None else None,
        "ttft_ms": round(first_visible_ms, 1) if first_visible_ms is not None else None,
        "ttft_any_ms": round(first_any_ms, 1) if first_any_ms is not None else None,
        "total_ms": round(total_ms, 1),
        "answer_chars": answer_chars,
        "error": error,
    }


async def run_scenario(
    scenario: Scenario,
    *,
    repeat: int = 1,
) -> dict[str, Any]:
    """跑一个场景 repeat 次。每次用全新会话（同前缀 → 缓存可跨次命中）。

    冷/暖的切分不靠 sleep：provider 的前缀缓存只认「请求前缀」，第 1 次跑
    某场景必然全 miss（前缀刚写进去），第 2 次起才可能命中。所以第 1 次
    就是 cold，其余进 warm（见 metrics.split_cold_warm）。
    """
    runs: list[list[dict[str, Any]]] = []
    for run_no in range(repeat):
        thread_id = service.create_conversation(BENCH_USER_ID)["thread_id"]
        turns: list[dict[str, Any]] = []
        try:
            for text in scenario.turns:
                sample = await run_turn(
                    thread_id,
                    text,
                    reasoning=scenario.reasoning,
                    web_search=scenario.web_search,
                )
                sample["run"] = run_no + 1
                sample["turn"] = len(turns) + 1
                turns.append(sample)
        finally:
            await service.delete_conversation(thread_id, BENCH_USER_ID)
        runs.append(turns)

    flat = [turn for run in runs for turn in run]
    errors = [t for t in flat if t.get("error")]
    called = {r["tool"] for t in flat for r in t.get("tool_results", [])}
    missing_tools = [t for t in scenario.expect_tools if t not in called]

    return {
        "scenario": scenario.id,
        "title": scenario.title,
        "reasoning": scenario.reasoning,
        "web_search": scenario.web_search,
        "repeat": repeat,
        "runs": runs,
        "overall": metrics.aggregate_turns(flat),
        "cold_warm": metrics.split_cold_warm(runs),
        "errors": [t["error"] for t in errors],
        "missing_tools": missing_tools,
    }
