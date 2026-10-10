"""应用层：把「会话元数据」（conversations.py）和「Agent 消息状态」（agent_core.py）拼成用例。

api.py 和 cli.py 只调这一层，不必知道底下是谁在干活。「删除会话」要同时删
LangGraph 的 checkpoint 和元数据 JSON；「会话列表」要同时读标题和消息条数；
「回退后会话被清空」要把标题还原——这类跨层组合全部收在这里，
不散落到 HTTP 路由和命令行里各写一遍。

哪些函数是 async、哪些是 sync，标准只有一条：**底层要不要 await**。
只要碰 agent_core（异步 checkpoint / MCP）就必须 async；只读写
conversations.py 那两个小 JSON 文件的（建会话、重命名）保持同步，
这样 FastAPI 会把它们丢进线程池，不会占着事件循环做文件 IO。
"""
from __future__ import annotations

from typing import AsyncIterator

import agent_core
import conversations

# 只在会话前几轮自动起标题，之后固定下来，避免每轮都白调一次模型。
AUTO_TITLE_TURNS = 10
# 起标题时喂给模型的最近几条用户消息。
TITLE_CONTEXT_TURNS = 10


async def list_conversations() -> list[dict[str, object]]:
    """会话列表：元数据里的标题 + 从 Agent 状态里数出来的消息条数。"""
    summaries = conversations.load_conversation_summaries()
    return [
        {
            "thread_id": thread_id,
            "summary": summary,
            "message_count": len(await agent_core.get_history(thread_id)),
        }
        for thread_id, summary in summaries.items()
    ]


def create_conversation() -> dict[str, str]:
    return {
        "thread_id": conversations.create_conversation(),
        "summary": conversations.DEFAULT_SUMMARY,
    }


def rename_conversation(thread_id: str, title: str) -> dict[str, str]:
    """重命名会话。标题清洗后为空抛 ValueError，会话不存在抛 KeyError。"""
    return {
        "thread_id": thread_id,
        "summary": conversations.rename_conversation(thread_id, title),
    }


async def delete_conversation(thread_id: str) -> bool:
    """同时删掉 Agent 侧的 checkpoint 和会话元数据，返回它此前是否存在。"""
    if not conversations.conversation_exists(thread_id):
        return False
    await agent_core.delete_checkpoint_thread(thread_id)
    conversations.drop_conversation(thread_id)
    return True


async def conversation_messages(thread_id: str) -> list[dict[str, object]]:
    """会话的历史消息。会话不存在抛 KeyError。"""
    if not conversations.conversation_exists(thread_id):
        raise KeyError(thread_id)
    return await agent_core.get_history(thread_id)


async def rewind_conversation(thread_id: str, turn: int) -> dict[str, object]:
    """回退到第 turn 条用户消息之前。

    会话不存在抛 KeyError，序号越界抛 ValueError。回退后如果整个会话被清空，
    标题一并还原成默认值、并撤销手动命名标记。
    """
    if not conversations.conversation_exists(thread_id):
        raise KeyError(thread_id)

    result = await agent_core.rewind_thread(thread_id, turn)
    if result["emptied"]:
        conversations.reset_summary(thread_id)

    return {
        "thread_id": thread_id,
        "removed": result["removed"],
        "text": result["text"],
        "messages": await agent_core.get_history(thread_id),
    }


async def _build_title(previous_messages: list[str], user_input: str) -> str:
    """标题策略：先问标题模型，拿不到就退回用户原话的前若干字。"""
    context = "\n".join((previous_messages + [user_input])[-TITLE_CONTEXT_TURNS:])
    title = await agent_core.generate_title(context)
    if not title:
        title = context.strip()[:conversations.MAX_TITLE_LENGTH] or "新会话"
    return title[:conversations.MAX_TITLE_LENGTH]


async def chat_events(user_id: str, thread_id: str, user_input: str) -> AsyncIterator[dict[str, object]]:
    """给网页和 CLI 共用的事件流：先按需推送标题，再转发 Agent 自己的事件。"""
    previous_messages = await agent_core.user_messages(thread_id)
    turn_number = len(previous_messages) + 1
    if turn_number <= AUTO_TITLE_TURNS and not conversations.is_renamed(thread_id):
        title = await _build_title(previous_messages, user_input)
        conversations.set_auto_summary(thread_id, title)
        yield {"type": "title", "title": title}

    async for event in agent_core.stream_agent(user_id, thread_id, user_input):
        yield event


async def shutdown() -> None:
    """释放 Agent 侧的资源：MCP 子进程与 checkpoint 连接。

    放在应用层而不是让 api.py 直接调 agent_core——这样 api.py 仍然只知道
    「用例」，不知道底下有哪些资源要收。
    """
    await agent_core.shutdown()
