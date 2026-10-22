"""探针：验证「模型自己决定什么时候记」这套记忆工具（零模型调用）。

测四件事：
  1. 后端没注入时不注册工具、系统提示不变；
  2. 注入后两个工具挂上，且排在原有 5 个工具之后（不打乱原顺序）；
  3. 模型看到的那张「名片」里没有 runtime / user_id（框架塞的，模型看不见）；
  4. 真的走一遍 ToolNode：user_id 从 Context 上来、replaces 能作废旧条目、
     后端没挂时如实说不可用；并发写真不丢数据（含冷启动）。

第 4 项之外都用假后端，不碰 data/memories.sqlite3；只有第 11 节用真存储，
且全部写到 .workbuddy/tmp/ 下的临时库。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.prebuilt import ToolNode

import agent_core
from agent_core import Context, SYSTEM_PROMPT

BASE_TOOLS = [
    "get_weather_for_location",
    "get_user_location",
    "get_current_time",
    "web_search",
    "web_extract",
]
MEMORY_TOOLS = ["recall_memory", "save_memory"]


class StubBackend:
    """假存储：按 user_id 分开存（真实现就是这么做的），并记录被调用的参数。"""

    def __init__(self) -> None:
        self.store: dict[str, list[dict]] = {}
        self.writes: list[tuple] = []
        self.forgets: list[tuple] = []
        self.forget_returns = True

    @property
    def entries(self) -> list[dict]:
        """默认用户的条目，测试里最常用。"""
        return self.store.setdefault("u-1", [])

    def read(self, user_id: str) -> list[dict]:
        return list(self.store.get(user_id, []))

    def write(self, user_id: str, facts: list, source=None) -> dict:
        self.writes.append((user_id, facts, source))
        entries = self.store.setdefault(user_id, [])
        added = 0
        for fact in facts:
            if not any(entry.get("text") == fact.get("text") for entry in entries):
                entries.append(
                    {"text": fact.get("text"), "kind": fact.get("kind") or "其他"}
                )
                added += 1
        return {"added": added, "merged": len(facts) - added, "total": len(entries)}

    def forget(self, user_id: str, text: str) -> bool:
        self.forgets.append((user_id, text))
        if not self.forget_returns:
            return False
        entries = self.store.get(user_id, [])
        kept = [e for e in entries if e.get("text") != text]
        if len(kept) == len(entries):
            return False
        self.store[user_id] = kept
        return True


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return condition


def runtime_for(user_id: str = "u-1", thread_id: str = "t-1") -> ToolRuntime:
    """手工造一个 ToolRuntime，把 Context 塞进去——和线上框架做的同一件事。"""
    return ToolRuntime(
        state={},
        context=Context(user_id=user_id, thread_id=thread_id),
        config={},
        stream_writer=None,
        tool_call_id=None,
        store=None,
    )


async def call(tool, args: dict, runtime: ToolRuntime | None = None) -> str:
    """走 ToolNode 调一次工具，取回它给模型的那段文本。"""
    node = ToolNode([tool])
    out = await node.ainvoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": tool.name, "args": args, "id": "c1"}],
                )
            ]
        },
        runtime=runtime or runtime_for(),
    )
    return out["messages"][0].content


async def main() -> int:
    ok = True

    print("== 1. 没注入后端：工具不注册，系统提示逐字不变（评测环境） ==")
    agent_core.set_memory_backend(read=None, write=None, forget=None)
    await agent_core.close_mcp()  # 强制按当前状态重建工具集
    names = [tool.name for tool in await agent_core.load_tools()]
    ok &= check("工具集正好是原来那 5 个", names == BASE_TOOLS, f"{names}")
    ok &= check("系统提示 == SYSTEM_PROMPT", agent_core._system_prompt() == SYSTEM_PROMPT)

    print()
    print("== 2. 注入后端：两个工具挂上，排在原有 5 个之后 ==")
    backend = StubBackend()
    agent_core.set_memory_backend(
        read=backend.read, write=backend.write, forget=backend.forget
    )
    await agent_core.close_mcp()
    tools = await agent_core.load_tools()
    names = [tool.name for tool in tools]
    ok &= check("一共 7 个工具", names == BASE_TOOLS + MEMORY_TOOLS, f"{names}")
    ok &= check("前 5 个的顺序一点没动", names[:5] == BASE_TOOLS)
    ok &= check("系统提示多了记忆说明", agent_core.MEMORY_GUIDE in agent_core._system_prompt())

    print()
    print("== 3. 模型看到的名片：没有 runtime / user_id ==")
    by_name = {tool.name: tool for tool in tools}
    save_schema = convert_to_openai_tool(by_name["save_memory"])["function"]
    recall_schema = convert_to_openai_tool(by_name["recall_memory"])["function"]
    props = save_schema["parameters"]["properties"]
    print(f"   save_memory 的参数：{sorted(props)}  必填：{save_schema['parameters'].get('required')}")
    ok &= check("参数正好是 text / kind / replaces", sorted(props) == ["kind", "replaces", "text"])
    ok &= check("只有 text 必填", save_schema["parameters"].get("required") == ["text"])
    ok &= check("模型看不见 runtime", "runtime" not in props)
    ok &= check("模型看不见 user_id", "user_id" not in props)
    ok &= check("recall_memory 一个参数都没有", recall_schema["parameters"]["properties"] == {})
    ok &= check("docstring 原样当描述发出去", "长期" in save_schema["description"])

    print()
    print("== 4. 真走一遍 ToolNode：写入 ==")
    message = await call(agent_core.save_memory, {"text": "不吃香菜", "kind": "偏好"})
    print(f"   工具返回：{message}")
    ok &= check("后端收到了 user_id", backend.writes[-1][0] == "u-1")
    ok &= check("后端收到了来源会话", backend.writes[-1][2] == "t-1")
    ok &= check("事实形状是 [{text, kind}]", backend.writes[-1][1] == [{"text": "不吃香菜", "kind": "偏好"}])
    ok &= check("落盘了一条", backend.entries == [{"text": "不吃香菜", "kind": "偏好"}])

    print()
    print("== 5. 真走一遍 ToolNode：读取 ==")
    message = await call(agent_core.recall_memory, {})
    print(f"   工具返回：{message!r}")
    ok &= check("读到了刚写的那条", "不吃香菜" in message)
    ok &= check("读不到别人的（换个 user_id 就是空）", "还没有" in await call(agent_core.recall_memory, {}, runtime_for("u-2")))

    print()
    print("== 6. replaces：先删后写，能把改了主意的旧条目作废 ==")
    message = await call(
        agent_core.save_memory, {"text": "现在能吃辣了", "replaces": "不吃香菜"}
    )
    print(f"   工具返回：{message}")
    ok &= check("forget 收到了旧原文", backend.forgets[-1] == ("u-1", "不吃香菜"))
    texts = [entry["text"] for entry in backend.entries]
    ok &= check("旧条目没了、新条目在", texts == ["现在能吃辣了"], f"{texts}")
    ok &= check("返回里说明了作废了哪条", "作废" in message)

    print()
    print("== 7. replaces 没匹配上要明说，不能静默失败 ==")
    backend.forget_returns = False
    message = await call(agent_core.save_memory, {"text": "爱吃甜", "replaces": "不吃甜"})
    print(f"   工具返回：{message}")
    ok &= check("提示了没找到旧记录", "没找到" in message)
    ok &= check("新条目照样写进去了", "爱吃甜" in [e["text"] for e in backend.entries])

    print()
    print("== 8. 后端中途被摘掉：如实说不可用，不假装成功 ==")
    agent_core.set_memory_backend(read=None, write=None, forget=None)
    message = await call(agent_core.save_memory, {"text": "随便一句"})
    ok &= check("save_memory 说不可用", "未启用" in message, f"{message!r}")
    message = await call(agent_core.recall_memory, {})
    ok &= check("recall_memory 说不可用", "未启用" in message, f"{message!r}")

    print()
    print("== 9. 空文本不让写 ==")
    agent_core.set_memory_backend(
        read=backend.read, write=backend.write, forget=backend.forget
    )
    before = len(backend.writes)
    message = await call(agent_core.save_memory, {"text": "   "})
    ok &= check("回了「没有可记的内容」", "没有可记" in message, f"{message!r}")
    ok &= check("没调用后端", len(backend.writes) == before)

    print()
    print("== 10. kind 有枚举约束：模型造不出新词 ==")
    kind_schema = props["kind"]
    enum = next((item["enum"] for item in kind_schema["anyOf"] if "enum" in item), None)
    print(f"   kind 的 enum：{enum}")
    ok &= check("schema 里带上了 4 个合法取值", enum == ["偏好", "身份", "计划", "事实"])
    rejected = await call(agent_core.save_memory, {"text": "x", "kind": "禁忌"})
    ok &= check("枚举外的值被拒", "Input should be" in rejected or "未启用" in rejected, f"{rejected[:60]!r}")

    print()
    print("== 11. 并发写不丢数据（真存储 + 真线程） ==")
    import threading
    import uuid

    import memories

    RUN = uuid.uuid4().hex[:8]

    def race(cold: bool, count: int = 8, same: bool = False) -> tuple[int, int, int, list[str]]:
        """让 count 个线程尽量同时写。

        `cold=True` 用的是**全新的库路径**，也就必然走「文件还不存在」那条路。
        这一条是关键回归：早先冷启动时 8 个线程会互相抢着创建同一个文件，
        5~7 个抛 `attempt to write a readonly database` / `PermissionError`。

        返回（落盘条数, 该用户最大 hits, 抛异常线程数, 异常样本）。
        """
        db = Path(__file__).with_name(f"probe-conc-{RUN}-{race.n}.sqlite3")
        race.n += 1
        race.created.append(db)  # type: ignore[attr-defined]
        old = memories.DB_PATH
        memories.DB_PATH = db
        if not cold:
            memories.remember("warmup", [{"text": "热身"}])
        try:
            barrier = threading.Barrier(count)
            errors: list[BaseException] = []
            guard = threading.Lock()

            def worker(index: int) -> None:
                barrier.wait()  # 卡在一起，一起冲进去
                try:
                    text = "同一条事实" if same else f"事实 {index}"
                    memories.remember("u", [{"text": text}])
                except BaseException as exc:  # noqa: BLE001 — 探针要统计，不要崩
                    with guard:
                        errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rows = memories.memories_of("u")
            samples = [f"{type(e).__name__}: {e}" for e in errors[:2]]
            return len(rows), (rows[0]["hits"] if rows else 0), len(errors), samples
        finally:
            memories.DB_PATH = old

    race.n = 0  # type: ignore[attr-defined]
    race.created = []  # type: ignore[attr-defined]

    kept, _, failed, samples = race(cold=True)
    print(f"   冷启动（文件还不存在）8 线程并发写 -> 落盘 {kept} 条，失败 {failed} 次")
    for sample in samples:
        print(f"      异常样本：{sample}")
    ok &= check("冷启动并发：一条都没丢、一次都没报错", kept == 8 and failed == 0, f"{kept} 条 / {failed} 次异常")

    kept, _, failed, samples = race(cold=False)
    print(f"   热库 8 线程并发写 -> 落盘 {kept} 条，失败 {failed} 次")
    for sample in samples:
        print(f"      异常样本：{sample}")
    ok &= check("热库并发：一条都没丢、一次都没报错", kept == 8 and failed == 0, f"{kept} 条 / {failed} 次异常")

    dup_kept, hits, dup_failed, samples = race(cold=True, same=True)
    print(f"   冷启动 8 线程并发写**同一条** -> 落盘 {dup_kept} 条，hits={hits}，失败 {dup_failed} 次")
    for sample in samples:
        print(f"      异常样本：{sample}")
    ok &= check("去重没被并发冲垮（只留 1 条）", dup_kept == 1, f"{dup_kept} 条")
    ok &= check("每次写都算进 hits", hits == 8 and dup_failed == 0, f"hits={hits} / {dup_failed} 次异常")

    ok &= check(
        "写锁没有偷偷加回来（原子性归数据库管）",
        not hasattr(memories, "_write_lock"),
        "memories._write_lock 不存在",
    )

    for db in race.created:  # type: ignore[attr-defined]
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(db) + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass

    print()
    print("结果:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


async def run() -> int:
    try:
        return await main()
    finally:
        await agent_core.close_mcp()
        agent_core.set_memory_backend(read=None, write=None, forget=None)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
