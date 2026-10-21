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

# 挂在 api.py 配好的 "agent" logger 下面，不自己配 handler。
_log = logging.getLogger("agent.service")

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

DATA_DIR = Path(__file__).with_name("data")


def _persisted_secret(env_name: str, filename: str) -> str:
    """取一个长期稳定的签名密钥：环境变量 > 落盘文件 > 现场生成并落盘。

    **必须落盘**，不能在进程内存里现生成：那样换个进程算出来的签名就完全不同，
    表现是「服务重启后签名全部失效」，而看代码毫无问题——因为签名函数本身是对的。
    落盘同时解决了重启和多进程两个场景。

    优先级把环境变量放第一，是为了部署时能显式指定（多机共用同一个密钥）。

    asset（图片链接）和 guest（访客身份）两处都用它，但**各用各的密钥**：
    一个泄漏不该连带另一个也破。规则只写这一份，免得一处改了 chmod、
    另一处忘了。
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

# 把教材检索注入给 Agent 层。理由和上面完全一样：`agent_core` 不许 import
# `retrieval`（依赖方向 service → agent_core），而检索必须按 `user_id` 隔离，
# `user_id` 只有编排层知道。
#
# **注入之后所有请求都会看到「你有教材」这段提示词**，哪怕这个人一本书都没传。
# 这是刻意的：工具表是进程级冻结的，没法按人加减；让模型查一次、工具回
# 「还没有可检索的教材」，比提示词里不敢说「你能查教材」要好——后者会让它
# 对着一本已经传好的书说「我读不了文件」。
agent_core.set_retrieval_backend(
    lambda user_id, query, doc_titles, limit: retrieval.search(
        user_id, query, limit=limit, doc_titles=doc_titles
    )
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

    `account is None` 表示「没有账号」，但它**不等于访客**：本机 CLI 也没有账号
    （走默认账号），可它不是陌生人。所以「是不是访客」单独用 `is_guest` 表达，
    别拿 `account is None` 去推——推错的两个方向都有代价（详见两个常量的注释）。
    """

    user_id: str
    account: str | None
    # 允不允许读写长期记忆。**逐请求**的事实，不是身份自带的属性——
    # 同一个 user_id 在不同场景下可以不同（本机 CLI 就开着，见 `CLI_IDENTITY`）。
    memory_enabled: bool
    # 没登录的访客。配额按 IP 加限、会话会被定时清理，这两件事都只针对访客。
    is_guest: bool = False
    # 这次请求刚分配了访客身份、**cookie 还没发下去**。
    # 由 `api.current_identity` 消费（普通响应都会顺带发 cookie）。
    # SSE 那条路（`StreamingResponse`）设了不生效，但它之前必然已经过普通请求。
    fresh_guest: bool = False


# 命令行是本机单人使用，没有「多个陌生访客共用一个账号」的问题，所以给它开着记忆。
# 用的仍是默认账号 `GUEST_USER_ID`（`"1"`），历史行为和以前一致。
#
# 2026-10-22 改：以前这个常量同时被 HTTP 访客复用，后果是任何人打开网址都能读到
# 此前所有访客的会话。现在 HTTP 访客一律走签名 cookie 的 `g-` 身份，
# 这个常量**只剩 CLI 一个使用者**——别再用它给 HTTP 侧兜底。
CLI_IDENTITY = Identity(user_id=GUEST_USER_ID, account=None, memory_enabled=True)

