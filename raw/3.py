"""开发历史归档：FastAPI 拆分前的单文件 CLI 入口。

当前运行入口已迁移到根目录 cli.py，Agent 核心位于 agent_core.py。
本文件仅用于保留历史结构，不作为当前运行入口。
"""

from agent_core import (
    create_thread,
    get_history,
    load_conversation_summaries,
    stream_agent,
)


def print_history(thread_id: str) -> None:
    history = get_history(thread_id)
    if not history:
        print("该会话暂无可显示的对话内容。")
        return

    print("\n--- 历史对话 ---")
    for message in history:
        role = "用户" if message["role"] == "user" else "助手"
        print(f"{role}：{message['content']}")


def run_archived_cli(user_id: str = "1") -> None:
    """保留旧版的命令行交互形态，使用当前核心实现。"""
    thread_id = None
    summaries = load_conversation_summaries()

    print("历史 CLI 归档模式：输入问题开始对话。")
    print("输入 /restart 新建会话，输入 /switch 切换会话，输入 q 退出。")

    while True:
        try:
            user_input = input("\n>>> 用户：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n会话结束。")
            return

        command = user_input.lower()
        if command in {"q", "quit", "exit", "退出"}:
            print("会话结束。")
            return
        if command == "/restart":
            thread_id = create_thread()
            summaries = load_conversation_summaries()
            print("已开启新对话。")
            continue
        if command == "/switch":
            conversations = list(summaries.items())
            for index, (_, summary) in enumerate(conversations, start=1):
                print(f"{index}. {summary}")
            choice = input("请选择编号：").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(conversations):
                thread_id = conversations[int(choice) - 1][0]
                print_history(thread_id)
            continue
        if not user_input:
            continue
        if thread_id is None:
            thread_id = create_thread()
            print("已自动开启新会话。")

        for event in stream_agent(user_id, thread_id, user_input):
            if event["type"] == "tool_call":
                print(f"\n[工具] {event['tool']}")
            elif event["type"] == "tool_result":
                print(f"[工具完成] {event['tool']}")
            elif event["type"] == "assistant":
                print(f"\n助手：{event['content']}")


if __name__ == "__main__":
    run_archived_cli()
