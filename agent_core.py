"""Agent 编排层：LangGraph 图、模型，以及读写会话「消息状态」的操作。

工具分两处：4 个纯函数工具（天气 / 时间 / 搜索 / 网页提取）在 `tools.py`，由
`mcp_server.py` 暴露成 stdio MCP server；需要本次请求上下文的 3 个工具（定位、
读写记忆）留在本模块，记忆后端由 `service.set_memory_backend()` 注入。

MCP 客户端是异步的，所以 `stream_agent` / `generate_title` 和所有状态操作
（`get_history` / `rewind_thread` / ...）都是 async：`graph.astream` 要求配
`AsyncSqliteSaver`，而它只有异步接口。

会话元数据在 `conversations.py`，两层拼成用例在 `service.py`，本模块不 import
conversations；位置是输入，客户端给什么用什么，不猜不查。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Sequence

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

load_dotenv()

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = DATA_DIR / "agent_checkpoints.sqlite"

_log = logging.getLogger("agent.agent_core")

SYSTEM_PROMPT = """你是一个 AI 助手。"""


@dataclass
class Context:
    """随每次请求注入图的运行时上下文。它不是会话状态，不进 checkpoint。"""

    user_id: str
    location: str | None = None
    thread_id: str | None = None
    memory_enabled: bool = True

    citations: list[dict] = field(default_factory=list)

    reasoning: bool = True
    web_search: bool = True

    scoped_title: str | None = None

    scope: str | None = None
    """用户「进入某本书」时由前端逐请求传入的书名（分类名）。与 scoped_title 必须分开：
    scoped_title 是单请求内模型自己选中的检索范围（每次请求重置），scope 是跨轮稳定的
    用户意图——混用会让非聚焦会话在第二轮被误判成聚焦模式。"""


@tool
def get_user_location(runtime: ToolRuntime[Context]) -> str:
    """获取用户当前所在的城市。位置由用户在客户端授权提供。"""
    context = getattr(runtime, "context", None)
    location = getattr(context, "location", None)
    if not location:
        return "用户没有提供位置信息。如果回答依赖位置，请先询问用户所在的城市。"
    return f"用户授权提供的当前位置：{location}。"


_memory_read = None  # Callable[[str], list[dict]]
_memory_write = None  # Callable[[str, list], dict[str, int]]
_memory_forget = None  # Callable[[str, str], bool]

MemoryKind = Literal["偏好", "身份", "计划", "事实"]

MAX_RECALL_ITEMS = 50


def set_memory_backend(*, read, write, forget) -> None:
    """由应用层注入长期记忆的读写实现。

    **必须在第一次用到工具之前调用**：工具列表是建 MCP 会话时一次性算好的，
    之后会被缓存。晚注入只会让工具消失，不会让它们出现。
    """
    global _memory_read, _memory_write, _memory_forget
    _memory_read, _memory_write, _memory_forget = read, write, forget


def _user_id_of(runtime: ToolRuntime[Context]) -> str:
    context = getattr(runtime, "context", None)
    return str(getattr(context, "user_id", "") or "")


def _thread_id_of(runtime: ToolRuntime[Context]) -> str | None:
    context = getattr(runtime, "context", None)
    return getattr(context, "thread_id", None)


def _memory_enabled_of(runtime: ToolRuntime[Context]) -> bool:
    """本次请求开没开记忆。**只有明确关掉才算关。**

    用 `getattr(..., True)` 而不是直接取字段：字段缺失时语义应等同「照老行为走」，不能静默
    变成「关掉记忆」——那样一个手写的 Context 会莫名其妙失去记忆功能，还不报错。
    """
    context = getattr(runtime, "context", None)
    return bool(getattr(context, "memory_enabled", True))


def _flag_of(runtime: ToolRuntime[Context], name: str) -> bool:
    """读本轮的一个开关（`reasoning` / `web_search`）。

    和上面同一个道理：**只有明确关掉才算关**。字段缺失时按「照老行为走」处理，
    也就是开着——否则一个手写的 Context 会静默失去联网能力。
    """
    return bool(getattr(getattr(runtime, "context", None), name, True))


MEMORY_OFF_FOR_REQUEST = (
    "本次对话没有开启长期记忆，请不要记录也不要查询，按普通对话回答即可。"
)


@tool
def recall_memory(runtime: ToolRuntime[Context]) -> str:
    """读出这个用户在以前会话里留下的长期记忆。

    不确定「这件事以前记过没有」、或要作废旧条目（需要它的原文）时用。
    """
    user_id = _user_id_of(runtime)
    if not user_id or _memory_read is None:
        return "长期记忆功能未启用。"
    if not _memory_enabled_of(runtime):
        return MEMORY_OFF_FOR_REQUEST
    try:
        entries = _memory_read(user_id)
    except Exception:
        return "长期记忆暂时读不出来。"
    if not entries:
        return "这个用户还没有任何长期记忆。"

    lines = [f"- {entry.get('text', '')}" for entry in entries]
    if len(lines) > MAX_RECALL_ITEMS:
        hidden = len(lines) - MAX_RECALL_ITEMS
        lines = lines[:MAX_RECALL_ITEMS] + [f"……还有 {hidden} 条更早的没有列出。"]
    return "这个用户的长期记忆：\n" + "\n".join(lines)


@tool
def save_memory(
    runtime: ToolRuntime[Context],
    text: str,
    kind: MemoryKind | None = None,
    replaces: str | None = None,
) -> str:
    """把一条关于用户的长期事实记下来，以后换个会话还能想起来。

    一件事一条，不要塞进同一个 `text`。新信息推翻旧条目时，把旧原文**逐字**放进
    `replaces`（先调 `recall_memory` 拿原文）。
    """
    user_id = _user_id_of(runtime)
    if not user_id or _memory_write is None:
        return "长期记忆功能未启用。"
    if not _memory_enabled_of(runtime):
        return MEMORY_OFF_FOR_REQUEST

    text = " ".join(str(text).split())
    if not text:
        return "没有可记的内容。"

    fact: dict[str, str] = {"text": text}
    if kind:
        fact["kind"] = kind

    try:
        replaced = bool(replaces and _memory_forget and _memory_forget(user_id, replaces))
        result = _memory_write(user_id, [fact], _thread_id_of(runtime))
    except Exception:
        return "长期记忆暂时写不进去，这次先不记了。"

    if replaces:
        note = (
            f"（作废了旧记录「{replaces}」）"
            if replaced
            else f"（注意：没找到旧记录「{replaces}」，原文可能不一致）"
        )
    else:
        note = ""

    if result.get("added"):
        return f"已记住：{text}{note}"
    if result.get("merged"):
        return f"这条已经记过了，刷新了一下：{text}{note}"
    return f"没能记下来：{text}{note}"


_retrieval_search = None  # Callable[[str, str, list[str] | None, int], dict]


def set_retrieval_backend(search) -> None:
    """由应用层注入教材检索实现。和 `set_memory_backend` 一样，必须在第一次请求之前。"""
    global _retrieval_search
    _retrieval_search = search


_textbook_catalog = None  # Callable[[str], str]：user_id -> 已格式化的书单文本，空库给空串


def set_textbook_catalog(provider) -> None:
    """由应用层注入书单提供者。agent_core 不 import documents，书单怎么查是应用层的事。"""
    global _textbook_catalog
    _textbook_catalog = provider


def _catalog_text(user_id: str, scope: str | None = None) -> str:
    if _textbook_catalog is None or not user_id:
        return ""
    try:
        return str(_textbook_catalog(user_id, scope) or "")
    except Exception:
        # 书单只是提示的一部分，拿不到就让模型走无书单的老路，不能因为它挂掉整轮。
        return ""


_SEARCH_LIMIT = 6

_TOOL_RESULT_PREVIEW_CHARS = 4000


def _figure_markdown(hit: dict, content: str) -> str:
    """图类命中在内容后补一行 markdown 图片，模型才能把图带进回答。

    chunk 里只剩题注和 `asset_id`（切分时 markdown 图片语法没跟进 chunk），不补的话图段
    召回了也渲染不出图。alt 用题注首行（要清掉 `]`、`(`、换行），文件名由前端换成签名 URL。
    """
    if str(hit.get("kind") or "") != "figure" or not hit.get("asset_id"):
        return ""
    caption = content.splitlines()[0] if content else ""
    caption = (
        caption.replace("[", "").replace("]", "")
        .replace("(", "（").replace(")", "）")
        .strip()[:60]
    )
    return f"\n![{caption or '教材插图'}]({hit['asset_id']}.png)"


def _md_alt(text: str, limit: int = 60) -> str:
    """把任意文本清洗成合法的 markdown 图片 alt（去 `]`、换括号、截断）。"""
    raw = str(text or "")
    cleaned = raw.splitlines()[0] if raw.strip() else ""
    cleaned = (
        cleaned.replace("[", "").replace("]", "")
        .replace("(", "（").replace(")", "）")
        .strip()[:limit]
    )
    return cleaned or "教材插图"


@tool
def search_textbook(
    runtime: ToolRuntime[Context],
    query: str,
    doc_title: str | None = None,
) -> str:
    """在学生上传的教材里查找相关段落，返回若干段原文和引用编号。

    query 用教材里的说法写，越具体越好（术语、公式编号、图号都可以直接写）。**同一个问题最多
    检索两次**：第一次不中可以换个说法再查一次，第二次仍不满意就用现有信息回答并说明教材里
    没找到，不要继续重试。

    doc_title 在问题指向**特定一本书**时填：点名了书名，或说到「第几章」「这本书」都算，填一部分
    就行。**整章提问也必须填**：不填就是在全部教材里搜，别本书的同名章节会混进答案、页码也对
    不上；填了但一本都没命中时，返回里会带现有书名，照着改一次再查。

    返回里的 `![...](as_xxxx.png)` 是教材插图行，与回答相关时原样写进回答（界面会显示原图）。
    """
    user_id = _user_id_of(runtime)
    if not user_id or _retrieval_search is None:
        return "教材检索功能未启用。"

    query = " ".join(str(query).split())
    if not query:
        return "查询内容为空。"

    context = getattr(runtime, "context", None)

    told = str(doc_title).strip() if doc_title else ""
    scoped = getattr(context, "scoped_title", None) if context else None
    titles = [told] if told else ([scoped] if scoped else None)

    try:
        result = _retrieval_search(user_id, query, titles, _SEARCH_LIMIT)
    except Exception:
        return "教材检索暂时不可用。"

    applied = str(result.get("scoped") or "").strip()
    if applied and context is not None and not getattr(context, "scoped_title", None):
        context.scoped_title = applied

    hits = result.get("hits") or []
    if not hits:
        return result.get("reason") or "教材里没有找到相关内容。"

    pool: list[dict] = getattr(context, "citations", None) if context else None
    assigned = {item["chunk_id"]: item["code"] for item in (pool or [])}
    lines: list[str] = []
    for hit in hits:
        chunk_id = hit.get("chunk_id") or ""
        code = assigned.get(chunk_id)
        content = str(hit.get("content") or "")
        figure_markdown = _figure_markdown(hit, content)
        nearby_note = ""
        shown_images: list[str] = []
        for figure in hit.get("nearby_figures") or []:
            asset_id = str(figure.get("asset_id") or "")
            if not asset_id or f"fig:{asset_id}" in assigned:
                continue
            assigned[f"fig:{asset_id}"] = ""
            image = f"![{_md_alt(figure.get('caption'))}]({asset_id}.png)"
            shown_images.append(image)
            if pool is not None:
                pool.append(
                    {
                        "code": "",
                        "chunk_id": f"fig:{asset_id}",
                        "doc_id": hit.get("doc_id"),
                        "doc_title": hit.get("doc_title"),
                        "chapter": hit.get("chapter"),
                        "page_start": hit.get("page_start"),
                        "page_end": hit.get("page_end"),
                        "kind": "figure",
                        "asset_id": asset_id,
                        "content": f"\n{image}",
                    }
                )
        if shown_images:
            nearby_note = (
                "\n（本节附近有插图，若与回答相关，把对应的图片行放进回答合适的位置："
                + "、".join(shown_images)
                + "）"
            )
        if code is None:
            code = f"c{len(assigned) + 1}"
            assigned[chunk_id] = code
            if pool is not None:
                pool.append(
                    {
                        "code": code,
                        "chunk_id": chunk_id,
                        "doc_id": hit.get("doc_id"),
                        "doc_title": hit.get("doc_title"),
                        "chapter": hit.get("chapter"),
                        "page_start": hit.get("page_start"),
                        "page_end": hit.get("page_end"),
                        "kind": hit.get("kind"),
                        "asset_id": hit.get("asset_id"),
                        "content": content + figure_markdown,
                    }
                )
        kind = {"figure": "图", "formula": "公式", "table": "表"}.get(
            str(hit.get("kind") or ""), ""
        )
        place = f"{hit.get('doc_title') or '教材'}"
        if hit.get("chapter"):
            place += f" · {hit['chapter']}"
        if hit.get("page_start"):
            start, end = hit.get("page_start"), hit.get("page_end") or hit.get("page_start")
            place += f" · 第 {start} 页" if start == end else f" · 第 {start}-{end} 页"
        lines.append(
            f"[{code}] {place}{' · ' + kind if kind else ''}\n{content}{figure_markdown}{nearby_note}"
        )

    body = "\n\n".join(lines)
    if not result.get("confident"):
        body += (
            "\n\n（注意：这些段落与问题的相关度不高，可能并没有直接回答这个问题。"
            "若确实答不了，请如实说教材里没有讲。)"
        )
    return body


MEMORY_GUIDE = (
    "\n\n关于长期记忆：用户提到稳定的偏好、身份、长期计划、禁忌时，用 save_memory 记一条；"
    "要作废旧条目，先 recall_memory 拿原文。一次性的需求、寒暄、猜测不记，也不用告诉用户。"
)

MEMORY_OFF_GUIDE = (
    "\n\n关于长期记忆：本次对话**没有**开启，那两个记忆工具只会返回「没有开启」。"
    "不要承诺记住、不要声称记得以前的事、也不要调用；用户要你长期记住，就说需要先登录。"
)


TEXTBOOK_GUIDE = (
    "\n\n关于教材：学生上传了教材，能用 search_textbook 查。与教材有关的问题（概念、公式、图、"
    "某章讲什么）**先查再答**，不要凭记忆。**查之前先从书单里挑最相关的一本**，把书名填进 "
    "doc_title——单本检索比全局准得多；doc_title 优先填**具体的章节文件名**，确实整本书都相关"
    "（或要对比书内多处）才填书名把范围放宽到整本；只有跨书的问题才不填、搜全部。"
    "回答只依据检索到的段落，句末标 [c1] 这样的编号、只标真用到的。"
    "确实答不了就直说教材没讲、**不要**用自己的知识补，也可以问用户要不要联网查。"
    "与课程无关的日常问题（天气、时间、闲聊）直接回答，**不要**查教材。"
)


def _system_prompt(
    *,
    memory_enabled: bool = True,
    textbook_catalog: str = "",
    focus: str | None = None,
) -> str:
    """本次请求真正发给模型的系统提示。

    没注入记忆后端时**逐字等于 `SYSTEM_PROMPT`**——不启用记忆就一个字都不改提示词。
    `memory_enabled=False`（游客）不是简单退回，而是加
    `MEMORY_OFF_GUIDE` 明说这次记不住：工具列表里还挂着记忆工具，光不提会得到一句假承诺。

    `textbook_catalog` 是书单文本。放系统提示而不是工具描述：工具描述是 MCP schema 的
    一部分、建会话时冻结，放不下动态书单；系统提示每轮重建，书单变了下一轮就能看到。

    `focus` 非空 = 用户「进入了某本书」的对话：检索已被 scoped_title 限定，提示只说明
    聚焦对象；书单由 provider 过滤成只含这一本。
    """
    prompt = SYSTEM_PROMPT
    if _memory_write is not None:
        prompt += MEMORY_GUIDE if memory_enabled else MEMORY_OFF_GUIDE
    if _retrieval_search is not None:
        if focus:
            prompt += (
                f"\n\n当前会话聚焦在《{focus}》：用户只想学这一本，检索已自动限定在它的"
                "文件范围内，search_textbook 的 doc_title **不需要填**（填了也以这个范围为准）。"
                "问题与这本书无关时正常回答，不要硬扯到书里。"
            )
            if textbook_catalog:
                prompt += f"\n\n《{focus}》包含的文件：\n{textbook_catalog}"
        else:
            prompt += TEXTBOOK_GUIDE
            if textbook_catalog:
                prompt += (
                    "\n\n现有教材（search_textbook 的 doc_title 从这里选，填书名或其中一部分）：\n"
                    f"{textbook_catalog}"
                )
    return prompt



MCP_SERVER_NAME = "agent-tools"
MCP_SERVER_SCRIPT = Path(__file__).with_name("mcp_server.py")

_TOOL_ORDER = [
    "get_weather_for_location",
    "get_user_location",
    "get_current_time",
    "web_search",
    "web_extract",
    "recall_memory",
    "save_memory",
    "search_textbook",
]


_WEB_TOOL_NAMES = frozenset({"web_search", "web_extract"})


def _order_tools(tools: list) -> list:
    """按 `_TOOL_ORDER` 重排合并后的工具。

    不在名单里的工具排到最后（`sorted` 是稳定排序，它们之间保持原有相对顺序），
    这样以后新增工具忘了登记也不会把它们弄丢，只是排在末尾。
    """
    rank = {name: index for index, name in enumerate(_TOOL_ORDER)}
    fallback = len(rank)
    return sorted(tools, key=lambda item: rank.get(getattr(item, "name", ""), fallback))


_mcp_client: MultiServerMCPClient | None = None


def _build_mcp_client() -> MultiServerMCPClient:
    """构造 MCP 客户端。**延迟到第一次用到时才构造**，不是 import 时。

    `env` 是 `dict(os.environ)` 的拷贝，import 时就构造的话，之后往环境里塞
    `TOOL_CASSETTE_MODE` 来不及——子进程拿到的还是旧环境，录制回放会静默失效。
    """
    return MultiServerMCPClient({
        MCP_SERVER_NAME: {
            "command": sys.executable,
            "args": [str(MCP_SERVER_SCRIPT)],
            "env": dict(os.environ),
            "transport": "stdio",
        }
    })


def get_mcp_client() -> MultiServerMCPClient:
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = _build_mcp_client()
    return _mcp_client


class _McpToolset:
    """在**一个专属常驻任务**里长期持有一个 MCP 会话。

    不能把会话上下文管理器挂在 `AsyncExitStack` 上跨调用复用：mcp 的 stdio 会话内部用 anyio
    的 cancel scope，它必须在「进入它的那个任务」里退出，否则报 RuntimeError: Attempted to
    exit cancel scope in a different task（单任务脚本测不出来，进了 web 服务才炸）。
    顺带保住持久连接：会话只建一次，之后每次调用都是既有管道上的一次 JSON-RPC 往返。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._stopped: asyncio.Event | None = None
        self._tools: list | None = None
        self._tools_no_web: list | None = None
        self._tool_node: ToolNode | None = None
        self._tool_node_no_web: ToolNode | None = None
        self._bound_models: dict[tuple[bool, bool, bool], object] = {}
        self._error: BaseException | None = None

    async def _serve(self) -> None:
        """常驻任务：建立会话、把工具交给外面，然后一直等到被要求关闭。"""
        try:
            async with get_mcp_client().session(MCP_SERVER_NAME) as session:
                in_process = [get_user_location]
                if _memory_write is not None:
                    in_process += [recall_memory, save_memory]
                if _retrieval_search is not None:
                    in_process += [search_textbook]
                tools = _order_tools([*await load_mcp_tools(session), *in_process])
                self._tools = tools
                self._tools_no_web = [
                    item
                    for item in tools
                    if getattr(item, "name", "") not in _WEB_TOOL_NAMES
                ]
                self._tool_node = ToolNode(tools)
                self._bound_models[(True, True, True)] = model.bind_tools(tools)
                assert self._ready is not None and self._stopped is not None
                self._ready.set()
                await self._stopped.wait()
        except BaseException as exc:  # noqa: BLE001 — 记下来，由 _ensure 抛给调用方
            self._error = exc
            if self._ready is not None:
                self._ready.set()

    async def _ensure(self) -> None:
        if self._tools is not None:
            return
        if self._ready is None:
            self._ready = asyncio.Event()
            self._stopped = asyncio.Event()
            self._task = asyncio.create_task(self._serve(), name="mcp-session")
        await self._ready.wait()
        if self._error is not None:
            raise self._error
        if self._tools is None:
            raise RuntimeError("MCP 会话未能建立。")

    async def tools(self, *, web: bool = True) -> list:
        await self._ensure()
        tools = self._tools if web else self._tools_no_web
        assert tools is not None
        return tools

    async def tool_node(self, *, web: bool = True) -> ToolNode:
        await self._ensure()
        if web:
            assert self._tool_node is not None
            return self._tool_node
        if self._tool_node_no_web is None:
            assert self._tools_no_web is not None
            self._tool_node_no_web = ToolNode(self._tools_no_web)
        return self._tool_node_no_web

    async def bound_model(
        self, *, reasoning: bool = True, web: bool = True, tools: bool = True
    ):
        """取「绑定了工具」的模型，按开关分几种。

        缓存而不是每次现建：`bind_tools` 要把每个工具的 schema 序列化进请求，开销不小，
        开关组合又是有限的。`tools=False` 返回未绑工具的裸模型，是「工具轮数用尽」的收尾路径
        （见 `MAX_TOOL_ROUNDS`）——不给它工具，它就只能用现有信息回答。这条路径不看 `web`。
        """
        await self._ensure()
        if not tools:
            key = (reasoning, False, False)
            if key not in self._bound_models:
                self._bound_models[key] = _model_for(reasoning)
            return self._bound_models[key]
        key = (reasoning, web, True)
        if key not in self._bound_models:
            self._bound_models[key] = _model_for(reasoning).bind_tools(
                await self.tools(web=web)
            )
        return self._bound_models[key]

    async def aclose(self) -> None:
        task, self._task = self._task, None
        self._tools = None
        self._tools_no_web = None
        self._tool_node = None
        self._tool_node_no_web = None
        self._bound_models = {}
        if task is None:
            self._ready = None
            self._stopped = None
            self._error = None
            return
        assert self._stopped is not None
        self._stopped.set()
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 关闭期的异常不该盖住主流程的报错
            pass
        finally:
            self._ready = None
            self._stopped = None
            self._error = None


