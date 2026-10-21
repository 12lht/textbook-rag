"""对真实 FastAPI 后端做端到端回退测试（会真实调用一次模型，prompt 极短）。"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8010"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def chat(thread_id, message):
    status, body = call("POST", "/api/chat/stream", {"message": message, "thread_id": thread_id, "user_id": "1"})
    events = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    kinds = []
    for raw in events:
        try:
            kinds.append(json.loads(raw).get("type"))
        except json.JSONDecodeError:
            kinds.append("?")
    return status, kinds


def messages(thread_id):
    status, body = call("GET", f"/api/conversations/{thread_id}/messages")
    if status != 200:
        return status, body
    return status, json.loads(body)["messages"]


status, body = call("POST", "/api/conversations")
thread_id = json.loads(body)["thread_id"]
print("1. 新建会话:", status, thread_id)

status, kinds = chat(thread_id, "只回复两个字：好的")
print("2. 第一轮 /chat/stream:", status, "事件:", sorted(set(kinds)))
status, kinds = chat(thread_id, "只回复两个字：收到")
print("3. 第二轮 /chat/stream:", status, "事件:", sorted(set(kinds)))

status, history = messages(thread_id)
print("4. 当前历史:", status)
for index, message in enumerate(history):
    print(f"   [{index}] {message['role']}: {message['content'][:40]!r}")

user_turns = [m for m in history if m["role"] == "user"]
print(f"   用户消息共 {len(user_turns)} 条")

print("\n5. 调用回退接口 turn=1 ...")
status, body = call("POST", f"/api/conversations/{thread_id}/rewind", {"turn": 1})
print("   HTTP", status)
print("   body:", body[:400])

status, history2 = messages(thread_id)
print("6. 回退后历史:", status)
for index, message in enumerate(history2):
    print(f"   [{index}] {message['role']}: {message['content'][:40]!r}")

ok = len(history2) == len(history) - 2
print(f"\n结论: 回退{'成功' if ok else '失败'}（{len(history)} -> {len(history2)} 条）")

status, body = call("DELETE", f"/api/conversations/{thread_id}")
print("7. 清理测试会话:", status)