# --------------------------------------------------------------------------
# 访客身份：签名 cookie
# --------------------------------------------------------------------------
#
# 为什么不干脆关掉访客（未登录一律 401）：产品本来就是邀请制，访客体验只是
# 「打开就能试一句」的余量。但**关掉会砍掉一个既有行为**，而隔离能保住它。
#
# 关键是**签名**：cookie 里不能只放一个随机 id，否则任何人手填 `g-00000000`
# 就能读到别人的会话。签名覆盖 id 本身，服务端只认自己签过的。
#
# id 用 `g-` 前缀，和真实账号的 `u-`、历史默认账号的 `"1"` 三套命名空间互不重叠。
# 这不是好看：`conversations._owner_from` 对「没有归属记录」的会话兜底成 "1"，
# 前缀一旦撞上，老会话就会被新访客认领走。
GUEST_PREFIX = "g-"
GUEST_COOKIE = "aw_guest"
# cookie 有效期。访客会话另外有 7 天清理（见 `purge_stale_guests`），
# 所以这个 30 天实际是「id 能活多久」，不是「数据能活多久」。
GUEST_COOKIE_DAYS = 30
# 给 `response.set_cookie(max_age=...)` 用的秒数。放在这里而不是 api.py：
# 天数和秒数是同一个事实的两种单位，分开写就会有一处改漏。
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

    - 令牌有效 → 登录用户；
    - 没带令牌、访客 cookie 有效 → 这个浏览器的访客身份；
    - 没带令牌、没有/伪造 cookie → **新分配**一个访客身份，`fresh_guest=True`，
      由 `/api/auth/me` 负责把 cookie 发下去；
    - 带了令牌但不认 → 抛 `InvalidToken`（端点转 401），**不要静默降级成访客**，
      理由见 `InvalidToken`。

    注意第三档：**不再复用固定 id**。同一个访客刷新页面时靠 cookie 认出来，
    cookie 一丢就是新身份、看不到旧会话——这正是「临时」该有的行为。
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

    注册完顺手签发令牌，省掉「注册成功再登录一次」的第二次 PBKDF2（一次约 110ms）。
    **同时把 `is_owner` 一起算出来返回**：前端注册完立刻要决定「要不要显示
    邀请码入口」，让它为此再打一次 `/api/auth/me` 是白跑一趟——这里本来就知道。

    账号重名抛 `users.AccountTaken`，格式不合规抛 `ValueError`，
    邀请码不对抛 `users.InviteRequired` / `users.InvalidInvite`。

    `invite` 只在**不是第一个用户**时才需要，判定在 `users.register` 里
    （要跟「空库」这个事实在同一个事务里看，这里看不到）。
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

    前端**不能自己猜**「库里有几个人」——那是服务端的事，猜错了要么
    让第一个用户填一个根本不存在的码，要么该拦的时候没拦。

    **只回「要不要码」，不回「主人是谁」**：这个端点不需要登录，
    多给一个 `owner` 就等于让任何路过的人都知道站里有几个人、最早那个的 id 是什么。
    前端要知道自己是不是主人，走 `/api/auth/me`（那个是按令牌算的）。
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


# --------------------------------------------------------------------------
# 限制：还能不能再来一次（F3 配额 / F4 防刷）
# --------------------------------------------------------------------------
#
# 两层保护，回答的是两个不同的问题：
#
#   限流：**这几秒你刷了几次**   → 内存滑窗，几十秒就恢复
#   配额：**今天你一共用了几次** → 落 SQLite，第二天才恢复
#
# 分开是因为对用户的意义完全不同：一个是「手太快了，等一下」，
# 另一个是「今天用完了，明天再来」。糊成一个「操作太频繁」会让人以为
# 等一会儿就好，结果发现怎么等都不行。

class RateLimited(Exception):
    """短时间做得太频繁。带 `retry_after` 秒数，端点直接拿去回 `Retry-After`。"""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExceeded(Exception):
    """今天的额度用完了。和 `RateLimited` 刻意分开——这个等多久都没用。"""