_mcp = _McpToolset()


async def load_tools() -> list:
    """建立 MCP 会话并返回工具列表。

    评测脚本靠它读当前的工具名与 schema。
    """
    return await _mcp.tools()


async def close_mcp() -> None:
    """关掉 MCP 会话及其子进程。api.py 的 lifespan 会调用它。"""
    await _mcp.aclose()



def build_glm_model(
    temperature: float = 0.5,
    max_tokens: int = 5000,
    *,
    disable_reasoning: bool = False,
):
    api_key = os.getenv("GLM_API_KEY")
    if not api_key:
        raise RuntimeError("使用 GLM 时必须配置 GLM_API_KEY。")
    extra = (
        {"extra_body": {"thinking": {"type": "disabled"}}}
        if disable_reasoning
        else {}
    )
    return ChatOpenAI(
            model=os.getenv("GLM_MODEL", "glm-4.7-flash"),
            api_key=api_key,
            base_url="https://open.bigmodel.cn/api/paas/v4/",
            temperature=temperature,
            max_tokens=max_tokens,
            **extra,
        )


def build_model(
    temperature: float = 0.5,
    max_tokens: int = 5000,
    *,
    disable_reasoning: bool = False,
):
    """按 `MODEL_PROVIDER` 建模型。后面的参数是为了起标题能复用同一个 provider，只改几个小项。

    `disable_reasoning` 关掉思考过程：deepseek-flash 是推理模型，`reasoning_content` 也算
    output token（实测关掉后起标题从 40 token 掉到 4）。走 `extra_body` 发，因为不是 OpenAI
    标准字段；换 provider 时新家不认的话会被静默忽略，所以能不能关由 `REASONING_TOGGLEABLE`
    说出来，前端据此置灰按钮。
    """
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    if provider == "glm":
        return build_glm_model(
            temperature=temperature,
            max_tokens=max_tokens,
            disable_reasoning=disable_reasoning,
        )
    extra = {"extra_body": {"reasoning_effort": "none"}} if disable_reasoning else {}
    return init_chat_model(
        "deepseek:deepseek-flash",
        temperature=temperature,
        max_tokens=max_tokens,
        **extra,
    )


