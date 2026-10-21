"""探针：`get_history` 有没有把工具的成功 / 失败一起带出来。

以前 operations 里只有 `{"kind": "result", "tool": ...}`，`status` 被丢掉了，
于是**刷新页面之后「工具失败」和「工具完成」长得一模一样**——历史里只剩一句
「工具完成：xxx」。前端的实时渲染修好之后，这条路径就成了漏网之鱼。

做法：直接把 `status="error"` 的 ToolMessage 灌进 checkpoint。真去造一个失败
场景要花一次模型调用，而且还得赌模型愿意调那个必然失败的工具——灌数据既便宜
又确定。**只读之外不碰任何真实数据，用完删掉自己造的线程。**
"""
import asyncio
import os
import sys
import uuid

os.environ.setdefault("no_proxy", "*")
sys.path.insert(0, r"D:\agent")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

import agent_core  # noqa: E402

SUFFIX = uuid.uuid4().hex[:6]
FAILURES = 0


def check(condition: bool, label: str, extra: str = "") -> None:
    global FAILURES
    if condition:
        print(f"  [ok]   {label}")
    else:
        print(f"  [FAIL] {label}{(' —— ' + extra) if extra else ''}")
        FAILURES += 1


def tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


async def history_for(messages: list) -> list[dict]:
    thread_id = f"t-probe-status-{SUFFIX}-{uuid.uuid4().hex[:4]}"
    graph = await agent_core._ensure_graph()
    await graph.aupdate_state(agent_core._config(thread_id), {"messages": messages})
    try:
        return await agent_core.get_history(thread_id)
    finally:
        try:
            await agent_core.delete_checkpoint_thread(thread_id)
        except Exception as error:  # noqa: BLE001
            print(f"  清理 {thread_id} 失败（不影响结论）：{error}")


def operations_of(history: list[dict]) -> list[dict]:
    return [
        op
        for message in history
        if message["role"] == "assistant"
        for op in (message.get("operations") or [])
    ]


async def main() -> int:
    print("失败的调用：status 必须是 error")
    history = await history_for([
        SystemMessage(content="（探针）"),
        HumanMessage(content="帮我打开 https://x.invalid"),
        AIMessage(
            content="",
            tool_calls=[tool_call("fetch_webpage", {"url": "https://x.invalid"}, "call_bad")],
        ),
        ToolMessage(
            content="错误：无法解析这个域名",
            tool_call_id="call_bad",
            name="fetch_webpage",
            status="error",
        ),
        AIMessage(content="抱歉，这个网址打不开。"),
    ])
    ops = operations_of(history)
    check([op["kind"] for op in ops] == ["call", "result"], "工具调用和返回都在", str([op["kind"] for op in ops]))
    result_ops = [op for op in ops if op["kind"] == "result"]
    check(bool(result_ops), "有 result 操作")
    if result_ops:
        check(
            result_ops[0].get("status") == "error",
            "失败的返回带 status=error",
            f"实际 {result_ops[0].get('status')!r}",
        )
        check(
            "status" in result_ops[0],
            "status 这个键确实存在（不是靠前端默认值兜的）",
            str(result_ops[0]),
        )

    print("\n成功的调用：status 必须是 success")
    history = await history_for([
        SystemMessage(content="（探针）"),
        HumanMessage(content="北京天气怎么样"),
        AIMessage(
            content="",
            tool_calls=[tool_call("get_weather", {"city": "北京"}, "call_ok")],
        ),
        ToolMessage(
            content="北京 晴 22 度",
            tool_call_id="call_ok",
            name="get_weather",
            status="success",
        ),
        AIMessage(content="北京今天晴，22 度。"),
    ])
    result_ops = [op for op in operations_of(history) if op["kind"] == "result"]
    check(
        result_ops and result_ops[0].get("status") == "success",
        "成功的返回带 status=success",
        f"实际 {result_ops[0].get('status')!r}" if result_ops else "没有 result",
    )

    print("\n没显式给 status：走 langchain 的默认值 success")
    history = await history_for([
        SystemMessage(content="（探针）"),
        HumanMessage(content="现在几点"),
        AIMessage(content="", tool_calls=[tool_call("get_current_time", {}, "call_d")]),
        ToolMessage(content="2026-10-11 10:43", tool_call_id="call_d", name="get_current_time"),
        AIMessage(content="现在是 10:43。"),
    ])
    result_ops = [op for op in operations_of(history) if op["kind"] == "result"]
    check(
        result_ops and result_ops[0].get("status") == "success",
        "省略 status 时读出 success（兼容老 checkpoint）",
        f"实际 {result_ops[0].get('status')!r}" if result_ops else "没有 result",
    )

    await agent_core.shutdown()
    print(f"\n失败项：{FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