def limit_login(client_key: str) -> None:
    """登录/注册前先过一道。超了抛 `RateLimited`。

    `client_key` 由路由层给（通常是客户端 IP）。为什么按来源而不是按账号：
    按账号挡不住「拿一万个账号试同一个密码」，而按来源挡得住——他得先有一万个 IP。

    没有 IP 时（比如测试客户端）退化成按一个固定串计数，也就是全局共享一个额度。
    比不设限好，但会误伤；所以路由层该尽量给真实来源。
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

    **顺序是先限流后配额**：限流是内存判断（几微秒），配额要写一次 SQLite（约 30ms）。
    先做便宜的那个，被脚本刷的时候就不用白写一次库。

    ## 访客多两道闸（2026-10-22）

    访客身份按 cookie 分配，**清 cookie 就是新身份**，所以只按人算的话：
    「每分钟 20 次」和「每天 200 次」都能靠清 cookie 绕过。补两道：

    1. **按 IP 限流**（`client_key`）——换 cookie 换不掉 IP；
    2. **全局访客日池**（`GUEST_ASK_PER_DAY`）——把陌生人总共能烧的钱封顶。

    顺序：IP 限流（内存）→ 个人配额（SQLite）→ 池（SQLite）。池失败要把
    个人那次退回去，否则「被池挡下」会连个人额度一起吃掉，用户第二天就少一次。

    登录用户不受这两道闸影响，行为和以前完全一致。
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


# 全局访客日池在 `limits.daily` 里的 key。用 `pool-` 前缀和真实 id 分开——
# 真实账号是 `u-`、访客是 `g-`，三套命名空间互不重叠，不会张冠李戴。
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


# 单个上传请求的**请求体**上限，比文件本身宽 2MB：multipart 编码要带 boundary
# 和表单头，实测一本 250MB 的 PDF 请求体会多出几十 KB，留 2MB 是为了不误伤。
#
# 为什么路由层需要一个「请求体」上限，而不只是 `documents.MAX_FILE_BYTES`：
# 那个值是在文件**全部读进内存之后**才检查的。一个 3GB 的请求会先把进程内存
# 吃光，然后才被告知「文件太大」——拒绝得太晚，代价已经付了。
# 这个常量是「还没读就该断」的阈值，用在 HTTP 层（见 `api.limit_upload_size`）。
MAX_UPLOAD_BYTES = documents.MAX_FILE_BYTES + 2 * 1024 * 1024


def check_document_quota(user_id: str) -> None:
    """传书之前查一下数量上限。超了抛 `QuotaExceeded`。

    **数量在这里查，单本大小在 `documents.py` 里查**：那边是「这一个文件本身
    合不合规」（格式、大小），这边是「这个人还能不能再存一本」。
    两件事的判据不同，硬塞进一个函数会让「文件太大」和「书太多了」变成同一句报错。

    为什么不也用 `limits.consume`：书的数量是**存下来的结果**（`documents` 表里
    真有这么多行），不是一个会自然衰减的计数。再记一份计数就多了一个要跟真实
    数据对账的地方——删书时忘了减，用户就永远传不了了。直接数行最实在。
    """
    owned = documents.list_documents(user_id)
    if len(owned) >= limits.DOCS_PER_USER:
        raise QuotaExceeded(
            f"最多存 {limits.DOCS_PER_USER} 本书，先删掉几本没在用的再传。"
        )


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
    # 记一次「这个会话今天还活着」，访客清理靠它排序（见 `purge_stale_guests`）。
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

    两件事，都只对助手消息做：

    - 正文里的 `![题注](as_xx.png)` 换成带签名的图片 URL。历史消息不能指望
      前端自己换——前端拿不到签名密钥，而 `<img>` 请求又带不上令牌。
    - 引用条目（C5：刷新后引用面板还在）里的图片同样换 URL。

    每次加载都现签新 URL：签名带过期时间，存死的链接过几天就全 403 了。
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
    reasoning: bool = True,
    web_search: bool = True,
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
        reasoning=reasoning,
        web_search=web_search,
    ):
        # 引用条目里带 `asset_id`（图/公式所在段落）就顺手签一个图片 URL。
        # 签名只能在 service 层做——密钥在这层，agent_core 不该知道签名这回事。
        # 前端拿它干两件事：引用面板里出图、把回答正文里的 `![题注](as_xx.png)`
        # 换成这个 URL（`<img>` 不带请求头，所以必须给现成 URL，见 asset_url 的说明）。
        if event.get("type") == "citations":
            for item in event.get("items") or []:
                asset_id = item.get("asset_id")
                if asset_id:
                    item["asset_url"] = asset_url(str(asset_id), user_id)
        yield event


def capabilities() -> dict[str, bool]:
    """前端该把哪两个开关显示成可点。

    存在的理由只有一条：**别让按钮骗人**。
    `reasoning` 是靠各家私有的非标准字段关掉的（deepseek 叫 `reasoning_effort`、
    智谱叫 `thinking`），换到一个不认它的 provider 时请求会被静默接受、参数被忽略，
    于是按钮点了没反应而用户只会以为模型卡了。宁可置灰，也不要摆设。
    联网同理：工具在 MCP 子进程里，key 没配时它们只会回「服务未配置」。
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


# --------------------------------------------------------------------------
# 教材库（A 域）
# --------------------------------------------------------------------------
#
# 这一段的形状和别的用例不一样：**收文件和解析是两个用例**，不是一个。
#
# 一次 300 页 PDF 的解析要几十秒。放在上传请求里同步做完，浏览器就卡在一个
# 转圈的框上，用户以为失败了会重复点。所以上传只落盘 + 写一行 pending（毫秒级），
# 解析由后台任务另起（`parse_uploaded_document`），前端轮询列表看状态推进。
#
# 代价是「后台任务」这个东西的生命周期不受控：进程要是正好在解析中途重启，
# 这本书会永远停在 parsing。`documents.reconcile_pending()` 在启动时兜这个底。
#
# 全部走 `asyncio.to_thread`：底下是 SQLite + PDF 解析，都是阻塞的。
# 直接在事件循环里调，会卡住所有人正在流式输出的 SSE 连接。