model = build_model()

title_model = build_model(temperature=0.2, max_tokens=400, disable_reasoning=True)


REASONING_TOGGLEABLE = os.getenv("MODEL_PROVIDER", "deepseek").lower() in {
    "deepseek",
    "glm",
}


_model_fast = None


def _model_for(reasoning: bool = True):
    """按「本轮要不要深度思考」取模型实例。只有两个实例、不是每轮现建：客户端无状态，
    而 `bind_tools` 的结果挂在实例上缓存（见 `_McpToolset._bound_models`），换来换去缓存就废了。
    """
    global _model_fast
    if reasoning:
        return model
    if _model_fast is None:
        _model_fast = build_model(disable_reasoning=True)
    return _model_fast



class AgentState(dict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    summary: str


CONTEXT_LIMIT_TOKENS = 200_000
CONTEXT_TRIGGER_RATIO = 0.75
CONTEXT_TRIGGER_TOKENS = int(CONTEXT_LIMIT_TOKENS * CONTEXT_TRIGGER_RATIO)
CONTEXT_TARGET_TOKENS = 10_000
MAX_KEPT_MESSAGES = 200
MAX_SUMMARY_CHARS = 800
SUMMARY_SOURCE_CHARS = 300  # 单条消息丢给摘要模型前先截断到这里

_WIDE_TOKENS = 0.6  # 汉字及中文标点
_ASCII_WORD_TOKENS = 0.3  # 英文字母、数字
_ASCII_PUNCT_TOKENS = 0.6  # ASCII 标点：结构化文本的主要成本
_ASCII_SPACE_TOKENS = 0.1  # 空格换行：多数会被并进相邻的 token
_ALNUM_RE = re.compile(r"[A-Za-z0-9_]")
_MESSAGE_OVERHEAD_TOKENS = 4
_TOOL_MESSAGE_OVERHEAD_TOKENS = 24

SUMMARY_PROMPT = (
    "你在维护一段长对话的长期记忆。下面是【已有摘要】和【一批即将被遗忘的对话记录】。\n"
    "请把它们合并成一段新的摘要，规则：\n"
    "1. 只保留以后可能还用得上的事实：用户的身份与偏好、已经确认过的结论、"
    "正在处理的事情、明确的数字与日期。\n"
    "2. 不要保留寒暄、工具调用的过程细节、以及明显已经过时的临时信息。\n"
    "3. 用陈述事实的写法，**不要用「用户」「助手」这类称呼做主语**"
    "（写「猫叫豆豆，3 岁」，不要写「用户的猫叫豆豆」）——这段摘要会直接进"
    "模型的上下文，第三人称的文风会传染到它给用户的回复里。\n"
    "4. 直接输出摘要正文，不要标题、不要 Markdown、不要解释你做了什么，"
    "更不要说明你丢弃了哪些内容。\n"
    f"5. 长度控制在 {MAX_SUMMARY_CHARS} 字以内。\n"
)

_ROLE_LABELS = {"human": "用户", "ai": "助手", "tool": "工具结果", "system": "系统"}


def _plain_text(content: object) -> str:
    """把消息内容压成纯文本。

    内容可能是字符串，也可能是 content-block 列表——MCP 工具返回的就是后者，
    不摊平的话摘要里会混进一堆 JSON 包装。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _text_only(content: object) -> str | None:
    """content-block 列表**全是文本**时摊平成字符串，否则返回 None。

    和 `_plain_text` 的区别：那个是「能摊就摊，摊不掉就算了」，这个是「摊不干净
    就别动」——发给模型的路径上不能悄悄丢掉图片之类的非文本块。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return None
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
        else:
            return None
    return "".join(parts)


def _flatten_for_model(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """把工具返回的 content-block 摊平成纯文本再发给模型。

    这不是优化，是修一个正在发生的浪费：MCP 返回 content-block 列表，langchain 的 deepseek
    适配层对非字符串 content 直接 `json.dumps`（默认 `ensure_ascii=True`），每个汉字变成
    `\u5faa` 六个字节。实测 668 字的中文 chunk 摊平 455 token、转义后 2408 token（5.3 倍）。

    数组到不了 API（那段 `json.dumps` 是无条件的，1.1.1 已是最新版、上游 master 一字不差），
    所以选摊平而不是 override 私有方法：token 一样，却少一个要跟上游版本走的补丁。

    只改发出去的内容，不动 `state`。摊不干净的（含图片等非文本块）保持原样，不丢内容。
    """
    flattened: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            text = _text_only(message.content)
            if text is not None and text is not message.content:
                message = message.model_copy(update={"content": text})
        flattened.append(message)
    return flattened


def _estimate_tokens(text: str) -> int:
    """粗估一段文本有多少 token。零成本、零依赖，误差见 `tests/probes/probe-token-estimate.py`。

    ASCII 的拆分全靠 C 层实现（`encode("ascii", "ignore")` 丢非 ASCII、`count`、
    正则替换），没有 Python 层逐字符循环——这个函数每轮要跑十几次，历史长了就是
    几十万字符，逐字符数会变成裁剪的开销本身。
    """
    if not text:
        return 0
    ascii_text = text.encode("ascii", "ignore").decode()
    wide_chars = len(text) - len(ascii_text)
    rest = _ALNUM_RE.sub("", ascii_text)
    space_chars = sum(rest.count(ch) for ch in " \n\t\r")
    punct_chars = len(rest) - space_chars
    alnum_chars = len(ascii_text) - len(rest)
    return int(
        wide_chars * _WIDE_TOKENS
        + alnum_chars * _ASCII_WORD_TOKENS
        + punct_chars * _ASCII_PUNCT_TOKENS
        + space_chars * _ASCII_SPACE_TOKENS
    )


def _message_tokens(message: BaseMessage) -> int:
    """一条消息发给模型时大概占多少 token。

    工具调用要单独算：这类消息的 `content` 往往是空串，真正的信息全在
    `tool_calls` 的 name 和 args 里。漏掉它会让「模型决定查教材」这一步几乎不占
    额度，而它实际带着整段检索用的 query。
    """
    total = _estimate_tokens(_plain_text(message.content))
    tool_calls = getattr(message, "tool_calls", None) or ()
    for call in tool_calls:
        total += _estimate_tokens(str(call.get("name") or ""))
        total += _estimate_tokens(
            json.dumps(call.get("args") or {}, ensure_ascii=False)
        )
    tool_related = bool(tool_calls) or isinstance(message, ToolMessage)
    overhead = (
        _TOOL_MESSAGE_OVERHEAD_TOKENS if tool_related else _MESSAGE_OVERHEAD_TOKENS
    )
    return total + overhead


_schema_token_cache: dict[tuple[str, ...], int] = {}


def _tool_schema_tokens() -> int:
    """工具表本身在请求里占的 token（只有这一轮真给了工具才算）。

    不写死数字：工具表随注入的后端变（记忆 / 教材），写死会静默失真。按当前工具表现算、
    按工具名缓存。会话还没建起来时返回 0——那时也没有历史要裁，为算准它去建会话反而会
    破坏「第一个请求才拉起子进程」这个前提。关掉联网时按全量算略高，几百 token 无所谓。
    """
    tools = getattr(_mcp, "_tools", None)
    if not tools:
        return 0
    key = tuple(getattr(item, "name", "") for item in tools)
    cached = _schema_token_cache.get(key)
    if cached is not None:
        return cached
    try:
        payload = [convert_to_openai_tool(item) for item in tools]
        tokens = _estimate_tokens(json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — 估不出来就当没有，别让裁剪因为估算崩掉
        _log.warning("工具 schema 估不出 token，本次按 0 计", exc_info=True)
        tokens = 0
    _schema_token_cache[key] = tokens
    return tokens


def _prompt_overhead_tokens() -> int:
    """每次请求里「不随对话增长」的 token：系统提示附加段 + 工具 schema。

    记忆引导和教材引导是发送前现拼的，不算就会系统性低估。取「两段都在」的最坏情况——
    差几百 token 对 150K 预警线没意义，但估低是坏的那一边（压得太晚）。
    """
    guide = SYSTEM_PROMPT + MEMORY_GUIDE + TEXTBOOK_GUIDE
    return _estimate_tokens(guide) + _tool_schema_tokens()


def _render_for_summary(messages: Sequence[BaseMessage]) -> str:
    lines = []
    for message in messages:
        label = _ROLE_LABELS.get(getattr(message, "type", ""), "消息")
        text = _plain_text(message.content).strip().replace("\n", " ")
        if not text:
            continue
        if len(text) > SUMMARY_SOURCE_CHARS:
            text = text[:SUMMARY_SOURCE_CHARS] + "…"
        lines.append(f"[{label}] {text}")
    return "\n".join(lines)


async def summarize_history(
    previous: str | None, dropped: Sequence[BaseMessage]
) -> str | None:
    """把「被裁掉的一批消息」并进已有摘要，返回新摘要；失败返回 None。

    这里只负责「产出摘要」这一件事——裁剪决策在 `truncate_node`，不在这里。
    """
    transcript = _render_for_summary(dropped)
    if not transcript:
        return None
    prompt = (
        f"{SUMMARY_PROMPT}\n"
        f"【已有摘要】\n{previous.strip() if previous else '（还没有摘要，这是第一次）'}\n\n"
        f"【一批即将被遗忘的对话记录】\n{transcript}"
    )
    try:
        response = await model.ainvoke(prompt)
    except Exception:
        _log.warning("生成摘要失败，被裁掉的内容将直接丢弃", exc_info=True)
        return None
    summary = _plain_text(response.content).strip()
    return summary[:MAX_SUMMARY_CHARS] or None


MAX_TOOL_ROUNDS = 6


def _tool_rounds_this_turn(messages: Sequence[BaseMessage]) -> int:
    """**本轮**已经发生过几次「模型要调工具」。

    只数最后一条 HumanMessage 之后的部分：拿整段历史比上限的话，聊到第 7 轮时历史里早就
    超过 6 条 tool_calls，于是每一轮都不给工具，功能看起来像坏了。按「带 tool_calls 的 AI
    消息条数」而不是个数——并行查三个工具仍是同一次往返。
    """
    rounds = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
            rounds += 1
    return rounds


def _messages_for_model(
    state: AgentState,
    *,
    memory_enabled: bool = True,
    answer_now: bool = False,
    user_id: str = "",
    scope: str | None = None,
) -> list[BaseMessage]:
    """发送前把摘要并进系统消息。**每轮都重建系统消息**。

    只改发出去的内容、不写回 `state`：历史展示、回退、标题生成都不受影响（靠不去改这些
    消息对象，不是靠拷贝）。`memory_enabled` 要一路透传给 `_system_prompt`，忘了就会出现
    「第一轮游客没有记忆引导，第二轮摘要生效后又冒出来」。

    为什么每轮重建而不是沿用历史里的第一条：系统提示有两个来源（首轮 stream_agent 插入、
    本函数重建），一旦内容有任何漂移，模型在第二轮看到的规则就和第一轮不一样。重建的内容
    逐字节一致时缓存照常命中，代价只是一次字符串拼接。

    `answer_now=True` 是「工具轮数用尽」的收尾指令。`scope` 是聚焦的书名，与
    stream_agent 首轮插入用的必须是同一个值。
    """
    messages = _flatten_for_model(state["messages"])
    summary = (state.get("summary") or "").strip()

    extra = ""
    if summary:
        extra += (
            "\n\n以下是本次会话更早内容的摘要，作为背景资料供你参考。"
            "把它当成你已经知道的事实即可，不要原样复述它，"
            "也不要向用户提及「摘要」这件事；"
            "回答时用你自己的口吻直接对用户说话，不要照抄摘要的措辞。\n"
            f"<摘要>\n{summary}\n</摘要>"
        )
    if answer_now:
        extra += (
            "\n\n（这一轮的工具调用次数已经用完，请立刻基于上面已有的信息给出最终回答。"
            "如果这些信息还不足以回答，就明确说明缺什么、建议用户怎么补充，"
            "不要凭记忆编造教材内容。）"
        )

    system = SystemMessage(
        content=(
            f"{_system_prompt(memory_enabled=memory_enabled, textbook_catalog=_catalog_text(user_id, scope), focus=scope)}"
            f"{extra}"
        )
    )
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = system
    else:
        messages.insert(0, system)
    return messages


async def model_node(state: AgentState, runtime: ToolRuntime[Context]):
    """模型节点。要 `runtime` 是为了拿本轮的 `memory_enabled` 和两个开关（和工具同一套注入
    机制，不会出现在模型看到的 schema 里）。

    **工具轮数的硬闸也在这里落地**：数一下本轮已调过几次工具，到顶就换一个不绑工具的模型
    并给一句「用现有信息收尾」。放节点里而不是图结构里——它只是「这次绑不绑工具」的差别。
    """
    rounds = _tool_rounds_this_turn(state["messages"])
    exhausted = rounds >= MAX_TOOL_ROUNDS
    if exhausted:
        _log.warning(
            "工具轮数用尽（%s 轮），本轮不再提供工具", rounds
        )

    bound = await _mcp.bound_model(
        reasoning=_flag_of(runtime, "reasoning"),
        web=_flag_of(runtime, "web_search"),
        tools=not exhausted,
    )
    reply = await bound.ainvoke(
        _messages_for_model(
            state,
            memory_enabled=_memory_enabled_of(runtime),
            answer_now=exhausted,
            user_id=_user_id_of(runtime),
            scope=getattr(runtime.context, "scope", None),
        )
    )

    if exhausted and getattr(reply, "tool_calls", None):
        reply = reply.model_copy(update={"tool_calls": []})
    return {"messages": [reply]}


async def tool_node(state: AgentState, runtime: ToolRuntime[Context]):
    node = await _mcp.tool_node(web=_flag_of(runtime, "web_search"))
    return await node.ainvoke(state, runtime=runtime)


async def truncate_node(state: AgentState):
    """裁掉过长的历史，并把裁掉的部分压成摘要留下来。判据是 token 估算，不是轮数。

    这个节点在图的入口、以及每次工具返回之后都会跑（truncate → model → tools → truncate）：
    入口是一条用户消息刚写进 state，**这是轮边界**，在这里压用户只觉得这一轮慢了一点；
    工具返回之后是任务中途，压掉旧轮次本身安全，但摘要模型插在两次调用之间会把连贯的任务
    拖出一次停顿，所以只有撞上硬顶才动手。

    async 是因为要 `await` 摘要模型；不需要压的时候它一次都不让出执行权。
    """
    messages = state["messages"]
    if len(messages) <= 1:
        return {}
    tail = messages[1:]  # 第 0 条是 SystemMessage，永远保留

    turn_starts = [i for i, m in enumerate(tail) if isinstance(m, HumanMessage)]
    if not turn_starts:
        return {}

    prefix = [0]
    for message in tail:
        prefix.append(prefix[-1] + _message_tokens(message))

    summary = state.get("summary") or ""
    overhead = _prompt_overhead_tokens() + _estimate_tokens(summary)
    total = overhead + prefix[-1]

    if total <= CONTEXT_TRIGGER_TOKENS:
        return {}

    last = tail[-1]
    running = isinstance(last, ToolMessage) or bool(getattr(last, "tool_calls", None))
    at_turn_boundary = not running
    over_limit = total > CONTEXT_LIMIT_TOKENS
    if not at_turn_boundary and not over_limit:
        _log.info(
            "上下文约 %s token（预警线 %s），但本轮任务未结束，等下一个轮边界再压",
            total,
            CONTEXT_TRIGGER_TOKENS,
        )
        return {}

    cut = None
    for start in turn_starts:
        kept_tokens = overhead + prefix[-1] - prefix[start]
        if kept_tokens <= CONTEXT_TARGET_TOKENS and len(tail) - start <= MAX_KEPT_MESSAGES:
            cut = start
            break
    if cut is None:
        cut = turn_starts[-1]

    if cut <= 0:
        return {}

    dropped = list(tail[:cut])
    update: dict[str, object] = {
        "messages": [RemoveMessage(id=message.id) for message in dropped]
    }
    summary_new = await summarize_history(summary or None, dropped)
    if summary_new:
        update["summary"] = summary_new

    kept_tokens = overhead + prefix[-1] - prefix[cut]
    _log.info(
        "裁剪上下文：%s 条 / 约 %s token -> %s 条 / 约 %s token"
        "（目标 %s，预警 %s，硬顶 %s，时机 %s）",
        len(tail),
        total,
        len(tail) - cut,
        kept_tokens,
        CONTEXT_TARGET_TOKENS,
        CONTEXT_TRIGGER_TOKENS,
        CONTEXT_LIMIT_TOKENS,
        "轮边界" if at_turn_boundary else "任务中途·撞硬顶",
    )
    if not summary_new:
        if _render_for_summary(dropped):
            _log.warning("摘要有内容可归纳却没拿到结果，这次裁掉的内容直接丢弃（%s 条）", len(dropped))
    return update


def should_continue(state: AgentState):
    """决定 model 之后是去调工具还是结束。

    必须容忍 messages 为空、或最后一条不是 AI 消息：回退到第一条用户消息之前时，
    整个会话会被删到只剩 SystemMessage，此时 update_state 仍会跑一遍这条条件边，
    直接取 .tool_calls 会抛 AttributeError（'SystemMessage' object has no attribute）。
    """
    messages = state["messages"]
    if not messages:
        return END
    return "tools" if getattr(messages[-1], "tool_calls", None) else END


builder = StateGraph(AgentState)
builder.add_node("model", model_node)
builder.add_node("tools", tool_node)
builder.add_node("truncate", truncate_node)
builder.set_entry_point("truncate")
builder.add_edge("truncate", "model")
builder.add_conditional_edges("model", should_continue, {"tools": "tools", END: END})
builder.add_edge("tools", "truncate")



_graph = None
_checkpointer: AsyncSqliteSaver | None = None
_graph_stack: AsyncExitStack | None = None
_graph_lock: asyncio.Lock | None = None


async def _ensure_graph():
    """首次调用时建连接、建表、编译图；之后复用同一个图。"""
    global _graph, _checkpointer, _graph_stack, _graph_lock
    if _graph is not None:
        return _graph
    if _graph_lock is None:
        _graph_lock = asyncio.Lock()
    async with _graph_lock:
        if _graph is None:
            stack = AsyncExitStack()
            try:
                checkpointer = await stack.enter_async_context(
                    AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_PATH))
                )
            except BaseException:
                await stack.aclose()
                raise
            _graph = builder.compile(checkpointer=checkpointer)
            _checkpointer = checkpointer
            _graph_stack = stack
    return _graph


async def get_graph():
    """给需要直接访问图的调用方（评测脚本、测试）使用。"""
    return await _ensure_graph()


async def shutdown() -> None:
    """关掉 MCP 会话（及其子进程）和 checkpoint 连接。api.py 的 lifespan 会调用。"""
    global _graph, _checkpointer, _graph_stack, _mcp_client
    await _mcp.aclose()
    stack, _graph_stack = _graph_stack, None
    _graph = None
    _checkpointer = None
    _mcp_client = None
    if stack is not None:
        await stack.aclose()


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


async def user_messages(thread_id: str) -> list[str]:
    """会话里所有用户消息的原文，按先后顺序。

    只暴露「读状态」：第几轮开始起标题、拿最近几条做上下文，这些业务判断
    属于应用层，由 service.py 决定。
    """
    graph = await _ensure_graph()
    state_values = (await graph.aget_state(_config(thread_id))).values
    return [
        str(message.content)
        for message in state_values.get("messages", [])
        if isinstance(message, HumanMessage)
    ]


async def delete_checkpoint_thread(thread_id: str) -> None:
    """只删 LangGraph 的 checkpoint，不碰会话元数据。"""
    await _ensure_graph()
    assert _checkpointer is not None
    await _checkpointer.adelete_thread(thread_id)


async def generate_title(context: str) -> str | None:
    """让标题模型给一段对话上下文起个短标题。失败返回 None——回退文案和长度截断由
    service.py 决定。

    没有「没配标题模型」这个分支了：`title_model` 跟着 `MODEL_PROVIDER` 一起建，真出问题
    （key 无效、限流）走 except，效果一样是退回用户原话。
    """
    try:
        response = await title_model.ainvoke(
            "请根据下面的对话主题生成一个简短的中文会话标题。"
            "只输出标题，不要引号、标点解释或 Markdown，长度不超过 20 个字。\n\n"
            f"对话中的用户问题：\n{context}"
        )
    except Exception:
        return None
    title = str(response.content).strip().replace("\n", " ").strip("`\"'“”‘’")
    return title or None


async def rewind_thread(thread_id: str, turn: int) -> dict[str, object]:
    """回退到第 turn 条用户消息发出之前（turn 从 0 开始）。该条及其之后的消息都删除，
    被删的用户消息原文返回给前端，方便放回输入框再编辑。

    删除必须从 HumanMessage 的位置切起：一个回合以它开头，这样切不会留下没有前置
    `tool_calls` 的 ToolMessage。
    """
    graph = await _ensure_graph()
    config = _config(thread_id)
    stored = list((await graph.aget_state(config)).values.get("messages", []))
    human_positions = [
        index for index, message in enumerate(stored) if isinstance(message, HumanMessage)
    ]
    if turn < 0 or turn >= len(human_positions):
        raise ValueError("要回退的消息不存在")

    cut = human_positions[turn]
    removed = stored[cut:]
    await graph.aupdate_state(
        config,
        {"messages": [RemoveMessage(id=message.id) for message in removed]},
        as_node="truncate",
    )

    return {
        "removed": len(removed),
        "text": str(removed[0].content),
        "emptied": not any(isinstance(message, HumanMessage) for message in stored[:cut]),
    }



_TOOL_RESULT_CITE_RE = re.compile(r"^\[(c\d+)\] (.+)$", re.MULTILINE)

_CITE_KIND_OF_LABEL = {"图": "figure", "公式": "formula", "表": "table"}
_CITE_PAGE_RE = re.compile(r"^第 (\d+)(?:-(\d+))? 页$")


def _citations_from_tool_text(text: str) -> list[dict]:
    """从一次 search_textbook 的返回文本解析出引用条目。

    每条命中是 `[{code}] {出处}\n{原文}`，出处形如「书名 · 章节 · 第 X 页 · 图」，章节和页码
    都可能缺。解析规则跟着 `search_textbook` 里拼 `lines.append` 的地方走，两处要一起改。
    同一编号会在后一次检索的文本里原样复用，调用方按 code 去重即可。
    """
    matches = list(_TOOL_RESULT_CITE_RE.finditer(text))
    items: list[dict] = []
    for index, match in enumerate(matches):
        code = match.group(1)
        place = match.group(2).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end():end].strip()
        content = "\n".join(
            line for line in content.splitlines()
            if not line.startswith("（本节附近有插图")
        ).strip()
        segments = [segment.strip() for segment in place.split("·") if segment.strip()]

        kind = ""
        if segments and segments[-1] in _CITE_KIND_OF_LABEL:
            kind = _CITE_KIND_OF_LABEL[segments.pop()]

        page_start = page_end = None
        for position, segment in enumerate(segments):
            page_match = _CITE_PAGE_RE.match(segment)
            if page_match:
                page_start = int(page_match.group(1))
                page_end = int(page_match.group(2) or page_match.group(1))
                segments.pop(position)
                break

        items.append(
            {
                "code": code,
                "doc_title": segments[0] if segments else None,
                "chapter": segments[1] if len(segments) > 1 else None,
                "page_start": page_start,
                "page_end": page_end,
                "kind": kind,
                "content": content,
            }
        )
    return items


