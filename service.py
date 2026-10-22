"""应用层：把「会话元数据」（conversations.py）和「Agent 消息状态」（agent_core.py）拼成用例。

api.py 和 cli.py 只调这一层。「删会话」要同时删 checkpoint 和元数据、「会话列表」要同时读
标题和条数——这类跨层组合全收在这里，不在路由和命令行各写一遍。

async 还是 sync 只看一条：底层要不要 await。碰 agent_core（异步 checkpoint / MCP）必须
async；只读写那两个小 JSON 的保持同步，让 FastAPI 丢线程池，不占着事件循环做文件 IO。

三处规则的收敛点也都在这里，而且只在这里：`resolve_client_location`（位置优先级）、
`resolve_identity`（令牌 → 身份，`memory_enabled` 的唯一来源）、`_require_owned`
（越权判断 404，路由层不做归属逻辑——否则 CLI 和新端点会各写一遍）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

import agent_core
import conversations
import documents
import geocode
import limits
import memories
import retrieval
import users

_log = logging.getLogger("agent.service")

TITLE_INTERVAL = 10
TITLE_LAST_TURN = 50
TITLE_CONTEXT_TURNS = 10
MAX_CITY_LENGTH = 60

DATA_DIR = Path(__file__).with_name("data")


def _persisted_secret(env_name: str, filename: str) -> str:
    """取一个长期稳定的签名密钥：环境变量 > 落盘文件 > 现场生成并落盘。

    **必须落盘**：只放进程内存的话，换个进程算出来的签名完全不同，现象是「重启后历史图片
    全 403」，而看代码毫无问题。asset 和 guest 两处各用各的密钥，一个泄漏不连带另一个。
    """
    configured = os.getenv(env_name)
    if configured:
        return configured
    path = DATA_DIR / filename
    if path.is_file():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    generated = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 语义有限，失败不影响功能
        pass
    return generated

agent_core.set_memory_backend(
    read=memories.memories_of,
    write=memories.remember,
    forget=memories.forget,
)

agent_core.set_retrieval_backend(
    lambda user_id, query, doc_titles, limit: retrieval.search(
        user_id, query, limit=limit, doc_titles=doc_titles
    )
)



GUEST_USER_ID = users.GUEST_USER_ID

InvalidCredentials = users.InvalidCredentials


class InvalidToken(ValueError):
    """带了令牌，但令牌不认（过期 / 被撤销 / 伪造）。

    和「没带令牌」是两回事：没带 = 还没登录，正常走游客；不认 = 用户以为自己是登录的，
    降级成游客会让他看到游客名下的会话、还以为那是自己的。所以报 401 让前端清令牌重登。
    """


@dataclass(frozen=True)
class Identity:
    """一次请求的「你是谁」。由 `resolve_identity()` 产出，不要在别处手工拼。

    `account is None` 表示「没有账号」，但它**不等于访客**：本机 CLI 也没有账号
    （走默认账号），可它不是陌生人。所以「是不是访客」单独用 `is_guest` 表达，
    别拿 `account is None` 去推——推错的两个方向都有代价（详见两个常量的注释）。
    """

    user_id: str
    account: str | None
    memory_enabled: bool
    is_guest: bool = False
    fresh_guest: bool = False


CLI_IDENTITY = Identity(user_id=GUEST_USER_ID, account=None, memory_enabled=True)

GUEST_PREFIX = "g-"
GUEST_COOKIE = "aw_guest"
GUEST_COOKIE_DAYS = 30
GUEST_COOKIE_MAX_AGE = GUEST_COOKIE_DAYS * 86400
_GUEST_SECRET = _persisted_secret("GUEST_SECRET", "guest_secret.key")


def new_guest_id() -> str:
    """一个全新的访客 id。16 位十六进制 ≈ 64 位随机，猜不中。"""
    return GUEST_PREFIX + secrets.token_hex(8)


def _guest_signature(guest_id: str) -> str:
    return hmac.new(_GUEST_SECRET.encode(), guest_id.encode(), hashlib.sha256).hexdigest()[:24]


def sign_guest_cookie(guest_id: str) -> str:
    """`<id>.<签名>`。签名覆盖 id，改 id 就验不过。"""
    return f"{guest_id}.{_guest_signature(guest_id)}"


def verify_guest_cookie(raw: str | None) -> str | None:
    """验签通过返回 id，其余（没带 / 格式错 / 签名假）一律返回 None。

    返回 None 的结果是**发一个新 id**，不是报错：cookie 被清掉、被浏览器拦掉
    都是正常情况，不该让访客看到错误页。
    """
    text = (raw or "").strip()
    if not text:
        return None
    guest_id, _, signature = text.partition(".")
    if not guest_id.startswith(GUEST_PREFIX) or not signature:
        return None
    if not hmac.compare_digest(signature, _guest_signature(guest_id)):
        return None
    return guest_id


def resolve_identity(token: str | None, guest_cookie: str | None = None) -> Identity:
    """令牌（或访客 cookie）→ 身份。**这是「你是谁」的唯一入口。**

    令牌有效 → 登录用户；没带令牌 + 访客 cookie 有效 → 这个浏览器的访客；都没有 → 新分配
    一个（`fresh_guest=True`，由 `/api/auth/me` 下发 cookie）；带了令牌但不认 → 抛
    `InvalidToken`，**不静默降级**（理由见那个异常）。

    第三档不复用固定 id：刷新页面靠 cookie 认人，cookie 一丢就是新身份——这正是「临时」该有的。
    """
    if token:
        user = users.resolve_token(token)
        if user is None:
            raise InvalidToken("登录已过期，请重新登录")
        return Identity(user_id=user["user_id"], account=user["account"], memory_enabled=True)

    guest_id = verify_guest_cookie(guest_cookie)
    if guest_id:
        return Identity(user_id=guest_id, account=None, memory_enabled=False, is_guest=True)
    return Identity(
        user_id=new_guest_id(),
        account=None,
        memory_enabled=False,
        is_guest=True,
        fresh_guest=True,
    )


def register_user(account: object, password: object, invite: object = None) -> dict:
    """注册并直接登录，返回 `{"user", "token", "expires_at", "is_owner"}`。

    顺手签发令牌，省掉第二次 PBKDF2（一次约 110ms）；`is_owner` 一起返回，前端不必再打一次
    `/api/auth/me`。`invite` 只在不是第一个用户时才需要，判定在 `users.register` 里
    （要跟「空库」这个事实在同一个事务里看）。
    """
    user = users.register(account, password, invite)
    return {
        "user": user,
        "is_owner": users.owner_id() == user["user_id"],
        **users.create_session(user["user_id"]),
    }


def login(account: object, password: object) -> dict:
    """登录，返回 `{"user", "token", "expires_at", "is_owner"}`。

    `is_owner` 的理由同上：前端拿到它才能决定侧栏要不要出现「邀请同学加入」。
    """
    user = users.authenticate(account, password)
    return {
        "user": user,
        "is_owner": users.owner_id() == user["user_id"],
        **users.create_session(user["user_id"]),
    }


def registration_policy() -> dict[str, object]:
    """注册这块现在是什么规矩，给前端决定要不要显示邀请码输入框。

    前端不能自己猜「库里有几个人」。**只回「要不要码」，不回「主人是谁」**——这个端点不需要
    登录，多给一个 owner 就等于让任何路过的人都知道站里有几个人。
    """
    return {"needs_invite": users.count_users() > 0}


class NotAllowed(PermissionError):
    """这个人不该做这件事（角色不够，不是资源不属于他）。

    和 `_require_owned` 的 404 不同：那里报 403 等于确认「这个 id 是真的」，
    所以装不存在；这里没什么可泄露的——邀请码入口是公开可想的，
    只是当前这个人没有权限而已。
    """


def is_owner(user_id: str) -> bool:
    """是不是站点主人（最早注册的那个）。游客一律不是。"""
    if not user_id:
        return False
    return users.owner_id() == user_id


def create_invite(user_id: str, note: object = None) -> dict:
    """发一个邀请码。**只有第一个注册的人能发。** 否则抛 `NotAllowed`。

    为什么只给主人：邀请制的意义是「用的人有数」。要是每个被邀请进来的人
    都能再发码，那它退化成一条转发的链接，谁都能把自己那一圈拉进来。
    """
    owner = users.owner_id()
    if owner != user_id:
        raise NotAllowed("只有创建这个站点的账号能发邀请码")
    return users.create_invite(user_id, note)


def invites_of(user_id: str) -> list[dict]:
    """这个人发出去的邀请码。同样只有主人能看（别人本来就一个都没发过，
    但报 403 比返回空列表更诚实——空列表会让人以为「我发的都被人用完了」）。"""
    if users.owner_id() != user_id:
        raise NotAllowed("只有创建这个站点的账号能看邀请码")
    return users.list_invites()


def delete_invite(user_id: str, code: str) -> bool:
    """删掉一个邀请码（未用的等于立刻作废）。只有主人能删，否则抛 `NotAllowed`。

    「不存在」和「不是他发的」都返回 False，路由层统一转 404——
    和会话、教材那几处一个原则：不告诉调用方「这个 id 是真的，只是不归你」。
    """
    if users.owner_id() != user_id:
        raise NotAllowed("只有创建这个站点的账号能删邀请码")
    return users.delete_invite(code, user_id)


def logout(token: str | None) -> bool:
    """撤销这个令牌。撤掉了返回 True（没撤到也算成功——登出本就该幂等）。"""
    return users.revoke_token(token)



class RateLimited(Exception):
    """短时间做得太频繁。带 `retry_after` 秒数，端点直接拿去回 `Retry-After`。"""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExceeded(Exception):
    """今天的额度用完了。和 `RateLimited` 刻意分开——这个等多久都没用。"""


def limit_login(client_key: str) -> None:
    """登录 / 注册前先过一道，超了抛 `RateLimited`。

    按来源（IP）而不是按账号：按账号挡不住「拿一万个账号试同一个密码」。没有 IP 时退化成
    全局共享一个额度——比不设限好但会误伤，路由层该尽量给真实来源。
    """
    allowed, wait = limits.allow(
        f"login:{client_key}", limits.LOGIN_ATTEMPTS_PER_MINUTE, 60
    )
    if not allowed:
        raise RateLimited(
            f"登录太频繁了，{max(1, int(wait + 0.999))} 秒后再试", wait
        )


def _is_guest_id(user_id: str) -> bool:
    """这个 user_id 是访客吗。**只看前缀，不看 `account`**。

    前缀是唯一的判据：`Identity.is_guest` 只在有身份的调用链上有，而配额、
    清理这些函数只拿到一个 user_id 字符串。两处判据必须一致，所以都从
    这个函数走——真实账号是 `u-`、历史默认账号是 `"1"`，都不会误判成访客。
    """
    return str(user_id).startswith(GUEST_PREFIX)


def take_ask_slot(user_id: str, client_key: str | None = None) -> int:
    """扣掉一次提问额度，返回今天还剩多少次。超了抛 `RateLimited` / `QuotaExceeded`。

    **先限流后配额**：限流是内存判断（几微秒），配额要写一次 SQLite（约 30ms），被脚本刷时
    不必白写库。访客清 cookie 就是新身份，所以另加两道闸：按 IP 限流（换 cookie 换不掉 IP）
    + 全局访客日池（封顶陌生人总共能烧多少）。池失败要把个人那次退回去，否则被池挡下会连
    个人额度一起吃掉。登录用户不受这两道闸影响。
    """
    guest = _is_guest_id(user_id)

    if guest and client_key:
        allowed, wait = limits.allow(
            f"ask-ip:{client_key}", limits.ASKS_PER_MINUTE, 60
        )
        if not allowed:
            raise RateLimited(f"问得太快了，{max(1, int(wait + 0.999))} 秒后再试", wait)

    allowed, wait = limits.allow(f"ask:{user_id}", limits.ASKS_PER_MINUTE, 60)
    if not allowed:
        raise RateLimited(f"问得太快了，{max(1, int(wait + 0.999))} 秒后再试", wait)

    remaining = limits.consume(user_id, "ask", limits.ASK_PER_DAY)
    if remaining < 0:
        raise QuotaExceeded(
            f"今天的 {limits.ASK_PER_DAY} 次提问已经用完了，明天再来。"
        )

    if guest:
        pool_left = limits.consume(GUEST_POOL_KEY, "ask", limits.GUEST_ASK_PER_DAY)
        if pool_left < 0:
            limits.refund(user_id, "ask")
            raise QuotaExceeded("今天的访客体验额度用完了，注册后可以继续。")
    return remaining


GUEST_POOL_KEY = "pool-guest"


def refund_ask_slot(user_id: str) -> None:
    """把刚扣掉的那次退回去。用在「扣完额度、后面的事没成」的路径上
    （比如会话不存在直接 404）——那种情况不该算用户用掉一次。"""
    limits.refund(user_id, "ask")


def ask_quota(user_id: str) -> dict[str, int]:
    """今天还剩多少、上限多少。前端想显示「今天还能问 N 次」的话用这个。"""
    used = limits.used_today(user_id, "ask")
    return {
        "limit": limits.ASK_PER_DAY,
        "used": used,
        "remaining": max(0, limits.ASK_PER_DAY - used),
    }


MAX_UPLOAD_BYTES = documents.MAX_FILE_BYTES + 2 * 1024 * 1024


def check_document_quota(user_id: str) -> None:
    """传书之前查一下数量上限，超了抛 `QuotaExceeded`。

    数量在这里查（这个人还能不能再存一本），单本大小在 `documents` 里查（这个文件合不合规）。
    不用 `limits.consume`：书的数量是存下来的结果，另记一份就多一个要对账的地方（删书忘了减，
    用户就永远传不了）。直接数行最实在。
    """
    owned = documents.list_documents(user_id)
    if len(owned) >= limits.DOCS_PER_USER:
        raise QuotaExceeded(
            f"最多存 {limits.DOCS_PER_USER} 本书，先删掉几本没在用的再传。"
        )


def _require_owned(thread_id: str, user_id: str) -> None:
    """确认这个会话存在且属于这个人，否则抛 `KeyError`。

    **别人的会话也报「不存在」，不报「无权访问」**：403 等于确认「这个 id 是真的，只是不属于
    你」，可以拿它枚举别人的会话 id。存在性和归属一起判，不要写成两段式。
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
    """聊天入口：定下这次用哪个会话，返回 thread_id。没给就新建（归这个人），给了就必须是
    他自己的，否则抛 `KeyError`。

    必须在开流**之前**判：SSE 一开始响应头就发出去了，之后发现越权只能塞 error 事件，
    前端拿到的是 200。
    """
    if thread_id is None:
        return conversations.create_conversation(user_id)
    _require_owned(thread_id, user_id)
    conversations.touch_conversation(thread_id)
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


