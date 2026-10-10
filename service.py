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

「你是谁」也在这里收敛，同样只在这里：`resolve_identity()` 是「令牌 → 身份」的
唯一入口，`Identity.memory_enabled` 是「这次能不能记长期记忆」的唯一来源。
**越权判断也只写在这里**（`_require_owned`），路由层不做归属逻辑——否则
CLI 和新加的端点会各写一遍，迟早有一处漏掉。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

import agent_core
import conversations
import geocode
import memories
import users

# 起标题的节奏：第 1 轮先起一次，之后每 10 轮重起一次，到第 50 轮为止。
#
# 触发点是 1 / 11 / 21 / 31 / 41 这五轮，第 51 轮起标题就固定下来不再改。
#
# 原来是一个上限（`AUTO_TITLE_TURNS = 10`），含义是「前 10 轮**每轮都重起一次**」
# ——一个会话光起标题就要调 10 次模型。改成间隔之后同样的会话只调 5 次。
#
# 为什么不干脆只在第 1 轮起一次：第 1 轮只有一句话，信息量太少，容易起出
# 「询问某问题」这种等于没说的标题；隔几轮重起一次，标题才能跟着话题走。
TITLE_INTERVAL = 10
TITLE_LAST_TURN = 50
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


# --------------------------------------------------------------------------
# 身份
# --------------------------------------------------------------------------

GUEST_USER_ID = users.GUEST_USER_ID

# 把 `users.InvalidCredentials` 转出来一份，api.py 才好 catch 而**不必 import users**：
# 路由层的依赖只该有 service 一个，多一条边就多一处要同步的地方。
# 只转这一个是因为**只有它需要被区分**——它决定登录失败该回 401 还是 400。
# 「账号重名」和「格式不合规」（`AccountTaken` / `ValueError`）对客户端都是
# 「你填的东西不行」，catch `ValueError` 一个就够，不必转类型出来。
InvalidCredentials = users.InvalidCredentials


class InvalidToken(ValueError):
    """带了令牌，但这个令牌不认（过期 / 被撤销 / 伪造）。

    **和「没带令牌」是两回事**，不能都当游客处理：
    没带令牌 = 用户还没登录，正常状态，让他以游客身份用；
    令牌不认 = 用户以为自己是登录的，实际已经不是了。这时候把他降级成游客
    很危险——他会看到**游客名下的会话**，还以为那是自己的。所以要报错，
    由端点返回 401，前端据此清掉本地令牌并提示重新登录。
    """


@dataclass(frozen=True)
class Identity:
    """一次请求的「你是谁」。由 `resolve_identity()` 产出，不要在别处手工拼。

    `account is None` 就是游客。是否加这个便利属性，取决于有没有地方真的用它——
    目前没有，所以不加。
    """

    user_id: str
    account: str | None
    # 允不允许读写长期记忆。**逐请求**的事实，不是身份自带的属性——
    # 同一个 user_id 在不同场景下可以不同（本机 CLI 就开着，见 `CLI_IDENTITY`）。
    memory_enabled: bool


# 游客身份。共用默认账号，**记忆关掉**——理由见 `users.GUEST_USER_ID`：
# 几个陌生游客共用同一个 user_id，开着记忆等于互相公开偏好。
GUEST_IDENTITY = Identity(user_id=GUEST_USER_ID, account=None, memory_enabled=False)

# 命令行是本机单人使用，没有「多个陌生游客共用一个账号」的问题，
# 所以给它开着记忆。用的仍是默认账号，历史行为和以前一致（以前 CLI 就是 user_id="1"）。
CLI_IDENTITY = Identity(user_id=GUEST_USER_ID, account=None, memory_enabled=True)


