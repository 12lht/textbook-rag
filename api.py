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
        logger.propagate = False
    return logger


logger = _setup_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await service.startup()
    logger.info("启动完成，日志级别 %s", LOG_LEVEL)
    yield
    await service.shutdown()
    logger.info("已退出")


app = FastAPI(title="Agent Workspace", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend"), name="static")




@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """上传接口的请求体上限。只拦 `POST /api/documents`，别的请求不受影响。"""
    if request.method == "POST" and request.url.path == "/api/documents":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > service.MAX_UPLOAD_BYTES:
            limit_mb = service.MAX_UPLOAD_BYTES // 1024 // 1024
            return JSONResponse(
                {"detail": f"文件太大，上限 {limit_mb}MB。请求没有开始读取。"},
                status_code=413,
            )
    return await call_next(request)


async def read_upload_limited(file: UploadFile) -> bytes:
    """分块读完上传文件，**边读边数**，超限立刻停。

    不用 `await file.read()`：那是一次把整个请求体读进内存，上限只能读完再查，一个畸形请求
    足以打满内存。这里按 1MB 一块读，累计超阈值就抛 413（多读的那块最多 1MB）。

    返回值仍是完整 bytes——下游要算 sha256 指纹和落盘。所以收益是「**不会无限读**」，
    不是「不全进内存」：250MB 峰值是明确接受的代价。
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



def client_key(request: Request) -> str:
    """限流用的「来源标识」。优先 `X-Forwarded-For` 第一段：挂反代时直连 IP 全是 127.0.0.1，
    按它限流等于全站共享一个额度。

    **这个头可以伪造**。当前部署没有反代，伪造成本和换 IP 差不多；真要上反代，得改成
    「只信任来自代理网段的 XFF」，那时这里必须一起改。
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
    """从 `Authorization: Bearer <令牌>` 里抠出令牌，没带头返回 None（未登录）。

    头**存在但格式不对**时报 401，而不是当没登录：否则「前端加了头但拼错了」会表现成
    「登录态时有时无」，极难排查。
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

    HTML 响应上也要发：访客靠 cookie 认人，某个入口没走 auth/me（比如直接打开 `/`）就会
    拿到临时身份，下一条消息又变新身份、会话立刻 404。首页就落 cookie，后续请求才稳。

    `guest_id` 由调用方给，保证「这次解析出的身份」和「发下去的 cookie」是同一个 id。
    `secure` 跟着实际协议走：本地 http 硬写 True 会被浏览器拒收，表现是每次刷新都变新访客。
    """
    if service.verify_guest_cookie(request.cookies.get(service.GUEST_COOKIE)):
        return
    response.set_cookie(
        key=service.GUEST_COOKIE,
        value=service.sign_guest_cookie(guest_id or service.new_guest_id()),
        max_age=service.GUEST_COOKIE_MAX_AGE,
        httponly=True,
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

    客户端不能自己声称「我是 user_id=3」——那样谁都能读别人的会话。唯一可信的来源是服务端
    签发的东西：令牌（登录用户）或签名 cookie（访客）。

    任何走这里的响应都顺带下发访客 cookie。以前只让 auth/me 和首页下发，结果只调 API 的
    客户端永远拿不到，每个请求都是新访客、上一个请求建的会话下一个就 404。
    `StreamingResponse` 设了不生效，但它之前必然经过一次普通 JSON 请求。
    """
    try:
        identity = service.resolve_identity(token, guest_cookie)
    except service.InvalidToken as exc:
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
    """客户端位置，两种来源二选一或都给：`latitude` / `longitude`（浏览器授权坐标）、
    `city`（手选城市，用于拒绝授权或浏览器不支持时兜底）。

    优先级由 `service.resolve_client_location` 决定，这里只做「防荒唐请求」的粗校验。
    """

    city: str | None = Field(default=None, max_length=60, description="用户手选的城市")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class Credentials(BaseModel):
    """注册和登录共用的入参。**不写长度 / 字符集规则**——那些只在 `users.py` 有一份，
    这里只限一个比业务上界宽得多的上界，提示文案才不会被两处写得不一样。
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
    location: ClientLocation | None = None
    reasoning: bool = True
    web_search: bool = True


class RewindRequest(BaseModel):
    turn: int = Field(ge=0, description="用户消息序号，从 0 开始")


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="新的会话标题")


class MemoryUpdate(BaseModel):
    """改一条长期记忆。`id` 走 query，这里只管 `text` / `kind`。

    清洗规则（折叠空白、截到 120 字、`kind` 白名单）只在 `memories.py` 一处，这里只防超大请求。
    """

    text: str = Field(min_length=1, max_length=4000, description="这条记忆的原文")
    kind: str | None = Field(default=None, max_length=20, description="偏好 / 身份 / 计划 / 事实")


class CategoryRequest(BaseModel):
    """新建 / 改名分类。**只挡超大请求**，清洗（折叠空白、40 字上限、不能为空）全在
    `documents._clean_category_name` 一处。

    这里若写 `min_length=1`，空名字会变成 FastAPI 的 422 和一段 `string_too_short` 的 JSON，
    而不是「分类名不能为空」。
    """

    name: str = Field(max_length=200)


class DocumentPatch(BaseModel):
    """改一本书的归属。`category_id=None` 表示挪回未分类。

    目前只有这一个可变字段，所以**省略 `category_id` 等价于挪回未分类**，
    不存在「别的字段没改」的歧义。前端每次都显式传，不会踩到这一点。
    """

    category_id: str | None = Field(default=None, max_length=64)




@app.get("/", response_class=FileResponse)
def index(request: Request) -> FileResponse:
    response = FileResponse(
        BASE_DIR / "frontend" / "index.html",
        headers={"Cache-Control": "no-cache"},
    )
    attach_guest_cookie(response, request)
    return response


@app.get("/library", response_class=FileResponse)
def library_page(request: Request) -> FileResponse:
    """知识库管理页。单独一页而不是主界面里开个更大的浮层：文档列表 + 分类 + 上传 + 解析状态
    挤在 260px 的侧栏里只放得下书名，上传一本书要来回开关好几次浮层。
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
    """前端该把哪些开关显示成可点。理由只有一条：**别让按钮骗人**。

    关推理走各家私有字段（deepseek 叫 `reasoning_effort`、智谱叫 `thinking`），不认它的
    provider 会静默忽略；联网工具依赖 `TAVILY_API_KEY`，没配就是没有。判定放在 service——
    路由层不该自己读环境变量或直接摸 agent_core。
    """
    return service.capabilities()



@app.post("/api/auth/register")
def register(request: Registration, http: Request) -> dict[str, object]:
    """注册并直接登录，返回用户信息 + 令牌。账号重名或格式不合规 → 400。

    `invite` 只在站里已经有人时才必填（判定在领域层，路由不数用户），缺码和码不对的文案由
    领域层异常带出来。注册也限流：一次 PBKDF2 110ms，不挡的话一个循环就能把 CPU 占满。
    """
    try:
        service.limit_login(client_key(http))
    except service.RateLimited as exc:
        raise rate_limited(exc) from exc
    try:
        return service.register_user(request.account, request.password, request.invite)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/auth/registration-policy")
def registration_policy() -> dict[str, object]:
    """注册这块要不要邀请码。前端靠它决定注册表单显不显示那一栏——不能让前端自己猜
    「库里几个人」，那是服务端的事实。
    """
    return service.registration_policy()


@app.post("/api/auth/login")
def login(request: Credentials, http: Request) -> dict[str, object]:
    """登录，返回用户信息 + 令牌。**账号不存在和密码错误返回同一句话**，不给探测账号是否存在。

    按来源限流：否则这里就是可以被无限次试密码的入口，每次还白烧 110ms CPU。
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
    """当前身份。**访客不是错误**——前端靠它判断自己还在不在登录状态。

    `is_owner` 一起返回，前端才知道要不要显示「发邀请码」入口。访客 cookie 由
    `current_identity` 统一下发，这里不再单独处理（两处都发会写出重复的 Set-Cookie）。
    """
    return {
        "user_id": identity.user_id,
        "account": identity.account,
        "guest": identity.account is None,
        "memory_enabled": identity.memory_enabled,
        "is_owner": service.is_owner(identity.user_id),
    }



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
    """删掉一个邀请码。**删除即作废**，没被用的话立刻失效。

    没删到 → 404（不存在、或不是这个人发的，同一句）。这里沿用 `GET /api/invites` 的 403：
    入口本身不是秘密，不必像会话那样装不存在；但**码本身**要不要暴露是另一回事。

    已用掉的码也允许删，代价是「这个账号用哪个码进来的」查不到了。提示放前端确认框，
    接口不拦——拦了的话列表永远清不干净。
    """
    try:
        removed = service.delete_invite(identity.user_id, code)
    except service.NotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    return {"code": code, "status": "deleted"}



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
    """把客户端位置解析成可读城市名，供前端立刻显示「定位到：北京市」。

    单独开这个端点是因为聊天接口是 SSE，坐标解析完只会进模型上下文，前端拿不到结果去回显。
    解析不出来返回 `{"location": null}`，不是错误——用户没授权、或坐标落在海上都正常。
    """
    return {"location": await service.resolve_client_location(location.model_dump())}


async def sse_events(
    request: ChatRequest, thread_id: str, identity: service.Identity
) -> AsyncIterator[str]:
    yield f"data: {json.dumps({'type': 'conversation', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
    try:
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
        logger.exception("聊天流失败 thread=%s user=%s", thread_id, identity.user_id)
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(
    request: ChatRequest,
    http: Request,
    identity: service.Identity = Depends(current_identity),
) -> StreamingResponse:
    try:
        service.take_ask_slot(identity.user_id, client_key(http))
    except service.RateLimited as exc:
        raise rate_limited(exc) from exc
    except service.QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    try:
        thread_id = service.begin_chat(identity.user_id, request.thread_id)
    except KeyError:
        service.refund_ask_slot(identity.user_id)
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return StreamingResponse(
        sse_events(request, thread_id, identity),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )




@app.post("/api/documents")
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    identity: service.Identity = Depends(logged_in_identity),
) -> dict[str, object]:
    """上传一本教材，**立刻返回**（状态 `pending`），解析在响应发出之后开始。

    同步等解析会几十秒，浏览器和反向代理都会超时。解析失败不回滚记录、也不回 400：文件已经
    收下，失败原因要留在列表里（「第 12 页结构损坏」比「上传失败」有用得多）。
    """
    data = await read_upload_limited(file)
    try:
        service.check_document_quota(identity.user_id)
        record = service.upload_document(identity.user_id, file.filename, data)
    except service.QuotaExceeded as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.DocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not record.get("duplicated"):
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

    同一个端点两种取法（前端切换时不必记两个路径）：默认 `render=false` 返回原文，图引用是
    `as_77.png` 这样的相对文件名，用于看锚点和解析质量；`render=true` 把图引用换成带签名的
    图片 URL（`/api/assets/as_77?u=..&t=..`），交给 marked 就能直接渲染。

    不在存储里存签名 URL：签名一小时过期，存进去的书第二天全是破图。
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
        headers={"Cache-Control": "private, max-age=3600"},
    )
