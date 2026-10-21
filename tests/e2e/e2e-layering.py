"""拆层之后的真实端到端：真实 uvicorn + 真实模型，走一遍完整链路。

重点验证跨层组合是否正确（这些逻辑现在都在 service.py 里）：
  - 列表要同时读元数据标题和 Agent 消息条数
  - 删除要同时删 checkpoint 和元数据
  - 回退到清空要顺带把标题还原成默认值
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8015"
results = []


def check(name, ok, extra=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or "null"
        # 500 时 body 是纯文本（Internal Server Error），不能让测试脚本自己崩掉，
        # 否则真正的问题会被 JSONDecodeError 盖住。
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def stream(path, body):
    """读一整段 SSE，返回事件类型列表。"""
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    events = []
    with urllib.request.urlopen(request, timeout=180) as response:
        for raw in response:
            line = raw.decode().strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


status, payload = call("GET", "/api/health")
check("健康检查", status == 200 and payload.get("status") == "ok", str(payload))

status, created = call("POST", "/api/conversations")
thread_id = created["thread_id"]
check("新建会话", status == 200 and thread_id.startswith("t-"), str(created))

try:
    status, renamed = call("PATCH", f"/api/conversations/{thread_id}", {"title": "  分层  验证  "})
    check("重命名（顺带验证空白折叠）", status == 200 and renamed["summary"] == "分层 验证", str(renamed))

    status, listed = call("GET", "/api/conversations")
    row = next((item for item in listed if item["thread_id"] == thread_id), None)
    check("列表同时给出标题和消息条数", row is not None and row["summary"] == "分层 验证" and row["message_count"] == 0, str(row))

    print("\n--- 发一条消息（真实模型） ---")
    events = stream("/api/chat/stream", {"message": "只回四个字：你好世界", "thread_id": thread_id})
    kinds = [event["type"] for event in events]
    print("事件序列:", kinds)
    check("发了 conversation 事件", kinds and kinds[0] == "conversation")
    check("手动命名过，所以没有 title 事件", "title" not in kinds, str(kinds))
    check("收到流式正文", "assistant_delta" in kinds)
    check("以 done 收尾", kinds and kinds[-1] == "done")

    status, history = call("GET", f"/api/conversations/{thread_id}/messages")
    roles = [message["role"] for message in history["messages"]]
    check("历史里有用户和助手消息", "user" in roles and "assistant" in roles, str(roles))

    status, listed = call("GET", "/api/conversations")
    row = next((item for item in listed if item["thread_id"] == thread_id), None)
    check("消息条数跟着涨了", row is not None and row["message_count"] == len(history["messages"]), str(row))

    print("\n--- 回退到第 1 条之前（应清空并把标题还原） ---")
    status, rewound = call("POST", f"/api/conversations/{thread_id}/rewind", {"turn": 0})
    check("回退返回 200", status == 200, str(status))
    check("回退删掉了消息", status == 200 and rewound["removed"] == len(history["messages"]), str(rewound.get("removed")))
    check("返回的 messages 已清空", rewound["messages"] == [], str(rewound["messages"]))

    status, listed = call("GET", "/api/conversations")
    row = next((item for item in listed if item["thread_id"] == thread_id), None)
    check("标题已还原成默认值", row is not None and row["summary"] == "新会话（暂无消息）", str(row))

    # 标题还原后，自动起标题应该重新生效 → 会多出一个 title 事件
    print("\n--- 再发一条，确认自动起标题恢复、且会话还能用 ---")
    events = stream("/api/chat/stream", {"message": "只回两个字：好的", "thread_id": thread_id})
    kinds = [event["type"] for event in events]
    check("标题还原后自动起标题恢复", "title" in kinds, str(kinds))
    check("回退清空后仍能正常对话（有流式正文）", "assistant_delta" in kinds and kinds[-1] == "done", str(kinds))

    status, history2 = call("GET", f"/api/conversations/{thread_id}/messages")
    roles2 = [message["role"] for message in history2["messages"]]
    check("清空后新消息已入库", roles2.count("user") == 1 and "assistant" in roles2, str(roles2))

    status, deleted = call("DELETE", f"/api/conversations/{thread_id}")
    check("删除返回 200", status == 200, str(deleted))
    status, gone = call("GET", f"/api/conversations/{thread_id}/messages")
    check("删除后查历史返回 404", status == 404, f"status={status} {gone}")
    status, gone = call("PATCH", f"/api/conversations/{thread_id}", {"title": "x"})
    check("删除后重命名返回 404", status == 404, f"status={status} {gone}")
finally:
    call("DELETE", f"/api/conversations/{thread_id}")

print()
print(f"合计 {sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