def _decorate_history(
    messages: list[dict[str, object]], user_id: str
) -> list[dict[str, object]]:
    """把 get_history 的返回装饰成前端能直接渲染的形态（就地修改并返回）。

    只对助手消息做两件事：正文和引用条目里的 `![题注](as_xx.png)` 换成签名 URL（前端拿不到
    密钥，`<img>` 又带不上令牌）。每次加载现签：签名带过期时间，存死链接过几天就全 403。
    """
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = inline_asset_refs(content, user_id)
        for item in message.get("citations") or []:
            asset_id = item.get("asset_id")
            if asset_id:
                item["asset_url"] = asset_url(str(asset_id), user_id)
            citation_content = item.get("content")
            if isinstance(citation_content, str) and citation_content:
                item["content"] = inline_asset_refs(citation_content, user_id)
    return messages


async def conversation_messages(thread_id: str, user_id: str) -> list[dict[str, object]]:
    """会话的历史消息。不存在或不属于自己抛 KeyError。"""
    _require_owned(thread_id, user_id)
    return _decorate_history(await agent_core.get_history(thread_id), user_id)


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
        "messages": _decorate_history(await agent_core.get_history(thread_id), user_id),
    }


async def resolve_client_location(location: dict | None) -> str | None:
    """把客户端传来的位置收敛成可读文本，如 `广东省广州市`。

    **优先级：手选城市 > 授权坐标**（手选是用户明确确认过的；坐标可能来自 VPN 或粗粒度
    定位）。两条都没有、或坐标解析不出来就返回 None——**绝不兜底猜测**：用服务器 IP 定位会把
    所有人都定位到服务器所在地，比「不知道」糟糕得多。
    """
    if not location:
        return None

    city = str(location.get("city") or "").strip()[:MAX_CITY_LENGTH]
    if city:
        return city

    latitude, longitude = location.get("latitude"), location.get("longitude")
    if latitude is None or longitude is None:
        return None

    return await asyncio.to_thread(geocode.describe, latitude, longitude)