def resolve_identity(token: str | None) -> Identity:
    """令牌 → 身份。**这是「你是谁」的唯一入口。**

    - 没带令牌 → 游客身份，不报错（未登录是正常状态，不是错误分支）；
    - 令牌不认 → 抛 `InvalidToken`（端点转 401），**不要静默降级成游客**，
      理由见 `InvalidToken`。
    """
    if not token:
        return GUEST_IDENTITY
    user = users.resolve_token(token)
    if user is None:
        raise InvalidToken("登录已过期，请重新登录")
    return Identity(user_id=user["user_id"], account=user["account"], memory_enabled=True)


def register_user(account: object, password: object) -> dict:
    """注册并直接登录，返回 `{"user", "token", "expires_at"}`。

    注册完顺手签发令牌，省掉「注册成功再登录一次」的第二次 PBKDF2（一次约 110ms）。
    账号重名抛 `users.AccountTaken`，格式不合规抛 `ValueError`。
    """
    user = users.register(account, password)
    return {"user": user, **users.create_session(user["user_id"])}


def login(account: object, password: object) -> dict:
    """登录，返回 `{"user", "token", "expires_at"}`。失败抛 `users.InvalidCredentials`。"""
    user = users.authenticate(account, password)
    return {"user": user, **users.create_session(user["user_id"])}


def logout(token: str | None) -> bool:
    """撤销这个令牌。撤掉了返回 True（没撤到也算成功——登出本就该幂等）。"""
    return users.revoke_token(token)


def _require_owned(thread_id: str, user_id: str) -> None:
    """确认这个会话存在且属于这个人，否则抛 `KeyError`。

    **别人的会话也报「不存在」，不报「无权访问」。** 403 等于告诉对方
    「这个 thread_id 是真的，只是不属于你」——攻击者可以拿它枚举别人的会话 id。
    404 什么都不泄露，而且从用户视角看，别人的会话本来就等于不存在。

    存在性 + 归属一起判，所以「先查存在、再查归属」这种两段式写法不要出现：
    两段之间要是有别的分支，很容易只判了前一半。
    """
    if not conversations.belongs_to(thread_id, user_id):
        raise KeyError(thread_id)


async def list_conversations(user_id: str) -> list[dict[str, object]]:
    """会话列表：**只列这个人的**，元数据里的标题 + 从 Agent 状态里数出来的消息条数。"""
    owned = conversations.ids_of_user(user_id)
    summaries = conversations.load_conversation_summaries()
    return [
        {
            "thread_id": thread_id,
            "summary": summary,
            "message_count": len(await agent_core.get_history(thread_id)),
        }
        for thread_id, summary in summaries.items()
        if thread_id in owned
    ]


def create_conversation(user_id: str) -> dict[str, str]:
    return {
        "thread_id": conversations.create_conversation(user_id),
        "summary": conversations.DEFAULT_SUMMARY,
    }


def begin_chat(user_id: str, thread_id: str | None) -> str:
    """聊天入口：定下这次要用哪个会话，返回 thread_id。

    没给 thread_id 就新建一个（归这个人）；给了就必须是**他自己的**，
    否则抛 `KeyError`。

    为什么要在开流**之前**判：SSE 一旦开始，HTTP 头就已经发出去了，之后
    再发现越权只能塞一个 error 事件进流里——前端拿到的是一个 200 的响应，
    语义上很别扭。放在这里判，端点可以干干净净地返回 404。
    """
    if thread_id is None:
        return conversations.create_conversation(user_id)
    _require_owned(thread_id, user_id)
    return thread_id


def rename_conversation(thread_id: str, title: str, user_id: str) -> dict[str, str]:
    """重命名会话。标题清洗后为空抛 ValueError，不存在或不属于自己抛 KeyError。"""
    _require_owned(thread_id, user_id)
    return {
        "thread_id": thread_id,
        "summary": conversations.rename_conversation(thread_id, title),
    }