async def get_history(thread_id: str) -> list[dict[str, object]]:
    graph = await _ensure_graph()
    snapshot = await graph.aget_state(_config(thread_id))
    history = []
    operations = []
    assistant_entry = None
    pending_citations: list[dict] = []
    for message in snapshot.values.get("messages", []):
        if isinstance(message, HumanMessage):
            history.append({"role": "user", "content": str(message.content)})
            assistant_entry = None
        elif isinstance(message, AIMessage):
            reasoning = (message.additional_kwargs or {}).get("reasoning_content")
            if reasoning:
                operations.append({"kind": "reasoning", "text": reasoning})
            operations.extend(
                {
                    "kind": "call",
                    "tool": call["name"],
                    "args": call.get("args", {}),
                }
                for call in message.tool_calls
            )
            if message.content:
                content = str(message.content)
                if assistant_entry is None:
                    assistant_entry = {"role": "assistant", "content": content}
                    history.append(assistant_entry)
                else:
                    assistant_entry["content"] += f"\n\n{content}"
                if operations:
                    assistant_entry.setdefault("operations", []).extend(operations)
                    operations = []
                if pending_citations:
                    assistant_entry["citations"] = pending_citations
                    pending_citations = []
        elif isinstance(message, ToolMessage):
            if getattr(message, "name", "") == "search_textbook":
                known = {item["code"] for item in pending_citations}
                for item in _citations_from_tool_text(str(message.content)):
                    if item["code"] not in known:
                        pending_citations.append(item)
                        known.add(item["code"])
            operations.append({
                "kind": "result",
                "tool": getattr(message, "name", "tool"),
                "status": getattr(message, "status", None) or "success",
                "content": str(message.content or "")[:_TOOL_RESULT_PREVIEW_CHARS],
            })

    if operations and assistant_entry is not None:
        assistant_entry.setdefault("operations", []).extend(operations)
    return history


