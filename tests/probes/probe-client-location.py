"""端到端验证：客户端位置这条链路。

覆盖四件事：
  1. /api/location 用真实坐标能解析出城市（走真高德，但只 2 次请求）
  2. 手选城市原样返回，且优先级高于坐标
  3. 坐标非法时是 422（HTTP 层粗校验），不是 500
  4. 聊天链路带位置能跑通，不带位置时降级成「用户没有提供位置」而不是报错

用 ASGITransport 在进程内打 API，不用起 uvicorn。
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import api  # noqa: E402
import service  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))


async def main() -> int:
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("== 1. /api/location 逆地理编码（真实坐标，广州塔）==")
        r = await client.post("/api/location", json={"latitude": 23.10614, "longitude": 113.32452})
        body = r.json() if r.status_code == 200 else {}
        check("HTTP 200", r.status_code == 200, f"status={r.status_code}")
        check("解析出广州", body.get("location") == "广东省广州市", f"location={body.get('location')!r}")

        print("\n== 2. 手选城市优先于坐标 ==")
        r = await client.post(
            "/api/location",
            json={"city": "长沙", "latitude": 23.10614, "longitude": 113.32452},
        )
        body = r.json() if r.status_code == 200 else {}
        check("手选覆盖坐标", body.get("location") == "长沙", f"location={body.get('location')!r}")

        print("\n== 3. 边界：非法坐标 / 空位置 / 未授权的坐标 ==")
        r = await client.post("/api/location", json={"latitude": 999, "longitude": 116})
        check("纬度越界被拒（422）", r.status_code == 422, f"status={r.status_code}")
        r = await client.post("/api/location", json={})
        check("空对象返回 null 而不是报错", r.status_code == 200 and r.json()["location"] is None,
              f"status={r.status_code} body={r.json() if r.status_code == 200 else ''}")
        r = await client.post("/api/location", json={"latitude": 0.0, "longitude": 0.0})
        check("海上坐标解析不出但仍是 200", r.status_code == 200 and r.json()["location"] is None,
              f"location={r.json().get('location') if r.status_code == 200 else ''!r}")

        print("\n== 4. 聊天链路：带位置 ==")
        thread_id = (await client.post("/api/conversations")).json()["thread_id"]
        events = []
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "我现在在哪个城市？", "thread_id": thread_id, "user_id": "1",
                  "location": {"city": "长沙"}},
            timeout=120,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        tools = [e["tool"] for e in events if e.get("type") == "tool_result"]
        answer = "".join(e.get("content", "") for e in events if e.get("type") == "assistant_delta")
        check("调用了定位工具", "get_user_location" in tools, f"tools={tools}")
        check("答案里出现了长沙", "长沙" in answer, f"answer={answer[:80]!r}")
        await client.delete(f"/api/conversations/{thread_id}")

        print("\n== 5. 聊天链路：不带位置（用户拒绝授权）==")
        thread_id = (await client.post("/api/conversations")).json()["thread_id"]
        events = []
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json={"message": "我现在在哪个城市？", "thread_id": thread_id, "user_id": "1"},
            timeout=120,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        errors = [e for e in events if e.get("type") == "error"]
        answer = "".join(e.get("content", "") for e in events if e.get("type") == "assistant_delta")
        check("没有报错事件", not errors, f"errors={errors}")
        check("没有编造城市（不提长沙/北京）",
              "长沙" not in answer and "北京" not in answer, f"answer={answer[:100]!r}")
        check("告知用户需要提供城市",
              any(word in answer for word in ("不知道", "没有", "告知", "告诉", "哪个城市", "所在")),
              f"answer={answer[:100]!r}")
        await client.delete(f"/api/conversations/{thread_id}")

    await service.shutdown()
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：", FAIL)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
