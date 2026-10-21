from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import (
    BackgroundTasks,
    Cookie,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import service

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
#
# **只配自己的 logger（"agent"），不动 uvicorn 的**：uvicorn 挂了它自己的
# handler，两边都往 root 挂会让同一条日志打印两遍。
#
# 为什么需要这一层：**最容易出问题的那条路恰好最安静**。聊天流的异常被转成
# SSE 里的一个 error 事件，HTTP 状态仍然是 200，uvicorn 的 access log 也只记
# 200 —— 线上用户说「报错了」，服务端日志里一片正常。管道里能看见的最小证据，
# 就是这里落下的那一行 traceback。
#
# 级别用环境变量控（`LOG_LEVEL=DEBUG` 排障），不用改代码。
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("agent")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        # 不往 root 传播：否则 uvicorn 的 handler 会再打印一遍同样的内容。
        logger.propagate = False
    return logger


logger = _setup_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时顺手清掉过期令牌（没有定时任务，见 service.startup 的说明）。
    await service.startup()
    logger.info("启动完成，日志级别 %s", LOG_LEVEL)
    # 进程退出时收掉 MCP 子进程和 checkpoint 连接，别留下孤儿进程。
    yield
    await service.shutdown()
    logger.info("已退出")


app = FastAPI(title="Agent Workspace", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend"), name="static")


# --------------------------------------------------------------------------
# 上传体积：在读之前就拒绝
# --------------------------------------------------------------------------
#
# 单本 250MB 的检查在 `documents.create_upload` 里，但它发生在文件**全部读进
# 内存之后**。正常情况下这是够的（250MB 峰值内存可以接受），问题是**畸形请求**：
# uvicorn 对请求体没有上限，一个 3GB 的 POST 会先让进程吃掉 3GB，然后才被拒。
#
# 所以这里加一道更早的闸：看 `Content-Length`，超了直接 413，一个字节都不读。
# 没有 `Content-Length`（chunked 编码）时它无能为力——那种情况由
# `read_upload_limited` 边读边数兜底。两道闸缺一不可：
# 前者省资源，后者防绕过。


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """上传接口的请求体上限。只拦 `POST /api/documents`，别的请求不受影响。"""
    if request.method == "POST" and request.url.path == "/api/documents":
        declared = request.headers.get("content-length")
        # 没有这个头、或者不是数字，都不在这里判断：交给边读边数那道闸。
        if declared and declared.isdigit() and int(declared) > service.MAX_UPLOAD_BYTES:
            limit_mb = service.MAX_UPLOAD_BYTES // 1024 // 1024
            return JSONResponse(
                {"detail": f"文件太大，上限 {limit_mb}MB。请求没有开始读取。"},
                status_code=413,
            )
    return await call_next(request)


async def read_upload_limited(file: UploadFile) -> bytes:
    """分块读完上传文件，**边读边数**，超限立刻停。

    为什么不直接 `await file.read()`：那是一次性把整个请求体读进内存，
    上限检查只能在读完之后做，一个畸形请求足以把内存打满。
    这里按 1MB 一块读，累计超过阈值就抛 413——多读的那一块最多 1MB，可忽略。

    返回值仍然是完整 bytes：下游要算内容指纹（sha256）和落盘，
    这两件事都需要完整内容。所以这里的收益是「**不会无限读**」，
    不是「不用全部进内存」——250MB 峰值是明确接受的代价。
    """
    limit = service.MAX_UPLOAD_BYTES
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            limit_mb = limit // 1024 // 1024
            raise HTTPException(status_code=413, detail=f"文件太大，上限 {limit_mb}MB。")
        chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------
# 身份：HTTP 层只做解析，判断交给 service
# --------------------------------------------------------------------------

def client_key(request: Request) -> str:
    """限流用的「来源标识」。

    优先 `X-Forwarded-For` 的第一段：挂在反向代理后面时，直连 IP 全都长一样
    （127.0.0.1），按它限流等于**全站共享一个额度**，一个人就能把所有人挡在门外。

    **这个头是可以伪造的**——攻击者自己塞一段假的就绕过了。当前部署里没有反代，
    伪造的成本和直接换 IP 差不多，所以没为它再加一层「只信可信代理」的校验。
    真要上反代时，得改成「只信任来自代理网段的 XFF」，那时这里必须一起改。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def rate_limited(exc: service.RateLimited) -> HTTPException:
    """把 `RateLimited` 翻成 429，并把「等多久」放进标准头。

    带 `Retry-After` 而不是只在文案里写「稍后再试」：客户端（和任何中间层）
    都能直接照它安排重试，不用去解析中文。
    """
    seconds = max(1, int(exc.retry_after + 0.999))
    return HTTPException(
        status_code=429,
        detail=str(exc),
        headers={"Retry-After": str(seconds)},
    )


def bearer_token(authorization: str | None = Header(default=None)) -> str | None:
    """从 `Authorization: Bearer <令牌>` 里把令牌抠出来。没带头就返回 None（未登录）。

    头**存在但格式不对**时报 401，而不是当没登录：客户端明明想带凭证，
    我们却当成游客，会让「前端加了头但拼错了」表现成「登录态时有时无」，
    极难排查。报错至少指得出问题在哪。
    """
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(
            status_code=401, detail="Authorization 头格式不对，应为 `Bearer <令牌>`"
        )
    return value.strip()


def attach_guest_cookie(
    response: Response, request: Request, guest_id: str | None = None
) -> None:
    """给还没有合法访客 cookie 的浏览器发一个。

    **为什么在 HTML 响应上也发**，而不是只靠 `/api/auth/me`：访客身份按 cookie
    认人，如果某个入口没走到 auth/me（比如直接打开 `/` 就用），他这次请求
    会拿到一个「临时身份」——下一条消息又变新身份，会话立刻断掉（404）。
    首页就把 cookie 落下去，任何后续请求都稳。

    `guest_id` 由调用方给，是为了让「这次请求已经解析出的身份」和「发下去的
    cookie」是同一个 id。不传就现生成一个——那只能用在「这次请求本来就没解析
    身份」的地方（首页）。两处一旦不一致，访客会看到「刚聊的会话不见了」。

    `secure` 跟着实际协议走：本地 http 下如果硬写 `secure=True`，浏览器会
    直接拒收这个 cookie，表现是「每次刷新都变成新访客」。
    """
    if service.verify_guest_cookie(request.cookies.get(service.GUEST_COOKIE)):
        return
    response.set_cookie(
        key=service.GUEST_COOKIE,
        value=service.sign_guest_cookie(guest_id or service.new_guest_id()),
        max_age=service.GUEST_COOKIE_MAX_AGE,
        # HttpOnly：前端 JS 不需要读它，少一个被 XSS 抄走的面。
        httponly=True,
        # Lax 而不是 None：cookie 只用于同站请求，None 会要求 Secure，
        # 在本地 http 下直接失效。
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def current_identity(
    response: Response,
    request: Request,
    token: str | None = Depends(bearer_token),
    guest_cookie: str | None = Cookie(default=None, alias=service.GUEST_COOKIE),
) -> service.Identity:
    """本次请求的身份。**所有端点的 user_id 都只能从这里来。**

    客户端不能再自己声称「我是 user_id=3」——那样任何人都能读别人的会话。
    唯一可信的来源是服务端签发的东西：令牌（登录用户）或签名 cookie（访客）。

    ## cookie 在这里发，不在 auth/me 单独发

    以前只让 `/api/auth/me` 和首页下发，结果**只调 API 的客户端永远拿不到**：
    每个请求都是「临时访客」，上一个请求建的会话下一个请求就 404 了。
    浏览器场景靠首页兜住了，所以这个问题只在 HTTP 客户端上暴露
    ——`test_rename.py` 就是这么撞出来的。

    现在改成「任何走 `current_identity` 的响应都顺带下发」。`StreamingResponse`
    那条路（聊天流）设了也不生效，但它之前必然已经经过一次普通 JSON 请求，
    所以不影响真实使用。
    """
    try:
        identity = service.resolve_identity(token, guest_cookie)
    except service.InvalidToken as exc:
        # 401 而不是降级成访客：令牌过期了还装作访客，用户会看到访客名下的
        # 会话，还以为那是自己的。前端收到 401 就清掉本地令牌、提示重新登录。
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if identity.fresh_guest:
        attach_guest_cookie(response, request, identity.user_id)
    return identity


def logged_in_identity(identity: service.Identity = Depends(current_identity)) -> service.Identity:
    """要求已登录。给「只有注册用户能做」的操作用。"""
    if identity.account is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return identity


class ClientLocation(BaseModel):
    """客户端位置。两种来源，二选一或都给：

    - `latitude` / `longitude`：浏览器 geolocation 授权后拿到的坐标；
    - `city`：用户手动选择/输入的城市，用于**拒绝授权或浏览器不支持**时的兜底。

    优先级由 `service.resolve_client_location` 决定（手选 > 坐标），不在这里管。
    这里只做「防荒唐请求」的粗校验：坐标范围、字段长度。
    """

    city: str | None = Field(default=None, max_length=60, description="用户手选的城市")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class Credentials(BaseModel):
    """注册和登录共用的入参。

    **不在这里写长度/字符集的业务规则**，那些只在 `users.py` 里有一份。
    这里只限一个「防超大请求」的上界（比业务上界宽得多），
    所以「密码至少 8 位」这类提示始终由领域层给出，不会两处措辞不一致。
    """

    account: str = Field(max_length=200)
    password: str = Field(max_length=400)


class Registration(Credentials):
    """注册入参 = 账号密码 + 邀请码。

    `invite` 单独一个模型而不是往 `Credentials` 加字段：登录不需要邀请码，
    加进共用模型会让登录接口也多出一个永远不用的字段，文档里看着像能填。
    """

    invite: str | None = Field(default=None, max_length=64, description="邀请码，首个账号可留空")


class InviteRequest(BaseModel):
    """发邀请码时附带的备注（比如「给室友小李」），方便回头认领。"""

    note: str | None = Field(default=None, max_length=200)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    thread_id: str | None = None
    # 不传就是「用户没有提供位置」——服务端不会拿 IP 去猜，见 service 里的说明。
    location: ClientLocation | None = None
    # 两个逐请求的开关。**默认 True，且 True 时的行为必须和没有这两个字段时
    # 完全一致**——老客户端（含评测脚本）不带它们，拿到的仍是原来的工具表和模型。
    reasoning: bool = True
    web_search: bool = True


class RewindRequest(BaseModel):
    turn: int = Field(ge=0, description="用户消息序号，从 0 开始")


class RenameRequest(BaseModel):
    # 只在这里做「防超大请求」的粗校验；真正的长度规则（截断到 30 字、空白折叠、
    # 非空）统一由 conversations.rename_conversation 负责，避免两处规则打架。
    title: str = Field(min_length=1, max_length=200, description="新的会话标题")


class MemoryUpdate(BaseModel):
    """改一条长期记忆。

    `id` 走 query（它是归一化后的原文，可能含中文，放 body 反而要两处拼）；
    这里只管 `text` / `kind`。清洗规则（折叠空白、截到 120 字、`kind` 白名单）
    仍然只在 `memories.py` 一处，这里只防超大请求。
    """

    text: str = Field(min_length=1, max_length=4000, description="这条记忆的原文")
    kind: str | None = Field(default=None, max_length=20, description="偏好 / 身份 / 计划 / 事实")


class CategoryRequest(BaseModel):
    """新建/改名分类。

    这里**只挡超大请求**（防止有人传 10MB 的 JSON），真正的清洗——折叠空白、
    40 字上限、不能为空——全在 `documents._clean_category_name` 一处。
    要是这里也写 `min_length=1`，空名字会变成 FastAPI 的 422，
    用户看到的是 `{"detail":[{"type":"string_too_short"...}]}` 这种东西，
    而不是「分类名不能为空」。
    """

    name: str = Field(max_length=200)


class DocumentPatch(BaseModel):
    """改一本书的归属。`category_id=None` 表示挪回未分类。

    目前只有这一个可变字段，所以**省略 `category_id` 等价于挪回未分类**，
    不存在「别的字段没改」的歧义。前端每次都显式传，不会踩到这一点。
    """

    category_id: str | None = Field(default=None, max_length=64)


# 端点用 async def 还是 def，取决于它调的 service 函数要不要 await：
# 要 await 的必须 async def（在事件循环里跑）；纯本地文件读写的保持 def，
# FastAPI 会把它们丢进线程池，反而不会占住事件循环。


@app.get("/", response_class=FileResponse)
def index(request: Request) -> FileResponse:
    # no-cache：前端是单文件内联脚本，改完必须让浏览器拿新的。没这个头时
    # 浏览器按启发式缓存旧页，「改了代码但界面没变」会变成排查无解的悬案。
    response = FileResponse(
        BASE_DIR / "frontend" / "index.html",
        headers={"Cache-Control": "no-cache"},
    )
    # 打开首页就把访客 cookie 落下去（理由见 `attach_guest_cookie`）。
    attach_guest_cookie(response, request)
    return response


@app.get("/library", response_class=FileResponse)
def library_page(request: Request) -> FileResponse:
    """知识库管理页。

    单独一页而不是在主界面里开个更大的浮层：文档列表 + 分类 + 上传 + 解析状态
    挤在 260px 的侧栏里，行高只够放书名，分类和状态都得靠悬浮提示——
    上传一本书要来回开关好几次浮层。这一页要的是横向空间（左侧分类、右侧列表）。
    """
    response = FileResponse(
        BASE_DIR / "frontend" / "library.html",
        headers={"Cache-Control": "no-cache"},
    )
    attach_guest_cookie(response, request)
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/capabilities")
def capabilities() -> dict[str, bool]:
    """前端该把哪些开关显示成可点。

    存在的理由只有一条：**别让按钮骗人**。`reasoning` 这个参数是走各家私有的
    非标准字段发的（deepseek 叫 `reasoning_effort`、智谱叫 `thinking`），
    换到一个不认它的 provider 时它会被静默忽略——按钮点了没反应，
    用户只会以为是模型卡了。所以能不能关由服务端说了算，前端据此置灰。
    同理联网工具依赖 TAVILY_API_KEY，没配就是没有，按钮也不该亮着。

    具体判定放在 service 里：这里是路由层，不该自己去读环境变量或直接摸 agent_core。
    """
    return service.capabilities()


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------

@app.post("/api/auth/register")
def register(request: Registration, http: Request) -> dict[str, object]:
    """注册并直接登录，返回用户信息 + 令牌。账号重名或格式不合规 → 400。

    注册完直接给令牌，前端不用再走一次登录（少一次 PBKDF2，省约 110ms）。

    `invite` 只有在「站里已经有人」时才必填，判定在领域层——路由不该去数用户。
    缺码和码不对是两种提示，都由领域层的异常文案带出来，这里不重写。

    注册也限流：PBKDF2 一次 110ms，不挡的话一个循环就能把 CPU 占满。
    """
    try:
        service.limit_login(client_key(http))
    except service.RateLimited as exc:
        raise rate_limited(exc) from exc
    try:
        return service.register_user(request.account, request.password, request.invite)
    except ValueError as exc:
        # 「账号重名」「账号/密码不合规」「邀请码缺失/不对」都是 ValueError 的子类，
        # 对客户端一律是「你填的东西不行」→ 400。文案由领域层给。
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/auth/registration-policy")
def registration_policy() -> dict[str, object]:
    """注册这块的规矩：要不要邀请码。

    前端需要它才能决定注册表单里显不显示「邀请码」那一栏。
    **不能让前端自己猜**——猜「第一个用户不用填」就得先知道库里几个人，
    那是服务端的事实。
    """
    return service.registration_policy()


@app.post("/api/auth/login")
def login(request: Credentials, http: Request) -> dict[str, object]:
    """登录，返回用户信息 + 令牌。

    **账号不存在和密码错误返回同一句话**，不给攻击者做「这个账号注册了没有」的探测。

    按来源限流：不挡的话这里就是一个可以被无限次试密码的入口，顺带每次白烧 110ms CPU。
    """
    try:
        service.limit_login(client_key(http))
    except service.RateLimited as exc:
        raise rate_limited(exc) from exc
    try:
        return service.login(request.account, request.password)
    except service.InvalidCredentials as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/auth/logout")
def logout(token: str | None = Depends(bearer_token)) -> dict[str, bool]:
    """登出：撤销当前令牌。

    刻意**不要求已登录**：令牌已经过期时，前端仍然应该能「登出」成功，
    好把本地状态清干净。否则用户会卡在一个点不动的按钮上。
    """
    return {"revoked": service.logout(token)}


@app.get("/api/auth/me")
def me(identity: service.Identity = Depends(current_identity)) -> dict[str, object]:
    """当前身份。**访客不是错误**：前端靠这个端点判断自己还在不在登录状态。

    `is_owner` 一起返回，前端才知道要不要给他看「发邀请码」的入口——
    否则每个登录用户都会看到一个点进去挨 403 的按钮。

    访客 cookie 由 `current_identity` 统一下发（任何普通响应都会带），
    这里不再单独处理——两处都发会写出重复的 `Set-Cookie`。
    """
    return {
        "user_id": identity.user_id,
        "account": identity.account,
        "guest": identity.account is None,
        "memory_enabled": identity.memory_enabled,
        "is_owner": service.is_owner(identity.user_id),
    }


# --------------------------------------------------------------------------
# 邀请码（F1）：只有站点主人能发、能看
# --------------------------------------------------------------------------

@app.post("/api/invites")
def create_invite(
    payload: InviteRequest | None = None,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, object]:
    """发一个一次性邀请码。不是主人 → 403。

    这里用 403 而不是像会话那样装 404：邀请码入口本身不是秘密，
    没什么可隐藏的，含糊其辞只会让人反复重试。
    """
    try:
        note = payload.note if payload else None
        return service.create_invite(identity.user_id, note)
    except service.NotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/invites")
def list_invites(
    identity: service.Identity = Depends(logged_in_identity),
) -> list[dict[str, object]]:
    """我发出去的邀请码，新的在前。每条带 `used`，界面好区分「还没用」和「已用掉」。"""
    try:
        return service.invites_of(identity.user_id)
    except service.NotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.delete("/api/invites/{code}")
def delete_invite(
    code: str,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, str]:
    """删掉一个邀请码。**删掉即作废**，没被用的话立刻失效。

    没删到 → 404（不存在、或者不是这个人发的，两种情况回同一句）。
    这里沿用 `GET /api/invites` 的 403：邀请码这个入口本身不是秘密，
    没必要像会话那样装不存在；但**码本身**要不要暴露给调用方就是另一回事了，
    所以「不是你发的」按不存在处理。

    已用掉的码也允许删，代价是「这个账号是通过哪个码进来的」查不到了。
    这个提示放在前端确认框里，接口不拦——拦了的话列表永远清不干净。
    """
    try:
        removed = service.delete_invite(identity.user_id, code)
    except service.NotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    return {"code": code, "status": "deleted"}


# --------------------------------------------------------------------------
# 会话（全部按 current_identity 过滤）
# --------------------------------------------------------------------------

@app.get("/api/conversations")
async def conversations(
    identity: service.Identity = Depends(current_identity),
) -> list[dict[str, object]]:
    return await service.list_conversations(identity.user_id)


@app.post("/api/conversations")
def new_conversation(
    identity: service.Identity = Depends(current_identity),
) -> dict[str, str]:
    return service.create_conversation(identity.user_id)


@app.delete("/api/conversations/{thread_id}")
async def remove_conversation(
    thread_id: str,
    identity: service.Identity = Depends(current_identity),
) -> dict[str, str]:
    if not await service.delete_conversation(thread_id, identity.user_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"thread_id": thread_id, "status": "deleted"}


@app.patch("/api/conversations/{thread_id}")
def rename_conversation(
    thread_id: str,
    request: RenameRequest,
    identity: service.Identity = Depends(current_identity),
) -> dict[str, str]:
    try:
        return service.rename_conversation(thread_id, request.title, identity.user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/conversations/{thread_id}/messages")
async def conversation_messages(
    thread_id: str,
    identity: service.Identity = Depends(current_identity),
) -> dict[str, object]:
    try:
        messages = await service.conversation_messages(thread_id, identity.user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return {"thread_id": thread_id, "messages": messages}


@app.post("/api/conversations/{thread_id}/rewind")
async def rewind_conversation(
    thread_id: str,
    request: RewindRequest,
    identity: service.Identity = Depends(current_identity),
) -> dict[str, object]:
    try:
        return await service.rewind_conversation(thread_id, request.turn, identity.user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/location")
async def resolve_location(location: ClientLocation) -> dict[str, str | None]:
    """把客户端位置解析成可读的城市名，供前端立刻显示「定位到：北京市」。

    单独开这个端点，是因为聊天接口是 SSE 流，坐标在那一轮里解析完只会进模型上下文，
    前端拿不到结果去回显。解析规则和聊天链路共用同一个 service 函数，不会两边不一致。

    解析不出来返回 `{"location": null}`，不是错误——用户没授权、或坐标落在海上，
    都不是异常情况，前端据此提示用户手动选城市。
    """
    return {"location": await service.resolve_client_location(location.model_dump())}


async def sse_events(
    request: ChatRequest, thread_id: str, identity: service.Identity
) -> AsyncIterator[str]:
    yield f"data: {json.dumps({'type': 'conversation', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
    try:
        # 传纯字典而不是 pydantic 模型：service 层不该依赖 HTTP 层的模型定义。
        client_location = request.location.model_dump() if request.location else None
        async for event in service.chat_events(
            identity.user_id,
            thread_id,
            request.message,
            client_location,
            memory_enabled=identity.memory_enabled,
            reasoning=request.reasoning,
            web_search=request.web_search,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as exc:
        # **同时记一条日志**。这里是最容易「静默失败」的地方：异常被转成流里的
        # 一个 error 事件，HTTP 仍然是 200，uvicorn 的 access log 也只记 200。
        # 没有这行日志的话，线上用户说「报错了」而服务端日志一片正常。
        logger.exception("聊天流失败 thread=%s user=%s", thread_id, identity.user_id)
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(
    request: ChatRequest,
    http: Request,
    identity: service.Identity = Depends(current_identity),
) -> StreamingResponse:
    # 这个端点刻意用同步 def：里面只有「定会话」和「构造响应对象」两步本地操作，
    # 真正的异步工作（跑图、MCP 调用）都发生在 sse_events 这个异步生成器里，
    # 由 StreamingResponse 在事件循环上迭代。
    #
    # 归属检查必须在这里做（而不是在生成器里）：SSE 一开流，响应头就已经发出去了，
    # 之后发现越权只能往流里塞一个 error 事件，前端拿到的是 200——
    # 语义上很别扭，客户端也没法用状态码统一处理。
    #
    # 额度也在开流之前扣：一条消息要跑模型、要花钱，一分钟刷几十条能把一个月的
    # 用量一天烧完。而且扣在这里判不上，前端能拿到规规矩矩的 429 而不是流里的一句话。
    #
    # `client_key` 只给访客那一层用（访客清 cookie 就是新身份，必须按 IP 兜一道），
    # 登录用户传了也不会被用到——判定在 `service.take_ask_slot` 里。
    try:
        service.take_ask_slot(identity.user_id, client_key(http))
    except service.RateLimited as exc:
        raise rate_limited(exc) from exc
    except service.QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    try:
        thread_id = service.begin_chat(identity.user_id, request.thread_id)
    except KeyError:
        # 会话不是他的（→ 404）。这一条消息根本没发出去，那次额度得退回去，
        # 不然「点了别人的会话」也会白白烧掉一次今天的次数。
        service.refund_ask_slot(identity.user_id)
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return StreamingResponse(
        sse_events(request, thread_id, identity),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# 教材库
# --------------------------------------------------------------------------
#
# 三个端点都要**登录**（`logged_in_identity`），不像会话那样允许游客。
# 理由不是「教材更贵重」，而是游客共用同一个默认 user_id——让游客传书，
# 等于把 A 的教材放进 B 也能看到的同一个库里。会话那边游客互相看见早就存在，
# 但那是历史包袱，不该再往新功能上复制一遍。


@app.post("/api/documents")
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, object]:
    """上传一本教材，**立刻返回**（状态 `pending`），解析在响应发出之后开始。

    为什么不用同步返回解析结果：一本 300 页的 PDF 要几十秒，浏览器和反向代理
    都会超时。而「上传成功、正在解析」本身就是有用的反馈——前端轮询列表就能
    看到状态推进，这是 A2 的验收标准。

    解析失败不回滚这条记录（不回 400）：文件已经收下了，失败原因要留在列表里
    让用户看见（「这本书第 12 页结构损坏」比「上传失败」有用得多）。
    """
    data = await read_upload_limited(file)
    try:
        # 先查「这个人还能不能再存一本」，再收文件。顺序反了的话，书数已满的人
        # 还是要把几十 MB 传上来才被告知存不下——白费他的流量和我们的磁盘。
        service.check_document_quota(identity.user_id)
        record = service.upload_document(identity.user_id, file.filename, data)
    except service.QuotaExceeded as exc:
        # 书太多了。和「文件太大」一样归到 400：都是「当前这个请求收不了」，
        # 不是限流（换成明天也不会变）。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.DocumentError as exc:
        # 格式不支持 / 太大 / 空文件。都是「你给的东西不行」→ 400，文案由领域层给。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not record.get("duplicated"):
        # 重复上传**不重新解析**：原来那份已经好了，重解析会让这期间产生的
        # chunk_id 全部失配（引用指向不存在的段落）。
        background.add_task(
            service.parse_uploaded_document, str(record["doc_id"]), identity.user_id
        )
    return record


@app.get("/api/documents")
async def list_documents(
    identity: service.Identity = Depends(logged_in_identity),
) -> list[dict[str, object]]:
    return await service.list_documents(identity.user_id)


@app.delete("/api/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, str]:
    if not await service.delete_document(doc_id, identity.user_id):
        raise HTTPException(status_code=404, detail="教材不存在")
    return {"doc_id": doc_id, "status": "deleted"}


@app.get("/api/documents/{doc_id}/markdown")
async def document_markdown(
    doc_id: str,
    render: bool = False,
    identity: service.Identity = Depends(logged_in_identity),
) -> Response:
    """教材的 Markdown，带 `<!-- page:N -->` / `<!-- chapter:X -->` 锚点。

    两种取法，**同一个端点**（前端切换时不必记两个路径）：

    - 默认 `render=false`：原文返回，图引用是 `as_77.png` 这样的相对文件名。
      用于「看锚点、看解析质量」——存进去什么样就拿出来什么样。
    - `render=true`：图引用换成带签名的图片 URL（`/api/assets/as_77?u=..&t=..`），
      这份文本交给 Markdown 渲染器就能**原地出图**。

    两种都用 `text/plain` 而不是 `text/markdown`：浏览器对 `text/markdown`
    的处理不一致（有的当附件下载），纯文本一定在页面里显示。
    """
    text = await service.document_markdown(doc_id, identity.user_id, with_assets=render)
    if text is None:
        raise HTTPException(status_code=404, detail="教材不存在或还没解析完成")
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "private, no-store" if render else "private, max-age=60"},
    )


@app.patch("/api/documents/{doc_id}")
async def patch_document(
    doc_id: str,
    request: DocumentPatch,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, object]:
    """把一本书挪到某个分类下（或挪回未分类）。

    为什么放在 `PATCH /api/documents/{doc_id}` 而不是
    `POST /api/categories/{id}/documents`：归属是**书身上的属性**，
    一本书同时只能在一个分类里。挂在书上，「改归属」就永远只有一个入口，
    不会出现「从 A 分类加进去」和「从 B 分类加进去」两条路打架。
    """
    outcome = await service.assign_document(doc_id, identity.user_id, request.category_id)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="教材不存在")
    if outcome == "no_category":
        # 分类不存在和分类是别人的，回同一句话：区分开就等于告诉对方
        # 「这个 id 存在，只是不归你」，没必要。
        raise HTTPException(status_code=404, detail="分类不存在")
    return {"doc_id": doc_id, "category_id": request.category_id}


@app.get("/api/categories")
async def list_categories(
    identity: service.Identity = Depends(logged_in_identity),
) -> list[dict[str, object]]:
    """这个人的全部分类，每个带 `doc_count`。"""
    return await service.list_categories(identity.user_id)


@app.post("/api/categories", status_code=201)
async def create_category(
    request: CategoryRequest,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, object]:
    """新建分类。

    `CategoryExists` 必须排在 `ValueError` 前面接：它是 ValueError 的子类，
    顺序反了「重名」会被当成「名字不合法」回 400，前端提示「分类名不能为空」，
    而用户明明填了名字——这种错配极难自查。
    """
    try:
        return await service.create_category(identity.user_id, request.name)
    except service.CategoryExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/categories/{category_id}")
async def rename_category(
    category_id: str,
    request: CategoryRequest,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, str]:
    """改分类名。`missing` → 404（该刷新），`clash` → 409（该换个名字）。"""
    try:
        outcome = await service.rename_category(category_id, identity.user_id, request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="分类不存在")
    if outcome == "clash":
        raise HTTPException(status_code=409, detail="已经有同名的分类了")
    return {"category_id": category_id, "status": "renamed"}


@app.delete("/api/categories/{category_id}")
async def delete_category(
    category_id: str,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, str]:
    """删分类。**类下的书退回未分类，不跟着删**。

    返回里带 `documents: unfiled` 是给前端提示用的——用户点删分类时最怕的
    就是「我这一类的 8 本书是不是也没了」，把结果写进响应就不必靠猜。
    """
    if not await service.delete_category(category_id, identity.user_id):
        raise HTTPException(status_code=404, detail="分类不存在")
    return {"category_id": category_id, "status": "deleted", "documents": "unfiled"}


@app.get("/api/memories")
async def list_memories(
    identity: service.Identity = Depends(logged_in_identity),
) -> list[dict[str, object]]:
    """AI 记住的关于你的东西，按记下来的先后排。

    游客拿不到（401）：记忆对游客本来就是关的，给他一个空列表会让人以为
    「登录也不会多什么」。每条带一个 `id`，删除和修改都拿它定位。
    """
    return await service.list_memories(identity.user_id)


@app.delete("/api/memories/{memory_id}")
async def delete_memory(
    memory_id: str,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, str]:
    """删掉一条。`memory_id` 是列表接口给的那个 `id`。

    放路径里而不是 query：它是 12 位十六进制，不会有需要转义的字符，
    路径更短也更好看。**别改成拿原文当 id**——原文差一个标点就是另一条了。
    """
    if not memory_id.strip():
        raise HTTPException(status_code=400, detail="缺少要删除的条目 id")
    if not await service.forget_memory(identity.user_id, memory_id):
        raise HTTPException(status_code=404, detail="这条记忆不存在")
    return {"status": "deleted"}


@app.patch("/api/memories/{memory_id}")
async def update_memory(
    memory_id: str,
    payload: MemoryUpdate,
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, str]:
    """改一条的原文。三个结果分开报：

    - 404：这条不存在（可能刚在别处删掉了）；
    - 409：改完和已有的一条重了，界面该提示「已经有这条了」而不是静默合并；
    - 200：改好了。**`id` 不变**，前端不用重新拉列表再记一遍编号。
    """
    if not memory_id.strip():
        raise HTTPException(status_code=400, detail="缺少要修改的条目 id")
    outcome = await service.update_memory(
        identity.user_id, memory_id, payload.text, payload.kind
    )
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="这条记忆不存在")
    if outcome == "clash":
        raise HTTPException(status_code=409, detail="已经有这条了，不用再改一次")
    return {"status": "updated"}


@app.get("/api/assets/{asset_id}")
async def asset(
    asset_id: str,
    u: str = "",
    t: str = "",
) -> FileResponse:
    """教材插图。**用签名鉴权，不用令牌**——`<img>` 不会带 Authorization 头。

    参数 `u` / `t` 由 `service.sign_asset` 生成（`u` 是 user_id，`t` 是
    `过期时间.签名`，签名覆盖了 asset_id 和 u，所以改任何一个都验不过）。

    这里刻意**不接 `Depends(current_identity)`**：接了就要求带令牌，而浏览器
    加载图片时带不上，结果是一律 401。签名本身就是这次的凭证。
    """
    if not service.verify_asset(asset_id, u, t):
        raise HTTPException(status_code=403, detail="图片链接无效或已过期")
    record = await service.get_asset(asset_id, u)
    if record is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    path = Path(str(record["file_path"]))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片文件已丢失")
    return FileResponse(
        path,
        media_type="image/png",
        # 私有缓存：一张图在同一次会话里会被反复渲染，但**不能进 CDN 或共享缓存**。
        headers={"Cache-Control": "private, max-age=3600"},
    )