DocumentError = documents.DocumentError
CategoryExists = documents.CategoryExists


def upload_document(user_id: str, filename: str | None, data: bytes) -> dict[str, object]:
    """收下一个教材文件，立刻返回。**不解析**，解析见 `parse_uploaded_document`。"""
    return documents.create_upload(user_id, filename or "", data)


async def parse_uploaded_document(doc_id: str, user_id: str | None = None) -> dict[str, object]:
    """后台解析。调用方拿到 doc_id 后就把它丢给 BackgroundTasks，不用等结果。

    失败不抛异常：`documents.parse_document` 会把原因写进记录、置 failed，
    前端在列表里能看到。这里抛出只会变成后台任务里一条没人看的 traceback。

    `user_id` 只用来**预热检索索引**：索引是「第一次查询时才建」的，一本 700 页
    的书有几千段，建一次要几秒——那几秒会算在用户第一次提问的等待里，看起来像
    「模型在发呆」。移植到后台任务里做，等用户真的提问时索引已经在了。

    预热失败不影响任何东西（索引没建成就等下次查询再建），所以这里吞掉异常。
    """
    record = await asyncio.to_thread(documents.parse_document, doc_id)
    if user_id and record.get("status") == documents.STATUS_READY:
        try:
            # 先补向量再建索引：索引构建时会把已有的向量读成矩阵，顺序反了
            # 就要多重建一次（老矩阵里没有刚算出来的那些行）。
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
        # 向量存在另一个库里（`retrieval.VECTOR_DB`），删书不会连带删掉。
        # 这里顺手清一次，否则删过的书会留下一堆再也用不上的向量。
        try:
            await asyncio.to_thread(retrieval.forget_document, doc_id)
        except Exception:  # noqa: BLE001 — 清理失败只是占点磁盘，不影响删书结果
            pass
    return deleted


# --------------------------------------------------------------------------
# 分类（知识库管理页）
# --------------------------------------------------------------------------
#
# 这一组都很薄：只是把同步的 SQLite 调用搬进线程池，**刻意不放任何业务判断**。
# 「删分类不删书」「改名撞了怎么办」「能不能挂到别人的分类上」全部在
# `documents.py` 里定死，这样 CLI 或别的调用方拿到的是同一套行为。


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
    """取一本书的 Markdown（带页码/章节锚点）。

    它是解析的产物，也是切分的输入。给前端的用途有两个，所以有 `with_assets`：

    - **默认（False）**：原文，图引用是相对文件名 `as_77.png`。这是「看解析质量、
      看锚点」用的——存进去什么样就拿出来什么样，不带任何随时间会变的签名。
    - **True**：把图引用换成带签名的 URL，于是这份 Markdown 交给
      `marked` 就能直接渲染出图（这也是 WeKnora 的做法：正文用相对引用，
      交给上层重写成能取到的地址）。

    不在存储里直接存签名 URL：签名一小时过期，存进去的书第二天就全是破图。
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


# 图片签名。**为什么不能用令牌**：回答里的图是 markdown 的 `![](url)`，
# 浏览器发图片请求时不会带 `Authorization` 头，所以拿不到身份。把令牌拼进 URL
# 又等于把长效凭证写进页面（能分享、能留在浏览器历史里）。
# 签名只是「这张图 + 这个人 + 这段时间」的一次性凭证，泄露了也只能看一张图，
# 而且会过期。
#
# 密钥的取法有优先级，最后一级是**落盘生成的**：
#   1. 环境变量 `ASSET_SECRET`（部署时配，最可控）；
#   2. `data/asset_secret.key`（第一次启动生成，之后一直用）。
#
# 第一版是「没配环境变量就用 `secrets.token_hex(16)`」，结果自己也踩了：
# 密钥在进程内存里，**换个进程算出来的签名完全不同**。表现是「服务重启后
# 历史消息里的图全部 403」，而排查时看着代码毫无问题——因为签名函数本身是对的。
# 落盘生成一次性解决重启和多进程（uvicorn --workers）两个场景。
ASSET_SECRET_PATH = DATA_DIR / "asset_secret.key"
# 签名有效期。一小时足够一轮对话，短到不适合当分享链接用。
ASSET_TOKEN_TTL = 3600


# 加载逻辑和访客 cookie 共用 `_persisted_secret`（规则只写一处），
# 但密钥本身是独立的：图片链接的密钥泄漏，不该连带访客身份可伪造。
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

    纯字符串操作，**同步**——和 `sign_asset` 一样不碰 IO，别为了对称写成 async
    （那样每张图都多一次线程池往返）。

    只替换本项目的 asset 文件名（`documents.ASSET_REF_RE`），外链图、
    已经是 URL 的引用原样保留，所以这个函数可以安全地作用在任意 Markdown 上。
    """
    return documents.ASSET_REF_RE.sub(
        lambda match: match.group(1) + asset_url(match.group(2), user_id) + match.group(3),
        text,
    )


