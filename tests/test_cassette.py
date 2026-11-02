"""录制回放：record / replay / 未命中 / off 四条路径。

只调工具，不调模型 → 零 LLM token 消耗，但要真实联网（web_search）。

关键点：MCP 子进程的环境变量是在**会话建立时**从父进程继承的，所以要换模式就得
先关掉旧会话（`agent_core.shutdown()` 会把 MCP 客户端作废），再让下次调用重新拉起
子进程。这也是 eval/run.py 一次只跑一种模式的原因。

原 `tests/probes/probe-cassette.py`，2026-11-03 迁到 pytest。
**保持单测试**：MCP 会话和事件循环强绑定，拆成多个 `asyncio.run` 会报
"Event loop is closed"（Windows 上尤其明显），且模式切换本来就有顺序。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import agent_core

QUERY_A = "录制回放探针 固定查询 A"
QUERY_B = "录制回放探针 固定查询 B"

pytestmark = pytest.mark.requires_network


async def call_search(query: str) -> str:
    """走一遍 agent 实际用的路径，把工具输出取成纯文本。

    MCP 适配器不会原样返回字符串，而是包成 content-block 列表
    `[{"type": "text", "text": ..., "id": "lc_<uuid>"}]`。cassette 是在**工具函数内部**
    录的，存的是工具的原始返回值，所以比较时要先把包装剥掉。
    """
    tools = {tool.name: tool for tool in await agent_core.load_tools()}
    result = await tools["web_search"].ainvoke({"query": query, "topic": "general"})
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "".join(
            block.get("text", "")
            for block in result
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return json.dumps(result, ensure_ascii=False)


async def use_mode(cassette: Path, mode: str) -> None:
    """切换模式。必须先 shutdown，子进程才会拿到新的环境变量。"""
    await agent_core.shutdown()
    os.environ["TOOL_CASSETTE_MODE"] = mode
    os.environ["TOOL_CASSETTE_PATH"] = str(cassette)


def counters(cassette: Path) -> dict:
    if not cassette.exists():
        return {"records": 0, "hits": 0, "misses": 0}
    return json.loads(cassette.read_text(encoding="utf-8")).get("counters", {})


@pytest.mark.requires_network
def test_cassette_record_replay_roundtrip(tmp_path):
    cassette = tmp_path / "cassette.json"
    try:
        asyncio.run(_roundtrip(cassette))
    finally:
        try:
            asyncio.run(agent_core.shutdown())
        except Exception:  # noqa: BLE001 — 清理阶段的失败不该盖住真正的断言失败
            pass


async def _roundtrip(cassette: Path) -> None:
    # 1. record：真实调用并落盘
    await use_mode(cassette, "record")
    value_a = await call_search(QUERY_A)
    assert cassette.exists(), "record 模式没写出 cassette 文件"
    assert counters(cassette).get("records") == 1, counters(cassette)

    # 2. record 再来一次：不重复写（只增不改）
    await use_mode(cassette, "record")
    await call_search(QUERY_A)
    assert counters(cassette).get("records") == 1, f"同一个键被重复录制：{counters(cassette)}"

    # 3. replay：命中的返回值和录制时逐字一致
    await use_mode(cassette, "replay")
    replayed = await call_search(QUERY_A)
    assert replayed == value_a, "replay 拿到的内容和录制时不一致"
    assert counters(cassette).get("hits") == 1, counters(cassette)

    # 4. replay 未命中：照常真实调用，计入 misses，且不写进 cassette
    await use_mode(cassette, "replay")
    live = await call_search(QUERY_B)
    assert counters(cassette).get("misses") == 1, counters(cassette)
    assert len(live) > 0, "未命中时没拿到真实结果"
    interactions = json.loads(cassette.read_text(encoding="utf-8"))["interactions"]
    assert len(interactions) == 1, f"未命中被写进 cassette 了：{len(interactions)} 条"

    # 5. off：不读不写
    await use_mode(cassette, "off")
    before = counters(cassette)
    await call_search(QUERY_A)
    assert counters(cassette) == before, f"off 模式改了计数：{before} -> {counters(cassette)}"

    # 6. 再 replay 一次：跨会话拿到的内容仍然逐字一致
    await use_mode(cassette, "replay")
    again = await call_search(QUERY_A)
    assert again == replayed, "第二次 replay 与第一次不一致"