async def delete_conversation(thread_id: str, user_id: str) -> bool:
    """同时删掉 Agent 侧的 checkpoint 和会话元数据，返回它此前是否存在。

    不属于自己的会话返回 False（→ 404），**不会**去删别人的东西。
    """
    if not conversations.belongs_to(thread_id, user_id):
        return False
    await agent_core.delete_checkpoint_thread(thread_id)
    conversations.drop_conversation(thread_id)
    return True


async def conversation_messages(thread_id: str, user_id: str) -> list[dict[str, object]]:
    """会话的历史消息。不存在或不属于自己抛 KeyError。"""
    _require_owned(thread_id, user_id)
    return await agent_core.get_history(thread_id)


async def rewind_conversation(thread_id: str, turn: int, user_id: str) -> dict[str, object]:
    """回退到第 turn 条用户消息之前。

    不存在或不属于自己抛 KeyError，序号越界抛 ValueError。回退后如果整个会话被清空，
    标题一并还原成默认值、并撤销手动命名标记。
    """
    _require_owned(thread_id, user_id)

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


def should_generate_title(turn_number: int) -> bool:
    """这一轮要不要（重新）生成标题。轮号从 1 开始。

    「第 1 轮起一次，之后每 10 轮重起一次，第 50 轮为止」这句话的**唯一**落点。
    单独拎成函数是为了能被探针直接测——它只做纯计算，不读状态、不碰模型。

    用 `(turn - 1) % INTERVAL` 而不是 `turn % INTERVAL`，是因为轮号从 1 开始：
    第 1 轮要落在「起」这一侧，写成 `turn % 10 == 0` 会把第一轮漏掉。
    """
    return turn_number <= TITLE_LAST_TURN and (turn_number - 1) % TITLE_INTERVAL == 0


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
    *,
    memory_enabled: bool = True,
) -> AsyncIterator[dict[str, object]]:
    """给网页和 CLI 共用的事件流：先按需推送标题，再转发 Agent 自己的事件。

    `client_location` 是客户端的原始位置（可能是手选城市，也可能是授权坐标），
    由这里统一解析成可读文本再交给 Agent——解析规则见 `resolve_client_location`。
    CLI 不传这个参数，行为就是「用户没有提供位置」，和以前一致。

    `memory_enabled` 由调用方**显式**给出（`Identity.memory_enabled`），过这里
    只是为了往 `Context` 上带一层。默认 True 服务的是 CLI 那种本机单人的场景。
    归属检查不在这里做：越权要在开流之前就判掉（见 `begin_chat`）。
    """
    location = await resolve_client_location(client_location)
    previous_messages = await agent_core.user_messages(thread_id)
    turn_number = len(previous_messages) + 1
    if should_generate_title(turn_number) and not conversations.is_renamed(thread_id):
        title = await _build_title(previous_messages, user_input)
        conversations.set_auto_summary(thread_id, title)
        yield {"type": "title", "title": title}

    # 长期记忆原本是在这里「每轮偷偷抽一次」的（丢个后台任务，用模型把最近十条
    # 消息提炼成事实）。现在改成**模型自己决定**：它需要时会调 `save_memory`，
    # 值不值得记由它当场判断，用户改主意时还能用 `replaces` 作废旧条目。
    # 所以这一层不再需要任何收尾动作。
    async for event in agent_core.stream_agent(
        user_id,
        thread_id,
        user_input,
        location=location,
        memory_enabled=memory_enabled,
    ):
        yield event


async def shutdown() -> None:
    """释放 Agent 侧的资源：MCP 子进程与 checkpoint 连接。

    放在应用层而不是让 api.py 直接调 agent_core——这样 api.py 仍然只知道
    「用例」，不知道底下有哪些资源要收。
    """
    await agent_core.shutdown()


async def startup() -> None:
    """进程启动时的一次性杂务。

    `purge_expired_sessions` 是一次**同步**的 SQLite 写（约 30ms），所以丢进
    线程池，别占着事件循环——和 `resolve_client_location` 里处理阻塞 IO 是同一个理由。
    """
    await asyncio.to_thread(users.purge_expired_sessions)