async def stream_agent(
    user_id: str,
    thread_id: str,
    user_input: str,
    location: str | None = None,
    *,
    memory_enabled: bool = True,
    reasoning: bool = True,
    web_search: bool = True,
    scope: str | None = None,
):
    """跑一遍图，产出 Agent 自己的事件流（异步生成器）。

    `reasoning=False` 关掉深度思考（不再吐思维链），`web_search=False` 把联网工具从本轮工具表
    里摘掉。两个默认 True，**不传时行为与以前完全一致**——CLI 和评测脚本不用改。

    `scope` 是「进入某本书的对话」：非空时检索限定在该书（分类）内，系统提示换成聚焦版。
    走 `Context.scoped_title` 复用现有机制——单请求内首次检索本就会把选定书写回这个字段，
    预填等价于「用户已经替模型选好了书」，检索端、去重逻辑一行都不用改。

    只做「跑图 + 转发事件」：不碰会话元数据，也不发 title 事件（那是 service.chat_events
    的职责）。`location` 是**已经解析好的**可读位置文本，传 None 就是没有位置（定位工具会
    如实告诉模型）。`location` 和 `memory_enabled` 都走 `Context`、都不进 checkpoint——它们
    是逐请求的事实，不是会话状态。
    """
    config = _config(thread_id)
    graph = await _ensure_graph()
    has_history = bool((await graph.aget_state(config)).values.get("messages", []))
    messages = [HumanMessage(content=user_input)]
    if not has_history:
        messages.insert(
            0,
            SystemMessage(
                content=_system_prompt(
                    memory_enabled=memory_enabled,
                    textbook_catalog=_catalog_text(user_id, scope),
                    focus=scope,
                )
            ),
        )

    await _mcp.tools()

    emitted_tool_calls: set[str] = set()
    context = Context(
        user_id=user_id,
        location=location,
        thread_id=thread_id,
        memory_enabled=memory_enabled,
        reasoning=reasoning,
        web_search=web_search,
        scoped_title=scope or None,
        scope=scope or None,
    )
    async for mode, chunk in graph.astream(
        {"messages": messages},
        config=config,
        context=context,
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            message_chunk, metadata = chunk
            if isinstance(message_chunk, AIMessageChunk):
                node = metadata.get("langgraph_node")
                if node is not None and node != "model":
                    continue
                reasoning = (message_chunk.additional_kwargs or {}).get(
                    "reasoning_content"
                )
                if reasoning:
                    yield {"type": "reasoning_delta", "content": reasoning}
                if isinstance(message_chunk.content, str) and message_chunk.content:
                    yield {"type": "assistant_delta", "content": message_chunk.content}
            continue

        for _node_name, state_update in chunk.items():
            if not state_update:
                continue
            for message in state_update.get("messages", []):
                if isinstance(message, AIMessage):
                    for position, tool_call in enumerate(message.tool_calls):
                        call_key = tool_call.get("id") or f"{tool_call['name']}:{position}"
                        if call_key in emitted_tool_calls:
                            continue
                        emitted_tool_calls.add(call_key)
                        yield {
                            "type": "tool_call",
                            "tool": tool_call["name"],
                            "calls": [{
                                "name": tool_call["name"],
                                "args": tool_call.get("args", {}),
                            }],
                        }
                elif isinstance(message, ToolMessage):
                    yield {
                        "type": "tool_result",
                        "tool": getattr(message, "name", "tool"),
                        "status": getattr(message, "status", "success"),
                        "content": str(message.content or "")[:_TOOL_RESULT_PREVIEW_CHARS],
                    }
    if context.citations:
        yield {"type": "citations", "items": context.citations}
    yield {"type": "done"}
