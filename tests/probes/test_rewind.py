"""测试回退功能：service.rewind_conversation 的完整路径 + FastAPI 接口。不调用任何 LLM。

改造后注意三点：
1. `agent_core.graph` 已经没有了（图是懒加载 + 绑事件循环的），要拿图得 await
   `agent_core.get_graph()`；`get_history` / `rewind_conversation` /
   `delete_conversation` 全部变成协程。
2. HTTP 部分不能用 `fastapi.testclient.TestClient`——它自带一个事件循环（anyio
   portal），嵌在 asyncio.run 里会出问题。改用 httpx 的 ASGITransport，
   全程跑在同一个事件循环上，也就避开了「aiosqlite 连接跨循环」的坑。
3. **2026-10-22 修**：账号隔离上线后 `service.create_conversation` /
   `rewind_conversation` / `delete_conversation` 都要 `user_id`，这里补上。
   HTTP 那一段用 `dependency_overrides` 直接注入身份，不走登录流程——
   测试里注册真账号会往 `users` 表留垃圾，而访客身份又看不到别人的会话。

⚠️ 会短暂写生产 `data/`（会话元数据 + checkpoint），每个用例都在 finally 里删掉。
   要完全隔离得把四个 JSON 路径和 checkpoint 库都指到临时目录，代价大于收益。
"""
import asyncio
import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent_core
import conversations
import service

TEST_USER = service.CLI_IDENTITY.user_id

SEED = [
    SystemMessage(id="s0", content="系统提示"),
    HumanMessage(id="h1", content="北京天气如何"),
    AIMessage(id="a1", content="北京今天晴"),
    HumanMessage(id="h2", content="上海呢"),
    AIMessage(id="a2", content="上海今天多云"),
    HumanMessage(id="h3", content="那广州"),
    AIMessage(id="a3", content="广州有雨"),
]


async def seed(thread_id: str, messages: list) -> None:
    graph = await agent_core.get_graph()
    await graph.aupdate_state(
        {"configurable": {"thread_id": thread_id}},
        {"messages": messages},
        as_node="truncate",
    )


async def brief(thread_id: str) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in await agent_core.get_history(thread_id)]


async def service_level() -> None:
    tid = service.create_conversation(TEST_USER)["thread_id"]
    try:
        conversations.rename_conversation(tid, "天气三连问")
        await seed(tid, SEED)
        print("初始历史:", await brief(tid))
        print("初始标题:", conversations.load_conversation_summaries().get(tid),
              " 手动命名:", conversations.is_renamed(tid))

        result = await service.rewind_conversation(tid, 1, TEST_USER)
        print("\n回退到第 2 条用户消息之前 ->")
        print("  返回文本:", repr(result["text"]), " 删除条数:", result["removed"])
        print("  之后历史:", await brief(tid))
        print("  标题保持不动:", conversations.load_conversation_summaries().get(tid))
        assert result["text"] == "上海呢"
        assert await brief(tid) == [("user", "北京天气如何"), ("assistant", "北京今天晴")]
        assert result["messages"] == [
            {"role": "user", "content": "北京天气如何"},
            {"role": "assistant", "content": "北京今天晴"},
        ]
        assert conversations.load_conversation_summaries().get(tid) == "天气三连问"

        result = await service.rewind_conversation(tid, 0, TEST_USER)
        print("\n再回退到第 1 条之前 ->")
        print("  返回文本:", repr(result["text"]), " 之后历史:", await brief(tid))
        print("  标题已还原:", conversations.load_conversation_summaries().get(tid),
              " 手动命名:", conversations.is_renamed(tid))
        assert await brief(tid) == []
        assert conversations.load_conversation_summaries().get(tid) == conversations.DEFAULT_SUMMARY
        assert conversations.is_renamed(tid) is False

        for bad in (0, 5, -1):
            try:
                await service.rewind_conversation(tid, bad, TEST_USER)
                print(f"  turn={bad}: 未报错（不符合预期）")
            except ValueError as exc:
                print(f"  turn={bad}: 正确抛出 ValueError -> {exc}")

        try:
            await service.rewind_conversation("t-不存在", 0, TEST_USER)
            print("  不存在的会话: 未报错（不符合预期）")
        except KeyError:
            print("  不存在的会话: 正确抛出 KeyError")

        print("\n--- service.rewind_conversation 全部断言通过 ---")
    finally:
        await service.delete_conversation(tid, TEST_USER)


async def http_level() -> None:
    import httpx

    import api as api_module

    api_module.app.dependency_overrides[api_module.current_identity] = (
        lambda: service.CLI_IDENTITY
    )
    try:
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tid2 = service.create_conversation(TEST_USER)["thread_id"]
            try:
                await seed(tid2, [
                    SystemMessage(id="s0", content="系统提示"),
                    HumanMessage(id="h1", content="问题一"),
                    AIMessage(id="a1", content="回答一"),
                    HumanMessage(id="h2", content="问题二"),
                    AIMessage(id="a2", content="回答二"),
                ])
                r = await client.post(f"/api/conversations/{tid2}/rewind", json={"turn": 1})
                print("\nPOST /rewind turn=1 ->", r.status_code, r.json())
                assert r.status_code == 200 and r.json()["text"] == "问题二"

                r = await client.post("/api/conversations/t-不存在/rewind", json={"turn": 0})
                print("POST /rewind 不存在的会话 ->", r.status_code, r.json())
                assert r.status_code == 404

                r = await client.post(f"/api/conversations/{tid2}/rewind", json={"turn": 9})
                print("POST /rewind 越界 turn ->", r.status_code, r.json())
                assert r.status_code == 400

                r = await client.post(f"/api/conversations/{tid2}/rewind", json={"turn": -1})
                print("POST /rewind 负数 turn ->", r.status_code, r.json())
                assert r.status_code == 422
                print("\n--- HTTP 接口断言全部通过 ---")
            finally:
                await service.delete_conversation(tid2, TEST_USER)
    finally:
        api_module.app.dependency_overrides.clear()


async def main() -> None:
    try:
        await service_level()
        await http_level()
    finally:
        await service.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
