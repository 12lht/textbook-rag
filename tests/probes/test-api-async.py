"""API 层回归：确认全量异步改造后 HTTP 行为不变。

用 fastapi.testclient.TestClient，`with` 语句会触发 lifespan，
从而验证「退出时收掉 MCP 子进程」这条路真的能走通。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import api

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"   {detail}" if detail else ""))


with TestClient(api.app) as client:
    print("=== 基础端点 ===")
    r = client.get("/api/health")
    check("GET /api/health", r.status_code == 200 and r.json()["status"] == "ok", str(r.status_code))

    r = client.get("/")
    check("GET / 返回前端页", r.status_code == 200 and "<html" in r.text.lower(), str(r.status_code))

    print("\n=== 会话生命周期 ===")
    r = client.post("/api/conversations")
    check("POST /api/conversations", r.status_code == 200 and r.json()["thread_id"], str(r.status_code))
    thread_id = r.json()["thread_id"]

    r = client.get("/api/conversations")
    ids = [c["thread_id"] for c in r.json()]
    check("GET /api/conversations 含新会话", r.status_code == 200 and thread_id in ids, f"{len(ids)} 个会话")

    print("\n=== 流式对话（真实模型 + MCP 工具）===")
    r = client.post("/api/chat/stream", json={
        "message": "现在几点了？", "thread_id": thread_id, "user_id": "1",
    })
    body = r.text
    events = []
    for block in body.split("\n\n"):
        line = next((p for p in block.split("\n") if p.startswith("data: ")), None)
        if line:
            events.append(json.loads(line[6:]))
    kinds = [e["type"] for e in events]
    check("SSE 状态码 200", r.status_code == 200, str(r.status_code))
    check("先推 conversation 事件", kinds[:1] == ["conversation"], str(kinds[:2]))
    check("有 tool_call / tool_result", "tool_call" in kinds and "tool_result" in kinds)
    check("有 done 收尾", kinds[-1] == "done", str(kinds[-2:]))
    tool_calls = [e["tool"] for e in events if e["type"] == "tool_call"]
    tool_results = [e["tool"] for e in events if e["type"] == "tool_result"]
    check("tool_call 与 tool_result 一一对应（无重复上报）",
          tool_calls == tool_results, f"{tool_calls} vs {tool_results}")
    answer = "".join(e["content"] for e in events if e["type"] == "assistant_delta")
    check("有回答文本", bool(answer.strip()), answer[:60])

    print("\n=== 历史 / 重命名 / 回退 / 删除 ===")
    r = client.get(f"/api/conversations/{thread_id}/messages")
    msgs = r.json()["messages"]
    check("GET messages 有 2 条", r.status_code == 200 and len(msgs) == 2, f"{len(msgs)} 条")

    r = client.patch(f"/api/conversations/{thread_id}", json={"title": "  " + "长" * 120 + "  "})
    check("PATCH 重命名 200", r.status_code == 200, str(r.status_code))
    check("标题截断到 30 字", len(r.json()["summary"]) == 30, f"长度={len(r.json()['summary'])}")

    r = client.post(f"/api/conversations/{thread_id}/rewind", json={"turn": 0})
    check("POST rewind 200", r.status_code == 200, str(r.status_code))
    check("回退后消息清空", len(r.json()["messages"]) == 0, f"{len(r.json()['messages'])} 条")

    r = client.delete(f"/api/conversations/{thread_id}")
    check("DELETE 200", r.status_code == 200, str(r.status_code))

    print("\n=== 404 分支 ===")
    check("对不存在会话取历史 → 404",
          client.get("/api/conversations/t-nope/messages").status_code == 404)
    check("对不存在会话回退 → 404",
          client.post("/api/conversations/t-nope/rewind", json={"turn": 0}).status_code == 404)
    check("对不存在会话删除 → 404",
          client.delete("/api/conversations/t-nope").status_code == 404)
    check("空标题重命名 → 422（Pydantic 粗校验）",
          client.patch(f"/api/conversations/t-nope", json={"title": ""}).status_code == 422)

failed = [name for name, ok, _ in results if not ok]
print(f"\n通过 {len(results) - len(failed)}/{len(results)}")
if failed:
    print("失败项：", failed)
raise SystemExit(1 if failed else 0)
