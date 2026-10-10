from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import service

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Agent Workspace", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend"), name="static")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    thread_id: str | None = None
    user_id: str = "1"


class RewindRequest(BaseModel):
    turn: int = Field(ge=0, description="用户消息序号，从 0 开始")


class RenameRequest(BaseModel):
    # 只在这里做「防超大请求」的粗校验；真正的长度规则（截断到 30 字、空白折叠、
    # 非空）统一由 conversations.rename_conversation 负责，避免两处规则打架。
    title: str = Field(min_length=1, max_length=200, description="新的会话标题")


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "frontend" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/conversations")
def conversations() -> list[dict[str, object]]:
    return service.list_conversations()


@app.post("/api/conversations")
def new_conversation() -> dict[str, str]:
    return service.create_conversation()


@app.delete("/api/conversations/{thread_id}")
def remove_conversation(thread_id: str) -> dict[str, str]:
    if not service.delete_conversation(thread_id):
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
def conversation_messages(thread_id: str) -> dict[str, object]:
    try:
        messages = service.conversation_messages(thread_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return {"thread_id": thread_id, "messages": messages}


@app.post("/api/conversations/{thread_id}/rewind")
def rewind_conversation(thread_id: str, request: RewindRequest) -> dict[str, object]:
    try:
        return service.rewind_conversation(thread_id, request.turn)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def sse_events(request: ChatRequest, thread_id: str) -> Iterator[str]:
    yield f"data: {json.dumps({'type': 'conversation', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
    try:
        for event in service.chat_events(request.user_id, thread_id, request.message):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    thread_id = request.thread_id or service.create_conversation()["thread_id"]
    return StreamingResponse(
        sse_events(request, thread_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
