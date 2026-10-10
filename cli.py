import service


def print_history(thread_id: str) -> None:
    history = service.conversation_messages(thread_id)
    if not history:
        print("该会话暂无可显示的对话内容。")
        return
    print("\n--- 历史对话 ---")
    for message in history:
        role = "用户" if message["role"] == "user" else "助手"
        print(f"{role}：{message['content']}")


def load_summaries() -> dict[str, str]:
    return {item["thread_id"]: item["summary"] for item in service.list_conversations()}


def run_cli(user_id: str = "1") -> None:
    active_thread_id = None
    summaries = load_summaries()

    print("请选择会话，或直接输入问题开始新会话。")
    print("输入 /switch 选择历史会话，输入 /restart 新建会话。")
    print("输入‘退出’、‘exit’、‘quit’或‘q’结束。")

    while True:
        try:
            user_input = input("\n>>> 用户：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n会话结束。")
            return

        command = user_input.lower()
        if command in {"退出", "exit", "quit", "q"}:
            print("会话结束。")
            return
        if command == "/restart":
            active_thread_id = service.create_conversation()["thread_id"]
            summaries = load_summaries()
            print("已开启新对话。")
            continue
        if command == "/switch":
            conversations = list(summaries.items())
            if not conversations:
                print("当前没有可切换的历史会话。")
                continue
            print("\n可用会话：")
            for index, (_, summary) in enumerate(conversations, start=1):
                print(f"{index}. {summary}")
            choice = input("请选择会话编号（直接回车取消）：").strip()
            if not choice:
                continue
            if not choice.isdigit() or not 1 <= int(choice) <= len(conversations):
                print("无效的会话编号。")
                continue
            active_thread_id = conversations[int(choice) - 1][0]
            print(f"已切换到：{summaries[active_thread_id]}")
            print_history(active_thread_id)
            continue
        if not user_input:
            continue

        if active_thread_id is None:
            active_thread_id = service.create_conversation()["thread_id"]
            summaries = load_summaries()
            print("已自动开启新会话。")

        for event in service.chat_events(user_id, active_thread_id, user_input):
            if event["type"] == "tool_call":
                print(f"\n[工具] {event['tool']}")
            elif event["type"] == "tool_result":
                print(f"[工具完成] {event['tool']}")
            elif event["type"] == "assistant":
                print(f"\n助手：{event['content']}")


if __name__ == "__main__":
    run_cli()
