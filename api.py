from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import service

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 进程退出时收掉 MCP 子进程和 checkpoint 连接，别留下孤儿进程。
    yield
    await service.shutdown()


app = FastAPI(title="Agent Workspace", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend"), name="static")


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


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    thread_id: str | None = None
    user_id: str = "1"
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


@app.get("/api/conversations")
async def conversations() -> list[dict[str, object]]:
    return await service.list_conversations()


@app.post("/api/conversations")
def new_conversation() -> dict[str, str]:
    return service.create_conversation()


@app.delete("/api/conversations/{thread_id}")
async def remove_conversation(thread_id: str) -> dict[str, str]:
    if not await service.delete_conversation(thread_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"thread_id": thread_id, "status": "deleted"}


@app.patch("/api/conversations/{thread_id}")
def rename_conversation(thread_id: str, request: RenameRequest) -> dict[str, str]:
    try:
        return service.rename_conversation(thread_id, request.title)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/conversations/{thread_id}/messages")
async def conversation_messages(thread_id: str) -> dict[str, object]:
    try:
        messages = await service.conversation_messages(thread_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return {"thread_id": thread_id, "messages": messages}


@app.post("/api/conversations/{thread_id}/rewind")
async def rewind_conversation(thread_id: str, request: RewindRequest) -> dict[str, object]:
    try:
        return await service.rewind_conversation(thread_id, request.turn)
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


async def sse_events(request: ChatRequest, thread_id: str) -> AsyncIterator[str]:
    yield f"data: {json.dumps({'type': 'conversation', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
    try:
        # 传纯字典而不是 pydantic 模型：service 层不该依赖 HTTP 层的模型定义。
        client_location = request.location.model_dump() if request.location else None
        async for event in service.chat_events(
            request.user_id, thread_id, request.message, client_location
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    # 这个端点刻意用同步 def：里面只有「建会话」和「构造响应对象」两步本地操作，
    # 真正的异步工作（跑图、MCP 调用）都发生在 sse_events 这个异步生成器里，
    # 由 StreamingResponse 在事件循环上迭代。
    thread_id = request.thread_id or service.create_conversation()["thread_id"]
    return StreamingResponse(
        sse_events(request, thread_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
