"""MCP 韧性逻辑的单测：调用级超时、连续失败熔断、死会话自愈。

不启动真实 MCP 子进程：`_harden` / `_note_failure` / `_ensure` 的重建路径
全部用假工具和假 `_serve` 驱动。真子进程的端到端行为由 e2e 覆盖。
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

import agent_core


class _NoArgs(BaseModel):
    """假工具的空参数 schema（_harden 只是透传它）。"""


def _fake_tool(name: str, inner) -> StructuredTool:
    """构造一个 _harden 能接受的假 MCP 工具。"""
    return StructuredTool(
        name=name, description="fake", args_schema=_NoArgs, coroutine=inner
    )


def _runner_of(ts: agent_core._McpToolset, hardened: list):
    """取 _harden 包出来的 runner（StructuredTool.coroutine 就是它）。"""
    return hardened[0].coroutine


def test_timeout_aborts_hung_call_and_counts_failure(monkeypatch):
    """子进程 deadlock（调用永不返回）→ wait_for 掐掉并记一次失败。"""
    monkeypatch.setattr(agent_core, "_MCP_CALL_TIMEOUT", 0.05)

    async def inner(**kwargs):
        await asyncio.sleep(5)

    ts = agent_core._McpToolset()
    runner = _runner_of(ts, ts._harden([_fake_tool("hung", inner)]))

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(runner())
    assert ts._failures == 1


def test_success_resets_failure_counter():
    """成功一次即清零——熔断只认连续失败。"""

    async def inner(**kwargs):
        return "ok"

    ts = agent_core._McpToolset()
    runner = _runner_of(ts, ts._harden([_fake_tool("ok", inner)]))

    ts._failures = 2
    assert asyncio.run(runner()) == "ok"
    assert ts._failures == 0


def test_breaker_triggers_after_consecutive_failures():
    """连续 3 次失败 → 熔断任务清掉会话状态并复位计数。"""

    async def inner(**kwargs):
        raise RuntimeError("pipe is dead")

    ts = agent_core._McpToolset()
    runner = _runner_of(ts, ts._harden([_fake_tool("dead", inner)]))

    async def scenario():
        for _ in range(agent_core._MCP_BREAKER_THRESHOLD):
            with pytest.raises(RuntimeError):
                await runner()
        assert ts._restarting  # 第 3 次失败已触发熔断
        await asyncio.sleep(0.05)  # 让 breaker 任务跑完
        assert not ts._restarting
        assert ts._failures == 0
        assert ts._tools is None  # 坏会话已丢弃，等下次 _ensure 重建

    asyncio.run(scenario())


def test_breaker_does_not_trigger_on_non_consecutive_failures():
    """失败→成功→失败→失败：计数被打断过，不该熔断。"""

    state = {"n": 0}

    async def inner(**kwargs):
        state["n"] += 1
        if state["n"] != 2:
            raise RuntimeError("boom")
        return "ok"

    ts = agent_core._McpToolset()
    runner = _runner_of(ts, ts._harden([_fake_tool("flaky", inner)]))

    async def scenario():
        for _ in range(4):
            try:
                await runner()
            except RuntimeError:
                pass
        assert not ts._restarting
        assert ts._failures < agent_core._MCP_BREAKER_THRESHOLD

    asyncio.run(scenario())


def test_ensure_rebuilds_after_dead_session_task():
    """会话任务意外退出后，下一次 _ensure 丢弃死引用并重建（自愈主路径）。"""
    ts = agent_core._McpToolset()
    builds: list[int] = []

    async def fake_serve():
        builds.append(1)
        assert ts._ready is not None and ts._stopped is not None
        ts._tools = ["fresh-tools"]
        ts._ready.set()
        await ts._stopped.wait()

    async def scenario():
        # 伪造一个已经死掉的旧会话：任务完结、状态里还挂着死引用
        dead = asyncio.create_task(asyncio.sleep(0))
        await dead
        ts._task = dead
        ts._ready = asyncio.Event()
        ts._ready.set()
        ts._stopped = asyncio.Event()
        ts._tools = ["stale-tools"]
        ts._serve = fake_serve  # type: ignore[method-assign]

        await ts._ensure()
        assert ts._tools == ["fresh-tools"]
        assert builds == [1]

        ts._stopped.set()
        await ts._task

    asyncio.run(scenario())


def test_drop_state_clears_everything():
    ts = agent_core._McpToolset()
    ts._tools = ["t"]
    ts._tools_no_web = ["t"]
    ts._tool_node = object()  # type: ignore[assignment]
    ts._bound_models[(True, True, True)] = object()
    ts._drop_state()
    assert ts._tools is None
    assert ts._tools_no_web is None
    assert ts._tool_node is None
    assert not ts._bound_models
