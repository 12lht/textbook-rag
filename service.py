"""应用层：把「会话元数据」（conversations.py）和「Agent 消息状态」（agent_core.py）拼成用例。

api.py 和 cli.py 只调这一层，不必知道底下是谁在干活。「删除会话」要同时删
LangGraph 的 checkpoint 和元数据 JSON；「会话列表」要同时读标题和消息条数；
「回退后会话被清空」要把标题还原——这类跨层组合全部收在这里，
不散落到 HTTP 路由和命令行里各写一遍。

哪些函数是 async、哪些是 sync，标准只有一条：**底层要不要 await**。
只要碰 agent_core（异步 checkpoint / MCP）就必须 async；只读写
conversations.py 那两个小 JSON 文件的（建会话、重命名）保持同步，
这样 FastAPI 会把它们丢进线程池，不会占着事件循环做文件 IO。

「用户位置」的收敛规则也在这里，而且**只在这里**：客户端可能给手选城市、
也可能给授权坐标，优先级和降级都由 `resolve_client_location` 一个函数决定，
`/api/location` 端点和聊天链路共用它，不会出现两处规则打架。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import agent_core
import conversations
import geocode
import memories

# 只在会话前几轮自动起标题，之后固定下来，避免每轮都白调一次模型。
AUTO_TITLE_TURNS = 10
# 起标题时喂给模型的最近几条用户消息。
TITLE_CONTEXT_TURNS = 10
# 用户手选的城市名长度上限，和 api 层的粗校验保持一致会有重复，所以只在这里管。
MAX_CITY_LENGTH = 60

# 把长期记忆的读写实现注入给 Agent 层。
#
# **为什么注入而不是让 agent_core 自己去 import `memories`**：项目有一条硬规矩——
# `agent_core` 不许 import 业务存储（`conversations` 就是这条规矩的产物）。
# 记忆工具需要「往哪个 user_id 存」这个动作，但「存哪、怎么去重」属于应用层。
# 所以由 service 把函数**当参数传下去**，依赖方向仍然是 service → agent_core。
#
# 注入之后 Agent 才会挂上 `save_memory` / `recall_memory` 两个工具，并在系统提示里
# 说明什么时候该记；没注入（评测脚本只 import agent_core）就一个都不出现。
# 所以**必须在这里、在任何一次请求之前完成**——工具列表是建 MCP 会话时算好的。
agent_core.set_memory_backend(
    read=memories.memories_of,
    write=memories.remember,
    forget=memories.forget,
)


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


async def resolve_client_location(location: dict | None) -> str | None:
    """把客户端传来的位置收敛成一个可读文本，例如 `广东省广州市`。

    **优先级：用户手选的城市 > 浏览器授权的坐标。**
    手选优先是因为它是用户明确确认过的；坐标可能来自 VPN、代理或粗粒度的
    Wi-Fi 定位，可靠性反而更低。两条都没有、或坐标解析不出来，一律返回 None
    ——**这里绝不兜底猜测**。以前用服务器 IP 定位兜底，多用户部署下会把所有人
    都定位到服务器所在地，模型据此向用户断言错误的城市，比「不知道」糟糕得多。

    拿不到位置时由模型的定位工具如实回答「用户没有提供位置」，而不是编一个。
    """
    if not location:
        return None

    city = str(location.get("city") or "").strip()[:MAX_CITY_LENGTH]
    if city:
        return city

    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is None or longitude is None:
        return None

    # geocode.describe 走 urllib，是**阻塞** IO。这个函数在 async 链路里被调用，
    # 直接在事件循环里跑会卡住所有并发请求（包括正在流式输出的那条 SSE），
    # 所以必须丢进线程池。异步函数里做同步 IO 是最典型的性能陷阱。
    return await asyncio.to_thread(geocode.describe, latitude, longitude)


async def _build_title(previous_messages: list[str], user_input: str) -> str:
    """标题策略：先问标题模型，拿不到就退回用户原话的前若干字。"""
    context = "\n".join((previous_messages + [user_input])[-TITLE_CONTEXT_TURNS:])
    title = await agent_core.generate_title(context)
    if not title:
        title = context.strip()[:conversations.MAX_TITLE_LENGTH] or "新会话"
    return title[:conversations.MAX_TITLE_LENGTH]


async def chat_events(
    user_id: str,
    thread_id: str,
    user_input: str,
    client_location: dict | None = None,
) -> AsyncIterator[dict[str, object]]:
    """给网页和 CLI 共用的事件流：先按需推送标题，再转发 Agent 自己的事件。

    `client_location` 是客户端的原始位置（可能是手选城市，也可能是授权坐标），
    由这里统一解析成可读文本再交给 Agent——解析规则见 `resolve_client_location`。
    CLI 不传这个参数，行为就是「用户没有提供位置」，和以前一致。
    """
    location = await resolve_client_location(client_location)
    previous_messages = await agent_core.user_messages(thread_id)
    turn_number = len(previous_messages) + 1
    if turn_number <= AUTO_TITLE_TURNS and not conversations.is_renamed(thread_id):
        title = await _build_title(previous_messages, user_input)
        conversations.set_auto_summary(thread_id, title)
        yield {"type": "title", "title": title}

    # 长期记忆原本是在这里「每轮偷偷抽一次」的（丢个后台任务，用模型把最近十条
    # 消息提炼成事实）。现在改成**模型自己决定**：它需要时会调 `save_memory`，
    # 值不值得记由它当场判断，用户改主意时还能用 `replaces` 作废旧条目。
    # 所以这一层不再需要任何收尾动作。
    async for event in agent_core.stream_agent(
        user_id, thread_id, user_input, location=location
    ):
        yield event


async def shutdown() -> None:
    """释放 Agent 侧的资源：MCP 子进程与 checkpoint 连接。

    放在应用层而不是让 api.py 直接调 agent_core——这样 api.py 仍然只知道
    「用例」，不知道底下有哪些资源要收。
    """
    await agent_core.shutdown()
