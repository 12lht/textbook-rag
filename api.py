from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import service

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时顺手清掉过期令牌（没有定时任务，见 service.startup 的说明）。
    await service.startup()
    # 进程退出时收掉 MCP 子进程和 checkpoint 连接，别留下孤儿进程。
    yield
    await service.shutdown()


app = FastAPI(title="Agent Workspace", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend"), name="static")


# --------------------------------------------------------------------------
# 身份：HTTP 层只做解析，判断交给 service
# --------------------------------------------------------------------------

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


def current_identity(token: str | None = Depends(bearer_token)) -> service.Identity:
    """本次请求的身份。**所有端点的 user_id 都只能从这里来。**

    客户端不能再自己声称「我是 user_id=3」——那样任何人都能读别人的会话。
    唯一可信的来源是服务端签发的令牌，所以 `ChatRequest` 里的 `user_id` 被删掉了。
    """
    try:
        return service.resolve_identity(token)
    except service.InvalidToken as exc:
        # 401 而不是降级成游客：令牌过期了还装作游客，用户会看到游客名下的
        # 会话，还以为那是自己的。前端收到 401 就清掉本地令牌、提示重新登录。
        raise HTTPException(status_code=401, detail=str(exc)) from exc


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


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    thread_id: str | None = None
    # 不传就是「用户没有提供位置」——服务端不会拿 IP 去猜，见 service 里的说明。
    location: ClientLocation | None = None


class RewindRequest(BaseModel):
    turn: int = Field(ge=0, description="用户消息序号，从 0 开始")


class RenameRequest(BaseModel):
    # 只在这里做「防超大请求」的粗校验；真正的长度规则（截断到 30 字、空白折叠、
    # 非空）统一由 conversations.rename_conversation 负责，避免两处规则打架。
    title: str = Field(min_length=1, max_length=200, description="新的会话标题")


# 端点用 async def 还是 def，取决于它调的 service 函数要不要 await：
# 要 await 的必须 async def（在事件循环里跑）；纯本地文件读写的保持 def，
# FastAPI 会把它们丢进线程池，反而不会占住事件循环。


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "frontend" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------

@app.post("/api/auth/register")
def register(request: Credentials) -> dict[str, object]:
    """注册并直接登录，返回用户信息 + 令牌。账号重名或格式不合规 → 400。

    注册完直接给令牌，前端不用再走一次登录（少一次 PBKDF2，省约 110ms）。
    """
    try:
        return service.register_user(request.account, request.password)
    except ValueError as exc:
        # 「账号重名」和「账号/密码不合规」都是 ValueError 的子类，对客户端一律是
        # 「你填的东西不行」→ 400。两者的文案由领域层给，这里不重写。
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auth/login")
def login(request: Credentials) -> dict[str, object]:
    """登录，返回用户信息 + 令牌。

    **账号不存在和密码错误返回同一句话**，不给攻击者做「这个账号注册了没有」的探测。
    """
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
    """当前身份。**游客不是错误**：前端靠这个端点判断自己还在不在登录状态。"""
    return {
        "user_id": identity.user_id,
        "account": identity.account,
        "guest": identity.account is None,
        "memory_enabled": identity.memory_enabled,
    }


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
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(
    request: ChatRequest,
    identity: service.Identity = Depends(current_identity),
) -> StreamingResponse:
    # 这个端点刻意用同步 def：里面只有「定会话」和「构造响应对象」两步本地操作，
    # 真正的异步工作（跑图、MCP 调用）都发生在 sse_events 这个异步生成器里，
    # 由 StreamingResponse 在事件循环上迭代。
    #
    # 归属检查必须在这里做（而不是在生成器里）：SSE 一开流，响应头就已经发出去了，
    # 之后发现越权只能往流里塞一个 error 事件，前端拿到的是 200——
    # 语义上很别扭，客户端也没法用状态码统一处理。
    try:
        thread_id = service.begin_chat(identity.user_id, request.thread_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return StreamingResponse(
        sse_events(request, thread_id, identity),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