def should_generate_title(turn_number: int) -> bool:
    """这一轮要不要（重新）生成标题。轮号从 1 开始。

    「第 1 轮起一次，之后每 10 轮重起一次，第 50 轮为止」的**唯一**落点，纯计算、可被探针直接
    测。用 `(turn - 1) % INTERVAL` 是因为轮号从 1 起，写 `turn % 10 == 0` 会漏掉第一轮。
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
    reasoning: bool = True,
    web_search: bool = True,
) -> AsyncIterator[dict[str, object]]:
    """给网页和 CLI 共用的事件流：标题与 Agent 并行生成，再转发 Agent 自己的事件。

    `client_location` 是原始位置（手选城市或授权坐标），在这里统一解析成可读文本。
    `memory_enabled` 由调用方显式给出，过这里只为带上 `Context`。归属检查不在这里——越权在
    开流之前就判掉（见 `begin_chat`）。
    """
    location = await resolve_client_location(client_location)
    previous_messages = await agent_core.user_messages(thread_id)
    turn_number = len(previous_messages) + 1
    title_task: asyncio.Task | None = None
    if should_generate_title(turn_number) and not conversations.is_renamed(thread_id):
        # 标题不阻塞首字：起标题与 Agent 并行，流收尾时再发。侧栏标题晚几秒更新用户
        # 无感，但串行等它会让第 1 轮的 TTFT 白白多出 ~1s（bench 实测占首字延迟一半）。
        title_task = asyncio.create_task(_build_title(previous_messages, user_input))
    try:
        async for event in agent_core.stream_agent(
            user_id,
            thread_id,
            user_input,
            location=location,
            memory_enabled=memory_enabled,
            reasoning=reasoning,
            web_search=web_search,
        ):
            if event.get("type") == "done":
                break  # done 是「流结束」的唯一标志，必须最后发（先收标题再发它）
            if event.get("type") == "citations":
                for item in event.get("items") or []:
                    asset_id = item.get("asset_id")
                    if asset_id:
                        item["asset_url"] = asset_url(str(asset_id), user_id)
            yield event

        if title_task is not None:
            try:
                title = await title_task
            except asyncio.CancelledError:
                raise
            except Exception:
                title = None
            if title:
                conversations.set_auto_summary(thread_id, title)
                yield {"type": "title", "title": title}
        yield {"type": "done"}
    except BaseException:
        # 生成器被提前关闭（客户端断开）时，别把并行的标题任务漏在后台。
        if title_task is not None and not title_task.done():
            title_task.cancel()
        raise


def capabilities() -> dict[str, bool]:
    """前端该把哪两个开关显示成可点。理由只有一条：**别让按钮骗人**。

    关推理靠各家私有字段（deepseek 叫 `reasoning_effort`、智谱叫 `thinking`），换到不认它的
    provider 会被静默忽略、按钮点了没反应；联网那两个工具 key 没配时只会回「服务未配置」。
    """
    return {
        "reasoning_toggleable": agent_core.REASONING_TOGGLEABLE,
        "web_search_available": bool(os.getenv("TAVILY_API_KEY")),
    }


async def shutdown() -> None:
    """释放 Agent 侧的资源：MCP 子进程与 checkpoint 连接。

    放在应用层而不是让 api.py 直接调 agent_core——这样 api.py 仍然只知道
    「用例」，不知道底下有哪些资源要收。
    """
    await agent_core.shutdown()



DocumentError = documents.DocumentError
CategoryExists = documents.CategoryExists


def upload_document(user_id: str, filename: str | None, data: bytes) -> dict[str, object]:
    """收下一个教材文件，立刻返回。**不解析**，解析见 `parse_uploaded_document`。"""
    return documents.create_upload(user_id, filename or "", data)


async def parse_uploaded_document(doc_id: str, user_id: str | None = None) -> dict[str, object]:
    """后台解析。调用方拿到 doc_id 就丢给 BackgroundTasks，不等结果。失败不抛异常——
    `documents.parse_document` 会写失败原因，抛出只会变成一条没人看的 traceback。

    `user_id` 只用来**预热检索索引**：索引是首次查询时才建的，700 页的书建一次要几秒，那几秒
    会算在用户第一次提问的等待里。预热失败不影响任何东西，所以吞掉异常。
    """
    record = await asyncio.to_thread(documents.parse_document, doc_id)
    if user_id and record.get("status") == documents.STATUS_READY:
        try:
            await asyncio.to_thread(retrieval.ensure_vectors, user_id)
            await asyncio.to_thread(retrieval.get_index, user_id)
        except Exception:  # noqa: BLE001 — 预热是纯优化，不许影响解析结果
            pass
    return record


async def list_documents(user_id: str) -> list[dict[str, object]]:
    return await asyncio.to_thread(documents.list_documents, user_id)


async def delete_document(doc_id: str, user_id: str) -> bool:
    """删一本书。不是自己的返回 False（→ 404），不碰别人的东西。"""
    deleted = await asyncio.to_thread(documents.delete_document, doc_id, user_id)
    if deleted:
        try:
            await asyncio.to_thread(retrieval.forget_document, doc_id)
        except Exception:  # noqa: BLE001 — 清理失败只是占点磁盘，不影响删书结果
            pass
    return deleted




async def list_categories(user_id: str) -> list[dict[str, object]]:
    """这个人的全部分类（带每类书数）。"""
    return await asyncio.to_thread(documents.list_categories, user_id)


async def create_category(user_id: str, name: object) -> dict[str, object]:
    """新建分类。重名抛 `CategoryExists`（→ 409），名字不合法抛 `ValueError`（→ 400）。"""
    return await asyncio.to_thread(documents.create_category, user_id, name)


async def rename_category(category_id: str, user_id: str, name: object) -> str:
    """返回 `"ok"` / `"missing"`（→ 404）/ `"clash"`（→ 409）。"""
    return await asyncio.to_thread(documents.rename_category, category_id, user_id, name)


async def delete_category(category_id: str, user_id: str) -> bool:
    """删分类。**类下的书退回未分类**，不跟着删。不存在/不是他的返回 False。"""
    return await asyncio.to_thread(documents.delete_category, category_id, user_id)


async def assign_document(doc_id: str, user_id: str, category_id: str | None) -> str:
    """把一本书挪到某个分类（`None` = 挪回未分类）。

    返回 `"ok"` / `"missing"`（没这本书）/ `"no_category"`（目标分类不是他的）。
    """
    return await asyncio.to_thread(documents.assign_category, doc_id, user_id, category_id)


async def document_markdown(doc_id: str, user_id: str, *, with_assets: bool = False) -> str | None:
    """取一本书的 Markdown（带页码 / 章节锚点），解析的产物、切分的输入。

    `with_assets=False` 给原文（图是相对文件名，用来看解析质量）；True 把图引用换成签名 URL，
    交给 `marked` 就能直接渲染出图。不在存储里存签名 URL：签名过期后书就全是破图。
    """
    text = await asyncio.to_thread(documents.markdown_of, doc_id, user_id)
    if text is None or not with_assets:
        return text
    return inline_asset_refs(text, user_id)


async def get_asset(asset_id: str, user_id: str) -> dict[str, object] | None:
    """取一张教材插图。**越权检查在这一层**（asset → doc → user 一条 SQL）。

    图片请求没法像别的端点那样靠 `Authorization` 头——`<img>` 标签不会带它。
    所以鉴权靠 URL 里的签名（见下面的 `sign_asset` / `verify_asset`），
    而这里只做「这张图是不是这个人的」这一层判断。
    """
    return await asyncio.to_thread(documents.asset_of, asset_id, user_id)


ASSET_SECRET_PATH = DATA_DIR / "asset_secret.key"
ASSET_TOKEN_TTL = 3600


_ASSET_SECRET = _persisted_secret("ASSET_SECRET", "asset_secret.key")


def sign_asset(asset_id: str, user_id: str, ttl: int = ASSET_TOKEN_TTL) -> str:
    """给一张图签一个 `过期时间.签名` 的凭证。"""
    expires = int(time.time()) + ttl
    return f"{expires}.{_asset_hmac(asset_id, user_id, expires)}"


def verify_asset(asset_id: str, user_id: str, token: str) -> bool:
    """校验凭证。三种失败一视同仁地返回 False：格式不对、签名不对、过期。

    `hmac.compare_digest` 而不是 `==`：字符串比较会在第一个不同的字符处返回，
    时间差可以反推出签名前缀（时序攻击）。这里只是图，但仍然不改这个习惯——
    以后把这套搬到更敏感的东西上时不会忘。
    """
    expires_text, _, signature = (token or "").partition(".")
    if not expires_text.isdigit() or not signature:
        return False
    expires = int(expires_text)
    if expires < int(time.time()):
        return False
    return hmac.compare_digest(signature, _asset_hmac(asset_id, user_id, expires))


def _asset_hmac(asset_id: str, user_id: str, expires: int) -> str:
    payload = f"{asset_id}:{user_id}:{expires}".encode()
    return hmac.new(_ASSET_SECRET.encode(), payload, hashlib.sha256).hexdigest()[:32]


def asset_url(asset_id: str, user_id: str) -> str:
    """一张图的**可直接放进 `![]( )` 的地址**（带签名，不带令牌）。

    这是「Markdown 能引用图片」和「图片不能公开」两件事的唯一接缝：
    Markdown 里写的是相对文件名（`as_77.png`），渲染前换成这里给出的 URL，
    浏览器 `<img>` 一请求就能验签放行，不需要任何请求头。
    """
    token = sign_asset(asset_id, user_id)
    return f"/api/assets/{quote(asset_id, safe='')}?u={quote(user_id, safe='')}&t={quote(token, safe='')}"


def inline_asset_refs(text: str, user_id: str) -> str:
    """把正文里的 `![图 3-4 抽样过程](as_77.png)` 换成能渲染的形式。

    纯字符串操作、**同步**——不碰 IO，别为了对称写成 async（每张图多一次线程池往返）。
    只替换本项目的 asset 文件名，外链和已经是 URL 的引用原样保留。
    """
    return documents.ASSET_REF_RE.sub(
        lambda match: match.group(1) + asset_url(match.group(2), user_id) + match.group(3),
        text,
    )




async def list_memories(user_id: str) -> list[dict[str, object]]:
    """列出这个用户被记住的全部条目，按写入先后排。

    `id` 是存储层给的稳定编号，**跟原文无关**：用户改了措辞它也不变，
    所以前端拿着旧列表去改、去删都不会失手。别拿 `text` 当 id——原文差一个
    标点就是另一条了。
    """
    rows = await asyncio.to_thread(memories.memories_of, user_id)
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "kind": row["kind"],
            "source": row.get("source"),
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
            "hits": row["hits"],
        }
        for row in rows
    ]


async def forget_memory(user_id: str, memory_id: str) -> bool:
    """删一条。不是自己的返回 False（→ 404）：存储层按 `user_id` 过滤，
    别人那行根本不会被命中，和 `_require_owned` 是同一个思路。
    """
    return await asyncio.to_thread(memories.forget_by_id, user_id, memory_id)


async def update_memory(
    user_id: str, memory_id: str, text: str, kind: str | None = None
) -> str:
    """改一条的原文。返回 `"ok"` / `"missing"` / `"clash"`，由路由映射成
    200 / 404 / 409——三个结果对用户的意义完全不同，不能都回一句「改失败」。
    """
    return await asyncio.to_thread(
        memories.update_text, user_id, memory_id, text, kind
    )


async def purge_stale_guests() -> int:
    """删掉过期和超量的**访客**会话，返回删了几个。启动时跑一次。

    访客没有「我的会话」界面，会话和 checkpoint 只增不减。两轮判据：时间（最后动静超过 TTL）
    和数量（清完还超上限的按最久没动静砍到上限——只靠时间挡不住成批造访客身份）。

    **只碰 `g-` 前缀**：登录用户和 CLI 用的 `"1"` 一条不动，清理程序擅自删就是丢数据。
    没有时间戳的访客会话也算过期（正常路径下一定写了时间戳）。
    """
    summaries = conversations.load_conversation_summaries()
    owners = conversations.load_conversation_owners()
    touched = conversations.load_touched()

    guests = [
        thread_id
        for thread_id, owner in owners.items()
        if str(owner).startswith(GUEST_PREFIX) and thread_id in summaries
    ]
    if not guests:
        return 0

    doomed: list[str] = []
    survivors: list[tuple[float, str]] = []
    for thread_id in guests:
        age = conversations.age_days(thread_id, touched)
        if age is None or age > limits.GUEST_THREAD_TTL_DAYS:
            doomed.append(thread_id)
        else:
            survivors.append((age, thread_id))

    overflow = len(survivors) - limits.GUEST_THREAD_MAX
    if overflow > 0:
        survivors.sort(reverse=True)
        doomed.extend(thread_id for _, thread_id in survivors[:overflow])
        survivors = survivors[overflow:]

    if not doomed:
        return 0

    for thread_id in doomed:
        await agent_core.delete_checkpoint_thread(thread_id)
    conversations.drop_conversations(doomed)
    return len(doomed)


async def startup() -> None:
    """进程启动时的一次性杂务。

    `reconcile_pending` 是教材库的崩溃恢复（上次进程解析中途退出会让书永远停在「解析中」），
    `purge_stale_guests` 清过期访客会话——两个都是同步 SQLite 写，丢线程池，别占事件循环。

    MCP 会话是「第一次用到才建」的（起子进程 + 握手 + 拉工具 + bind 模型，几百毫秒到两秒），
    而它算在 **TTFT** 里：启动时主动建一次，这笔代价就从「某个随机用户」挪到「服务启动」。
    失败**不拦启动**——常见原因是没配 key，真请求会给出更具体的错。
    """
    await asyncio.to_thread(users.purge_expired_sessions)
    await asyncio.to_thread(documents.reconcile_pending)
    await purge_stale_guests()

    try:
        tools = await agent_core.load_tools()
        _log.info("MCP 预热完成，工具 %d 个", len(tools))
    except Exception as exc:  # noqa: BLE001 — 预热失败只是回到「第一个请求等」
        _log.warning("MCP 预热失败（首个请求会自己再试）：%s", exc)
