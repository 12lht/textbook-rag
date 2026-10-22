"""真实端到端探针：跑一轮真实教材问答，验证 citations 事件真的流通。

会真实调用一次主模型（DeepSeek）和教材检索，一轮的成本可接受。
不落任何 checkpoint 污染——会话走完就留在 conversations 里，thread 前缀
probe- 便于辨认（和历史上其它探针同做法）。
"""
import asyncio
import sys

sys.path.insert(0, "D:/agent")

import service  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{('  ' + str(detail))[:200] if detail else ''}")
    if not condition:
        failures.append(label)


async def main():
    conv = service.create_conversation("u-671d242584")
    thread_id = conv["thread_id"]
    events = []
    try:
        async for event in service.chat_events(
            "u-671d242584", thread_id, "结合书里的插图，讲讲 5.4.5 扩频技术在移动通信中的应用。"
        ):
            events.append(event)
            kind = event.get("type")
            if kind == "assistant_delta":
                print(".", end="", flush=True)
            elif kind == "tool_call":
                print(f"\n[tool_call] {event.get('tool')}", flush=True)
            elif kind == "citations":
                print(f"\n[citations] {len(event['items'])} 条", flush=True)
            elif kind == "done":
                print("\n[done]", flush=True)
        history = await service.conversation_messages(thread_id, "u-671d242584")
    finally:
        try:
            service.conversations.drop_conversation(thread_id)
        except Exception:
            pass

    types = [e.get("type") for e in events]
    check("有回答正文", any(t == "assistant_delta" for t in types), types)
    check("流程正常结束", types[-1] == "done", types[-3:])

    cite_events = [e for e in events if e.get("type") == "citations"]
    check("发了 citations 事件", len(cite_events) == 1, len(cite_events))
    if cite_events:
        items = cite_events[0]["items"]
        numbered = [i for i in items if i.get("code")]
        proximity = [i for i in items if not i.get("code")]
        check("编号条目有编号和出处", all(i.get("code") and i.get("doc_title") for i in numbered),
              [(i.get("code"), i.get("doc_title"), i.get("page_start")) for i in items])
        check("邻近图条目都带 asset_id", all(i.get("asset_id") and i.get("kind") == "figure" for i in proximity),
              [(i.get("asset_id"), i.get("kind")) for i in proximity])
        check("引用条目带原文内容", all(i.get("content") for i in items))
        check("citations 事件在 done 之前", types.index("citations") < types.index("done"))

        body = "".join(e["content"] for e in events if e.get("type") == "assistant_delta")
        codes_in_body = [i["code"] for i in items if f"[{i['code']}]" in body]
        check("正文引用了至少一个编号", len(codes_in_body) >= 1, codes_in_body)
        print(f"\n正文 {len(body)} 字，引用条目 {len(items)} 条：")
        for i in items:
            print(f"  [{i['code']}] {i.get('doc_title')} · {i.get('chapter')} · "
                  f"第 {i.get('page_start')}-{i.get('page_end')} 页 · {i.get('kind')} · asset={i.get('asset_id')}")

    answer = [m for m in history if m.get("role") == "assistant" and m.get("citations")]
    check("历史消息能重建引用", len(answer) >= 1, len(answer))
    if answer:
        got = answer[-1]["citations"]
        check("重建条目有出处", all(i.get("doc_title") for i in got),
              [(i.get("code"), i.get("doc_title")) for i in got])
        check("重建条目有原文", all(i.get("content") for i in got))
        all_content = answer[-1]["content"] + "".join(i.get("content", "") for i in got)
        check("历史里图引用已是签名 URL", "as_" not in all_content or "/api/assets/" in all_content,
              all_content[:80])


asyncio.run(main())
print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部通过")
