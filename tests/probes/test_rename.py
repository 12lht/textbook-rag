"""重命名会话的接口回归测试。

覆盖：正常重命名、空白标题、超长标题、不存在的会话、以及
「手动命名后不再被自动起标题覆盖」这个关键点。

改造后的两点适配：
1. `service.chat_events` / `delete_conversation` 等变成协程，要 await / async for。
2. 不用 TestClient（它自带事件循环，会与 aiosqlite 的连接绑定冲突），
   改用 httpx 的 ASGITransport，全程同一个事件循环。
3. `generate_title` 现在是 async，替身也必须是 async。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

import agent_core
import conversations
import service

import api

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


async def main() -> None:
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created: list[str] = []
        try:
            thread_id = (await client.post("/api/conversations")).json()["thread_id"]
            created.append(thread_id)
            response = await client.patch(f"/api/conversations/{thread_id}", json={"title": "  天气  项目  "})
            check("PATCH 正常重命名返回 200", response.status_code == 200, f"status={response.status_code}")
            check("空白被折叠并去首尾", response.json().get("summary") == "天气 项目", f"got={response.json().get('summary')!r}")
            listed = {item["thread_id"]: item["summary"] for item in (await client.get("/api/conversations")).json()}
            check("列表接口读到新标题", listed.get(thread_id) == "天气 项目", f"got={listed.get(thread_id)!r}")
            check("重命名标记已写入", conversations.is_renamed(thread_id) is True)

            long_title = "很长" * 40
            response = await client.patch(f"/api/conversations/{thread_id}", json={"title": long_title})
            check("超长标题截断到 30 字", len(response.json()["summary"]) == 30, f"len={len(response.json()['summary'])}")

            response = await client.patch(f"/api/conversations/{thread_id}", json={"title": "   \n  "})
            check("纯空白标题返回 400", response.status_code == 400, f"status={response.status_code} detail={response.json().get('detail')}")

            response = await client.patch(f"/api/conversations/{thread_id}", json={"title": ""})
            check("空字符串返回 422", response.status_code == 422, f"status={response.status_code}")

            response = await client.patch(f"/api/conversations/{thread_id}", json={"title": "长" * 201})
            check("超过 200 字返回 422", response.status_code == 422, f"status={response.status_code}")

            response = await client.patch("/api/conversations/t-nonexistent", json={"title": "随便"})
            check("不存在的会话返回 404", response.status_code == 404, f"status={response.status_code} detail={response.json().get('detail')}")

            captured: dict[str, int] = {"calls": 0}
            original_generate = agent_core.generate_title

            async def spy(context: str) -> str:
                captured["calls"] += 1
                return "自动标题"

            agent_core.generate_title = spy
            try:
                async for event in service.chat_events("1", thread_id, "你好"):
                    if event.get("type") in {"title", "assistant_delta", "done", "error"}:
                        break
            except Exception:  # 模型调用可能失败，但我们只关心起标题是否被跳过
                pass
            finally:
                agent_core.generate_title = original_generate

            summaries = conversations.load_conversation_summaries()
            check("手动命名后自动起标题被跳过", captured["calls"] == 0, f"generate 被调用 {captured['calls']} 次")
            check("标题仍是手动改的那个", summaries.get(thread_id) == "很长" * 15, f"got={summaries.get(thread_id)!r}")

            fresh_id = (await client.post("/api/conversations")).json()["thread_id"]
            created.append(fresh_id)
            captured["calls"] = 0
            agent_core.generate_title = spy
            try:
                async for event in service.chat_events("1", fresh_id, "帮我查天气"):
                    # 标题与 Agent 并行生成、流收尾时才落盘（见 service.chat_events），
                    # 所以必须消费到 done 才能断言「已写入」；提前 break 等于客户端断开，
                    # 标题任务会被取消，那是另一条路径。
                    if event.get("type") in {"done", "error"}:
                        break
            except Exception:
                pass
            finally:
                agent_core.generate_title = original_generate
            check("未手动命名的会话会走自动起标题", captured["calls"] == 1, f"generate 被调用 {captured['calls']} 次")
            check("自动标题已写入", conversations.load_conversation_summaries().get(fresh_id) == "自动标题")

            response = await client.delete(f"/api/conversations/{thread_id}")
            created.remove(thread_id)
            check("删除会话返回 200", response.status_code == 200, f"status={response.status_code}")
            check("删除后标记被清掉", conversations.is_renamed(thread_id) is False)
        finally:
            for leftover in created:
                await client.delete(f"/api/conversations/{leftover}")

    await service.shutdown()

    print()
    print(f"合计 {sum(results)}/{len(results)} 通过")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