# ---------------------------------------------------------------------------
# 长期记忆（界面上那块「AI 记住了什么」）
#
# 这三个函数只服务**界面**。模型那条路走的是 `agent_core` 的
# `recall_memory` / `save_memory`，两条路读写同一张表，所以界面删掉一条，
# 模型下一次 `recall_memory` 就真的读不到了——这正是 E6 的验收标准。
#
# 权限：只给登录用户。api 层用 `logged_in_identity` 把游客挡在外面
# （游客的 `memory_enabled` 本来就是 False，记忆对他没意义）。
# ---------------------------------------------------------------------------


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

    为什么要清理：访客没有「我的会话」界面，也就没有人会去删自己的会话。
    他的会话连同 LangGraph 的 checkpoint 会一直留在库里，只增不减。

    两轮判据：

    1. **时间**——最后动静超过 `limits.GUEST_THREAD_TTL_DAYS` 天的；
    2. **数量**——清完还超过 `limits.GUEST_THREAD_MAX` 个的，按「最久没动静」
       往下砍到上限。只靠时间不够：有人能成批造访客身份，时间还没到就把库撑大了。

    **只碰 `g-` 前缀**。登录用户的会话一条都不动——他的数据归他自己删，
    清理程序擅自删掉就是丢数据。CLI 用的默认账号 `"1"` 同理不动。

    没有时间戳的访客会话也算过期：`g-` 前缀是这次改动才引入的，正常路径下
    `create_conversation` 一定写了时间戳；查不到说明记录被外部改过或缺了，
    这种会话也没人认领，删掉比留着占地方好。
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
        # age 从大到小：最久没动静的先走。
        survivors.sort(reverse=True)
        doomed.extend(thread_id for _, thread_id in survivors[:overflow])
        survivors = survivors[overflow:]

    if not doomed:
        return 0

    # checkpoint 要逐个删（每个是一次 SQLite 往返），元数据一次批量写完。
    for thread_id in doomed:
        await agent_core.delete_checkpoint_thread(thread_id)
    conversations.drop_conversations(doomed)
    return len(doomed)


async def startup() -> None:
    """进程启动时的一次性杂务。

    两个都是**同步**的 SQLite 写（各约 30ms），所以都丢进线程池，
    别占着事件循环——和 `resolve_client_location` 里处理阻塞 IO 是同一个理由。

    `reconcile_pending` 是教材库的崩溃恢复：上一次进程可能在解析某本书时退出，
    那本书会永远停在「解析中」。启动时把它标成失败，用户能重传，
    而不是盯着一个永远转不完的状态。

    `purge_stale_guests` 清掉访客留下的过期会话（访客自己不会删，见那个函数）。

    ## 预热 MCP（2026-10-22 加）

    MCP 会话是「第一次用到时才建」的：拉起一个 Python 子进程、握手、拉工具表、
    再 bind 一次模型。这几步加起来是**几百毫秒到两秒**，而它算在
    **TTFT** 里——也就是服务重启后第一个提问的人要白等这一截，
    之后再没人等过（会话是常驻的）。

    启动时主动建一次，这个代价就从「某个随机用户」挪到「服务启动」。
    启动慢一点无所谓，那里没人盯着进度条。

    失败**不拦启动**：常见原因是没配 `TAVILY_API_KEY` 之类，那时真请求会
    给出更具体的错误；为了预热把整个服务拉不起来是本末倒置。
    """
    await asyncio.to_thread(users.purge_expired_sessions)
    await asyncio.to_thread(documents.reconcile_pending)
    await purge_stale_guests()

    try:
        tools = await agent_core.load_tools()
        _log.info("MCP 预热完成，工具 %d 个", len(tools))
    except Exception as exc:  # noqa: BLE001 — 预热失败只是回到「第一个请求等」
        _log.warning("MCP 预热失败（首个请求会自己再试）：%s", exc)
