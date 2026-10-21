"""Agent 编排层：LangGraph 图、模型，以及读写会话「消息状态」的操作。

**工具分成两处。** 4 个「纯函数」工具（天气 / 时间 / 搜索 / 网页提取）的真实实现在
`tools.py`（那一层零 langchain 依赖），由 `mcp_server.py` 暴露成 stdio 传输的
MCP server；本模块作为 MCP client 通过 JSON-RPC 调用它们（见 `_McpToolset`）。

另外 3 个工具留在本模块，因为它们都需要**每次请求的运行时上下文**：
`get_user_location`（读授权位置）、`recall_memory` / `save_memory`（读用户是谁、
往哪个 user_id 存）。判据只有一条：**返回值只由参数决定的工具放 MCP，需要本次
请求上下文的工具留在进程内**——展开说明见各自小节。

记忆工具的后端不是本模块实现的（不许 import 业务存储），由 `service.py` 通过
`set_memory_backend()` 注入；**没注入时这两个工具不注册**，系统提示也不变——
所以评测环境看到的工具集和提示词与基线完全一致。

因为 MCP 客户端是异步的，`stream_agent` 与 `generate_title` 都是异步的
（`graph.astream`），调用方 `service.chat_events` / `eval/run.py` 也必须是异步的。

**本模块的公开函数现在全是 async**，包括读写状态的 `get_history` /
`rewind_thread` / `delete_checkpoint_thread` / `user_messages`。这不是风格选择，
而是被迫的：`graph.astream` 要求配 `AsyncSqliteSaver`，而它只有异步接口，
同步的 `graph.get_state` / `update_state` 会直接抛 NotImplementedError。
详见下面 `_ensure_graph` 上方的说明。

会话元数据（id、标题、重命名标记）在 `conversations.py`，把两层拼成用例在
`service.py`。所以本模块不 import conversations——标题策略这类业务判断由
service 决定，这里只提供「读状态」和「跑图」。位置则是**输入**：客户端给什么
就用什么，本模块不猜、不查、不去问外部服务（解析坐标在 `geocode.py`，
由 service 调用后把成品文本传进来）。
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

# 挂在 api.py 配好的 "agent" logger 下面。这一层不自己配 handler——
# 评测脚本单独 import 本模块时不该因为「没配 handler」而丢掉告警。
_log = logging.getLogger("agent.agent_core")

SYSTEM_PROMPT = """你是一个 AI 助手。"""


@dataclass
class Context:
    """随每次请求注入图的运行时上下文。它不是会话状态，不进 checkpoint。"""

    user_id: str
    # 用户授权提供的当前位置（已解析成可读文本，如「广东省广州市」）。
    # 为 None 表示这次请求没有位置可用——**不要退回任何猜测**（比如 IP 定位），
    # 如实告诉模型没有就是没有。
    location: str | None = None
    # 本次请求属于哪个会话。记忆工具用它记下来源，方便以后查「这条是哪儿记的」。
    thread_id: str | None = None
    # 本次请求**允不允许**读写长期记忆。
    #
    # 为什么不是一个进程级的全局开关：记忆后端是进程级注入的（`set_memory_backend`，
    # 进程活多久就多久变），但「这个人能不能用记忆」是**逐请求**的——同一个进程里，
    # 登录用户要能记，共用一个默认账号的游客不能记（否则几个陌生人会互相读到
    # 对方的偏好）。两种粒度的开关必须分开，混成一个就会出现「为了挡游客，
    # 把登录用户的记忆也关了」。
    #
    # 默认 True：`agent_core` 的任何直接使用者（评测脚本、探针）拿到的都是
    # 老行为，不需要知道这个字段存在。
    memory_enabled: bool = True

    # 本次请求**已经引用过的**段落，按引用编号顺序排：`[{"code": "c1", "chunk_id": ...}]`。
    #
    # 编号（c1、c2……）是**每轮临时映射**，不落库——落库的话同一段话在不同轮次
    # 会有不同编号，历史消息里的 [c1] 就再也对不上了（真正的锚点是 chunk_id）。
    # 池子必须挂在 Context 上而不是工具函数的局部变量：一轮里模型可能查两次书，
    # 第二次的 c1 和第一次的 c1 必须是**两段不同的内容**，否则引用会串。
    #
    # 逐请求新建，所以不需要清空逻辑：`stream_agent` 每次开流都造一个新的 Context。
    citations: list[dict] = field(default_factory=list)

    # 本轮的两个开关：深度思考、联网搜索。
    #
    # **默认必须都等于 True**，而且 True 时的行为要和加这两个字段之前**逐位一致**
    # （同一套工具表、同一个模型、同一句提示词）。否则每加一个默认关闭的开关，
    # 评测基线就悄悄漂移一次，历史成绩再也对不上。
    #
    # 它们跟着 Context 走而不是做成进程级开关，理由和 `memory_enabled` 一样：
    # 这是「这一轮用户想要什么」，不是「这个进程配了什么」。同一个进程里
    # 两个用户可以一个开着联网、一个关着，互不干扰。
    #
    # 也不进 checkpoint：开关是本轮的事实，下一轮用户可能就改了。历史里已经
    # 生成过的回答不会因此变化。
    reasoning: bool = True
    web_search: bool = True

    # 本轮已经锁定的教材（书名片段，如「第3章」）。
    #
    # 为什么需要它：`search_textbook` 的 `doc_title` 是模型自己填的，实测它**不填**
    # ——问「第3章 电波传播… 这一章讲什么」，9 次检索一次都没填，于是别本书的同名
    # 章节混进答案、引用页码跑到本书页数之外。检索层现在能从 query 里认出
    # 「第3章」自动限定（`retrieval.chapter_hint`），但模型第二轮会把 query 改写成
    # 「3.6 移动信道…」，章号就没了，限定随之失效。所以把第一次认出来的书名记在
    # 这里，同一轮后续检索沿用。
    #
    # 逐请求、不落库：下一轮用户可能换一本书问，锁死就是新 bug。
    # 显式传了 `doc_title` 时以模型填的为准，不被这里覆盖。
    scoped_title: str | None = None


# --------------------------------------------------------------------------
# 留在进程内的工具（一）：定位
# --------------------------------------------------------------------------
#
# **为什么不放在 MCP server 里**：这里要说准确，否则容易得出「协议做不到」的错误结论。
# MCP 协议**确实**留了逐次调用的旁路通道：`CallToolRequest.params._meta`，
# 在 SDK 里就是 `session.call_tool(name, args, meta={...})`。所以不是协议不能。
#
# 挡住它的是本项目这一层的技术栈：`langchain-mcp-adapters` 的 `execute_tool`
# 调 `session.call_tool` 时**不传 `meta`**，而 interceptor 可以覆盖的字段只有
# `name` / `args` / `headers`——**不含 `meta`**。想走通就得绕开适配器自己写一层
# MCP 工具包装（schema、结果转换、错误处理全得自理），为一个工具维护一套私有
# 适配器不划算。
#
# 更关键的是语义问题：就算通道打通，这个工具仍然要模型**先决定去调它**，
# 才能拿到一个本来可以直接被告知的事实——多一次模型往返、多一个失败点、
# 多一次计费调用。而「用户在哪」是**客户端已知的事实**，属于编排层的输入，
# 不是需要去探测的外部世界。
#
# 早先的实现是在 MCP server 里用高德 **IP 定位**（`/v3/ip`）猜位置，另有两个问题：
#   1. 它定位的是**请求来源 IP**。服务一旦多用户部署，所有人都会被猜成
#      服务器所在地，模型会对每个用户断言「你当前在北京」——这是在编造用户事实。
#   2. 即使单机自用「碰巧对」，多花的也是一次模型往返 + 一次计费的高德调用。
#
# 所以它回到了进程内，通过 `ToolRuntime` 读调用方传进来的位置。判据可以记成一句话：
# **返回值只由参数决定的工具放 MCP，需要本次请求上下文的工具留在进程内。**
#
# 注意 docstring 就是模型看到的工具描述。这里的措辞和改造前不同，是**有意的**：
# 工具的语义真的变了（从「自己去定位」变成「读用户授权的位置」），
# 描述必须跟着改，否则模型会以为它还能探测位置。
@tool
def get_user_location(runtime: ToolRuntime[Context]) -> str:
    """获取用户当前所在的城市。位置由用户在客户端授权提供。"""
    context = getattr(runtime, "context", None)
    location = getattr(context, "location", None)
    if not location:
        # 把「怎么办」也告诉模型，否则它可能编一个城市出来。
        return "用户没有提供位置信息。如果回答依赖位置，请先询问用户所在的城市。"
    return f"用户授权提供的当前位置：{location}。"


# --------------------------------------------------------------------------
# 留在进程内的工具（二）：长期记忆的读写
# --------------------------------------------------------------------------
#
# 判据和定位工具是同一条——**需要本次请求的上下文**。这里需要的是 `user_id`：
# 模型不知道自己在跟谁说话，但这对编排层是已知的事实。
#
# **为什么是「模型自己去记」，而不是每轮偷偷抽一遍**：
# 早先的实现在每轮对话结束后，由 service 丢一个后台任务，用模型把最近十条消息
# 提炼成事实再存。它能用，但有两个毛病：
#   1. **成本不可控**——不管这一轮有没有值得记的东西，都要多付一次模型调用；
#   2. **只能追加**——去重只能挡住字面重复，解不了「用户改主意了」，
#      旧的「不吃辣」永远作废不掉。
# 改成工具之后两个毛病一起消失：值不值得记由模型当场判断，而 `replaces`
# 让它能直接作废旧条目。
#
# **代价要认**：触发权交给模型，就存在「它忘了记」的风险。所以工具描述和系统
# 提示里的 `MEMORY_GUIDE` 都要把「什么时候该记」写清楚——这笔账记在提示词
# 质量上，不是白拿的。
#
# **后端是注入进来的**：本模块有一条硬规矩——不许 import 业务存储
# （`conversations` / `memories`）。所以这里只声明「往哪读、往哪写」，
# 具体实现由 `service.py` 通过 `set_memory_backend()` 传进来。
# 依赖方向不变：service → agent_core，反向没有 import。
#
# **没注入后端时这两个工具根本不注册**（见 `_McpToolset._serve`）：
# 一个「调用必然返回不可用」的空壳工具只会白占模型的选择面。评测脚本只 import
# `agent_core`，拿到的仍然是和基线一模一样的 5 个工具。
_memory_read = None  # Callable[[str], list[dict]]
_memory_write = None  # Callable[[str, list], dict[str, int]]
_memory_forget = None  # Callable[[str, str], bool]

# `kind` 的取值。**必须和 `memories.KINDS` 逐字一致**——本模块不许 import 业务存储，
# 所以只能在这里重抄一遍，改一边就得改另一边。
#
# 写成 `Literal` 不是装饰：模型看到的是 JSON Schema，`Literal` 会变成带 `enum`
# 的字段，它就只能从这几个里挑。踩过的坑：早先是自由字符串，模型自己造了
# 「禁忌」「背景」两个新词，全被存储层归到「其他」，kind 这个字段等于白设。
MemoryKind = Literal["偏好", "身份", "计划", "事实"]

# `recall_memory` 一次最多列多少条。存储层上限是 200，全倒进上下文会很难看。
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

    用 `getattr(..., True)` 而不是直接取字段：字段缺失时语义应当等同「照老行为走」，
    不能静默变成「关掉记忆」——那样一个手写的 Context 会莫名其妙失去记忆功能，
    而且不报错，排查起来很费劲。
    """
    context = getattr(runtime, "context", None)
    return bool(getattr(context, "memory_enabled", True))


def _flag_of(runtime: ToolRuntime[Context], name: str) -> bool:
    """读本轮的一个开关（`reasoning` / `web_search`）。

    和上面同一个道理：**只有明确关掉才算关**。字段缺失时按「照老行为走」处理，
    也就是开着——否则一个手写的 Context 会静默失去联网能力。
    """
    return bool(getattr(getattr(runtime, "context", None), name, True))


# 逐请求关闭记忆时回给模型的话。和「后端没注入」分开，是因为两者要对模型说的
# 话不一样：前者是这次不给用，后者是这个功能压根不存在。
MEMORY_OFF_FOR_REQUEST = (
    "本次对话没有开启长期记忆，请不要记录也不要查询，按普通对话回答即可。"
)


@tool
def recall_memory(runtime: ToolRuntime[Context]) -> str:
    """读出这个用户在以前会话里留下的长期记忆。

    在想确认「这件事以前记过没有」时用——尤其是要修改或作废某条旧记录之前，
    先看一眼原文，才能把它准确地放进 `save_memory` 的 `replaces`。
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

    # **只列原文，不要附带任何标注**。踩过的坑：早先这里写成
    # `- {text}（{kind}）`，模型就把整行「不吃香菜。（其他）」当成原文抄进
    # `replaces`，于是永远匹配不上旧条目。这段文本只有一个用途——
    # 让模型能逐字引用回去，那就必须干净。
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

    该记：稳定的偏好、身份背景（职业、城市、专业）、长期计划、明确的禁忌（过敏、忌口）。
    不该记：一次性的提问和要求、寒暄、你自己的猜测——不确定就别记。

    一件事一条，不要塞进同一个 `text`。

    新信息推翻旧条目时，把旧原文**逐字**放进 `replaces`（先调 `recall_memory` 拿原文）。
    记完不用告诉用户，也别提这个工具。
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

    # **读写失败必须在这里兜住**，不能让异常冒到图上去。
    # 这是真踩过的：探针跑真实对话时存储层抛了 PermissionError，异常从 ToolNode
    # 直接炸穿，整轮对话挂掉——辅助功能不该有这个权力。和 `generate_title`
    # 拿不到标题就退回用户原话是同一个态度。
    try:
        # 先删后写。顺序反过来会把刚写的那条一起删掉——新旧条目归一化之后可能
        # 相同（用户只是把说法补充了一下）。
        replaced = bool(replaces and _memory_forget and _memory_forget(user_id, replaces))
        result = _memory_write(user_id, [fact], _thread_id_of(runtime))
    except Exception:
        return "长期记忆暂时写不进去，这次先不记了。"

    if replaces:
        # 没匹配上要明说。静默失败会让模型以为已经作废了，而旧条目还在。
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


# --------------------------------------------------------------------------
# 留在进程内的工具（三）：教材检索
# --------------------------------------------------------------------------
#
# 判据和上面两个相同——**需要本次请求的上下文**，这里是 `user_id`。
# 而且这条判据比记忆那边更硬：检索必须**按用户隔离**（F2：各人只看自己的书）。
# 如果把检索放进 MCP 子进程，`user_id` 就得从参数一路传下去，而参数是模型填的
# ——模型永远不该有机会指定「查谁的书」。
#
# 后端同样是注入的：`agent_core` 不许 import `retrieval`（依赖方向 service →
# agent_core），所以这里只声明「查一下」，具体怎么打分由 `retrieval.py` 决定。
_retrieval_search = None  # Callable[[str, str, list[str] | None, int], dict]


def set_retrieval_backend(search) -> None:
    """由应用层注入教材检索实现。和 `set_memory_backend` 一样，必须在第一次请求之前。"""
    global _retrieval_search
    _retrieval_search = search


# 一次检索最多给几条。给多了（10+）会挤掉用户自己的问题，而且排序第 6 名之后
# 基本是噪声；给少了图/公式这类短段落挤不进来。
_SEARCH_LIMIT = 6

# 工具返回内容给前端展示时的截断上限。全文可能上万字（教材检索带 6 段原文），
# 折叠块里塞太多既拖慢渲染也看不完；看不全的本来就是「原文在这」，不是「原文没了」。
_TOOL_RESULT_PREVIEW_CHARS = 4000


def _figure_markdown(hit: dict, content: str) -> str:
    """图类命中在内容后补一行 markdown 图片，模型才能把图带进回答。

    **为什么必须有这一行**：整书 markdown 里的 `![题注](as_xx.png)` 语法在切分
    时没有跟进 chunk——chunk 里只剩题注文字和 `asset_id` 元数据（实测库里 0 条
    带图语法）。不补的话，图段就算被召回，模型看到的也只是「图 9.2 …」一行字，
    回答和引用面板里永远渲染不出图。

    alt 用题注首行（markdown alt 里不能有 `]`、`(`、换行，都要清洗或替换），
    回答/引用原文里的文件名由前端换成带签名的图片 URL。
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
    """在学生上传的教材里查找相关段落。回答任何与教材内容有关的问题前，先调用它。

    query 用教材里的说法写，越具体越好（术语、公式编号、图号都可以直接写）。
    一次查不到就换个说法再查一次（同义改写、只留关键词），不要查到一次就放弃。

    doc_title 在问题指向**特定一本书**时填：用户点名了书名，或者说到「第几章」
    「这本书」「这套课件」，都算。填书名的一部分就行（「第3章」「移动通信」），
    不用打全，也不用管书名里的空格、加号和日期后缀。

    **整章提问也必须填**。不填就是在全部教材里搜，别本书的同名章节会把内容混进
    答案，页码也会对不上（问第 3 章却引用到第 5 章的第 160 页）。填了但一本都没
    命中时，返回里会带现有书名，照着改一次再查。

    返回若干段落，每段带一个引用编号 [c1]、书名、章节、页码。**回答只能依据这些
    段落**，并在句末标出引用的编号；书里没讲就直说没讲，不要用你自己的知识补充。
    返回内容里的 `![...](as_xxxx.png)` 是教材插图的图片行（正文段落后的
    「本节附近有插图」标注里也有）：与回答相关时，把对应的图片行原样写进回答的
    合适位置，界面就会显示书上的原图；与回答无关就不要带。
    """
    user_id = _user_id_of(runtime)
    if not user_id or _retrieval_search is None:
        return "教材检索功能未启用。"

    query = " ".join(str(query).split())
    if not query:
        return "查询内容为空。"

    context = getattr(runtime, "context", None)

    # 模型自己填的书名优先；它没填时沿用本轮已经锁定那本（`Context.scoped_title`）。
    # 实测模型**不会**主动填 doc_title（问「第3章…这一章讲什么」，9 次检索一次
    # 都没填），所以这道沿用不是优化，是让「整章提问不许跨书」这条真正成立。
    told = str(doc_title).strip() if doc_title else ""
    scoped = getattr(context, "scoped_title", None) if context else None
    titles = [told] if told else ([scoped] if scoped else None)

    try:
        result = _retrieval_search(user_id, query, titles, _SEARCH_LIMIT)
    except Exception:
        # 和记忆工具同一条规矩：辅助功能出错不许把整轮对话炸掉。
        return "教材检索暂时不可用。"

    # 记下这次生效的限定（可能是检索层从「第3章」自动认出来的）。
    applied = str(result.get("scoped") or "").strip()
    if applied and context is not None and not getattr(context, "scoped_title", None):
        context.scoped_title = applied

    hits = result.get("hits") or []
    if not hits:
        return result.get("reason") or "教材里没有找到相关内容。"

    pool: list[dict] = getattr(context, "citations", None) if context else None
    # 同一个 chunk 在一轮里被查两次时**复用同一个编号**：否则模型会同时看到
    # c3 和 c7 是同一段话，回答里可能两处都引，前端就重复显示同一张图。
    assigned = {item["chunk_id"]: item["code"] for item in (pool or [])}
    lines: list[str] = []
    for hit in hits:
        chunk_id = hit.get("chunk_id") or ""
        code = assigned.get(chunk_id)
        content = str(hit.get("content") or "")
        figure_markdown = _figure_markdown(hit, content)
        # 邻近插图：命中的正文段同页可能有讲它的图（图段自己几乎排不进候选池，
        # 挂在正文段上才见得到模型）。给模型可见的标记；图同时进引用池
        # （不占编号、按 asset 去重），前端才有签名 URL 可换——不然正文里
        # 的图片行永远渲染不出来。
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
                # `content` 是给前端的：点开一条引用要能看到书上原文。带图类
                # 补的图片行，前端小窗才能直接出图（和工具文本同一份内容）。
                # 模型那边已经拿到了这段话（就在工具返回里），所以这里带上它
                # 不泄露任何模型不该知道的东西，只是把同一份事实交给界面。
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
        # 弱命中要说出来，但**不删结果**：书里可能只在某处提过一句，模型可以
        # 据此回答「只提到过……」，比直接说「没有」有用。
        body += (
            "\n\n（注意：这些段落与问题的相关度不高，可能并没有直接回答这个问题。"
            "若确实答不了，请如实说教材里没有讲。)"
        )
    return body


# 记忆功能的系统提示补充。**只在注入了后端时才拼上去**——理由见 `_system_prompt`。
MEMORY_GUIDE = (
    "\n\n关于长期记忆：你有 `save_memory` 工具，当用户告诉你值得长期记住的信息"
    "（偏好、身份、长期计划、禁忌）时主动记下来。不确定以前记过没有，可以先"
    "`recall_memory` 看一眼；要作废某条旧记录时必须先看，才能拿到它的原文。"
    "不要为了记而记：一次性的需求、寒暄、你自己的猜测都不用记。"
    "记完不用告诉用户，照常回答即可。"
)

# 逐请求**关掉**记忆时的提示。关记忆为什么要主动说明，而不是「不提就行」——
# 这是实测出来的：
#
# 工具列表是**进程级冻结**的（建 MCP 会话时算好，一进程一份），所以游客这一侧
# 照样看得见 `save_memory` 的说明书（docstring 就是工具描述）。只把系统提示退回
# `SYSTEM_PROMPT` 的版本上线后，游客问「用一句话说明你是什么」，模型回答的是
# 「我是一个……能记住你长期偏好以备后续对话使用的 AI 助手」——它读的是工具说明书。
# 等用户真说了值得记的事、工具回一句「没开启」，模型才反应过来，可承诺已经出口了。
#
# 所以关掉记忆必须**明说**。这段话和 `MEMORY_OFF_FOR_REQUEST`（回给工具调用的那句）
# 是一对：一句劝住模型别调，一句告诉模型这是怎么回事。
MEMORY_OFF_GUIDE = (
    "\n\n关于长期记忆：本次对话**没有**开启长期记忆功能。"
    "即便你在工具列表里看得到 `save_memory` / `recall_memory`，调用也只会返回"
    "「没有开启」。所以不要向用户承诺「我会记住」，也不要声称记得他以前说过的偏好，"
    "不要主动调用这两个工具。用户若希望你长期记住某件事，就如实告诉他需要先登录。"
)


# 教材问答的系统提示补充。同样**只在注入了检索后端时才拼上去**。
#
# 这段话每一句都对应一个验收点，不是泛泛的「请基于资料回答」：
#   - 「先查再答」→ B1（答案基于这本书），也防住模型用训练知识抢答；
#   - 「换说法再查」→ 这一层是 BM25，不懂同义改写，只能靠模型自己改写（见 retrieval.py）；
#   - 「书里没讲就直说」→ B4，而且**不许**接着用外部知识补一个答案；
#   - 「只标真的用了的」→ C6（不硬凑引用）。
TEXTBOOK_GUIDE = (
    "\n\n关于教材：学生上传了教材，你能用 `search_textbook` 查里面的内容。"
    "凡是要回答与教材有关的问题（概念、公式、图、某一章讲了什么），**先查再答**，"
    "不要凭记忆回答教材里的内容。"
    "一次查不到就换个说法再查一次（换同义词、只留关键词、拆成两问），最多两三次。"
    "回答时依据检索到的段落，并在相应句末标出引用编号，如 [c1]；只标你真正用到的段落，"
    "没用到的不标。"
    "如果检索结果确实没有回答这个问题，就明确说教材里没有讲，"
    "**不要**用自己的知识补一段看起来像答案的内容；可以问用户是否需要联网查或按常识回答。"
    "教材里出现图或公式时，检索结果会标明，你可以直接说「书上的图 3-4」。"
)


def _system_prompt(*, memory_enabled: bool = True) -> str:
    """本次请求真正发给模型的系统提示。

    没有注入记忆后端时**逐字等于 `SYSTEM_PROMPT`**。这样评测环境（只 import
    `agent_core`）拿到的提示词与基线完全一致——新功能不进评测，A/B 才干净。
    和工具列表要不要挂上记忆工具，是同一条规则的两半。

    `memory_enabled=False`（游客）时**不是**简单退回 `SYSTEM_PROMPT`，而是加上
    `MEMORY_OFF_GUIDE` 明确说「这次记不住」。理由见那段常量的注释：工具列表里
    还挂着记忆工具，光不提它会得到一句「我能记住你的偏好」的假承诺。

    教材那段同理：`_retrieval_search` 没注入（评测脚本/CLI）时**一个字都不加**，
    否则「你现在有教材」这句话会被写进每一份基线提示词里。
    """
    prompt = SYSTEM_PROMPT
    if _memory_write is not None:
        prompt += MEMORY_GUIDE if memory_enabled else MEMORY_OFF_GUIDE
    if _retrieval_search is not None:
        prompt += TEXTBOOK_GUIDE
    return prompt


# --------------------------------------------------------------------------
# MCP：另外 4 个工具跑在独立进程里
# --------------------------------------------------------------------------

MCP_SERVER_NAME = "agent-tools"
MCP_SERVER_SCRIPT = Path(__file__).with_name("mcp_server.py")

# 发给模型的**工具顺序**，必须与改造前 `agent_core.tools` 的顺序逐位一致——
# 有些模型对工具顺序敏感，顺序变了 A/B 就不干净。
# 定位工具排在天气之后、时间之前，所以它不能简单追加到 MCP 工具列表末尾，
# 得靠这张表重新排序。它是**合并两处工具后的唯一顺序来源**。
_TOOL_ORDER = [
    "get_weather_for_location",
    "get_user_location",
    "get_current_time",
    "web_search",
    "web_extract",
    # 记忆工具**刻意排到最后**：前面 5 个的位置要和改造前逐位一致，
    # 追加在末尾是唯一不打扰它们的位置。
    "recall_memory",
    "save_memory",
    # 教材检索排在最后：它只在注入了检索后端（有人传了书）时才存在，
    # 排在记忆之后可以让「没有教材的用户」看到的工具表和以前逐位一致。
    "search_textbook",
]


# 关掉「联网搜索」时要从工具表里摘掉的名字。
#
# 两个必须一起摘：`web_extract` 是「打开搜索结果里的某个链接」，留着它而摘掉
# `web_search`，模型就没有合法途径拿到 URL，这个工具等于废着占位。
# 反过来只摘 `web_extract` 更糟——模型能搜但不能深读，遇到长网页只能靠摘要瞎答。
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

    原因：`env` 是 `dict(os.environ)` 的一份**拷贝**。如果在 import 时构造，
    评测进程之后再往 os.environ 里塞 `TOOL_CASSETTE_MODE` 就来不及了——
    子进程拿到的还是旧环境，录制回放会静默失效。延迟构造让「先设环境变量、
    再启动会话」这个顺序成立。
    """
    return MultiServerMCPClient({
        MCP_SERVER_NAME: {
            # 用当前解释器拉起子进程，保证子进程和主进程在同一个 conda 环境里
            # （mcp / tavily 都装在那儿）。
            "command": sys.executable,
            "args": [str(MCP_SERVER_SCRIPT)],
            # 必须显式把父进程的整个环境传过去。mcp SDK 在 env=None 时会退回到一份
            # 精简的默认环境变量（只有 PATH 之类），AMAP_API_KEY / TAVILY_API_KEY
            # 不会带过去，结果是所有工具都静默变成「服务未配置」。
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

    为什么不能把会话的上下文管理器挂在 AsyncExitStack 上跨调用复用：
    mcp 的 stdio 会话内部用 anyio 的 cancel scope，而 anyio 要求 cancel scope
    必须在「进入它的那个任务」里退出。如果会话是在第一个请求的任务里建立的、
    之后在 FastAPI lifespan 的任务里关闭，就会报

        RuntimeError: Attempted to exit cancel scope in a different task
        than it was entered in

    （这个坑很隐蔽：单任务脚本里跑得好好的，只有进了 web 服务才炸。）
    把这套生命周期整体放进一个常驻任务后，进入与退出天然在同一个任务里。

    顺带这也保住了持久连接：会话只建一次，之后每次工具调用都是既有管道上的
    一次 JSON-RPC 往返，而不是重新拉起一个 Python 子进程——否则测到的就是
    解释器启动开销而不是 MCP 协议开销，和改造前的时延对比会失真。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._stopped: asyncio.Event | None = None
        self._tools: list | None = None
        # 关掉联网那一套：**从工具表里摘掉**，不是留着让模型自觉不用。
        # 模型只要看到 web_search 的描述就会想去调它，「请你在这一轮不要用联网」
        # 这种提示词约束对小模型根本挡不住。摘掉是唯一可靠的方式，
        # 顺带工具表还短了两项。
        self._tools_no_web: list | None = None
        self._tool_node: ToolNode | None = None
        self._tool_node_no_web: ToolNode | None = None
        # 按 (要不要思考, 要不要联网, 绑不绑工具) 缓存 bind 结果。
        # 每一套都只在**第一次真的有人用**时才建。
        #
        # 第三维是 2026-10-22 加的：工具调用轮数用尽后要换一个**不绑工具**的模型，
        # 逼它基于已有信息收尾（见 `MAX_TOOL_ROUNDS`）。
        self._bound_models: dict[tuple[bool, bool, bool], object] = {}
        self._error: BaseException | None = None

    async def _serve(self) -> None:
        """常驻任务：建立会话、把工具交给外面，然后一直等到被要求关闭。"""
        try:
            async with get_mcp_client().session(MCP_SERVER_NAME) as session:
                # 合并两处工具：MCP 子进程里的 4 个纯函数工具 + 进程内的工具。
                # 合并后必须重排，否则定位工具会被追加到末尾，
                # 发给模型的工具列表顺序就和改造前不一致了。
                #
                # 记忆工具**只在注入了后端时才挂上**。没注入就等于是两个空壳，
                # 挂着只会白占模型的选择面——评测环境因此拿到和基线完全一样的工具集。
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
                # 默认那套在会话建立时就 bind 好：它是绝大多数请求走的路径，
                # 留到第一个请求里现建就会把 MCP 启动之外的又一段耗时算进 TTFT。
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
        # 这一段没有 await，在单线程事件循环里是原子的，所以不需要额外的锁：
        # 并发首次调用不会各自起一个常驻任务。
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
        # 下面这段中间没有 await，单线程事件循环里是原子的，
        # 并发的首批请求不会各自建一个 ToolNode。
        if self._tool_node_no_web is None:
            assert self._tools_no_web is not None
            self._tool_node_no_web = ToolNode(self._tools_no_web)
        return self._tool_node_no_web

    async def bound_model(
        self, *, reasoning: bool = True, web: bool = True, tools: bool = True
    ):
        """取「绑定了工具」的模型，按开关分几种。

        缓存而不是每次现建：`bind_tools` 是有成本的（要把每个工具的 schema
        序列化进请求），开关组合又是有限的。

        `tools=False` 返回**未绑工具**的裸模型。这是「本轮工具轮数用尽」的
        收尾路径（见 `MAX_TOOL_ROUNDS`）——不给它工具，它就只能用现有信息回答。
        这条路径不看 `web`：没有工具表就没有联网工具，谈联网没有意义。
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
            # 允许关闭之后再重新建立（测试里会这么用）
            self._ready = None
            self._stopped = None
            self._error = None


_mcp = _McpToolset()


async def load_tools() -> list:
    """建立 MCP 会话并返回工具列表。

    评测脚本用它读取工具名与 schema，好跟改造前逐字对比。
    """
    return await _mcp.tools()


async def close_mcp() -> None:
    """关掉 MCP 会话及其子进程。api.py 的 lifespan 会调用它。"""
    await _mcp.aclose()


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------

def build_glm_model(
    temperature: float = 0.5,
    max_tokens: int = 5000,
    *,
    disable_reasoning: bool = False,
):
    api_key = os.getenv("GLM_API_KEY")
    if not api_key:
        raise RuntimeError("使用 GLM 时必须配置 GLM_API_KEY。")
    # 智谱关思考用的是 `thinking` 字段，不是 deepseek 的 `reasoning_effort`。
    # 字段名不统一，所以两家各写一份，不能指望一个参数通吃。
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
    """按 `MODEL_PROVIDER` 建模型。

    后面的参数都是为了起标题能**复用同一个 provider**、只改这几个小项，
    而不是再写一份 provider 分支。

    `disable_reasoning` 关掉模型的思考过程：deepseek-flash 是**推理模型**，
    回答前会先吐一段 `reasoning_content`，那部分也算 output token。起标题这种
    任务不需要思考，开着纯属浪费（实测关掉后输出从 40 token 掉到 4）。
    走 `extra_body` 发这个参数是因为它不是 OpenAI 标准字段。

    两家现在都支持了（智谱走 `thinking` 字段）。**但这件事必须留一处显式声明**：
    换 provider 时如果新家不认这个参数，它会被静默忽略——模型照旧思考，
    前端那个「深度思考」按钮就成了摆设，用户点了没反应也不知道为什么。
    所以能不能关由 `REASONING_TOGGLEABLE` 说出来，前端据此决定按钮能不能点。
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

# 起标题用**和主模型同一个 provider**，只是温度调低、token 卡紧、并且关掉推理——
# 标题要的是稳定和短，不是创意，更不是思考过程。
#
# 这里原来硬编码成 GLM（`build_glm_model(...) if GLM_API_KEY else None`），后果是：
# `.env` 里只要还留着 GLM_API_KEY（留着做备用很正常），**标题就一直在走智谱**，
# 和 `MODEL_PROVIDER` 说的不是一回事；而且 GLM 的 key 一撤，标题功能就静默消失
# （退回「取用户原话前 30 字」），不报错也不提示。
#
# `max_tokens=400` 是**安全垫**，不是预期用量：关掉推理之后实测只花 4 个 token。
# 定这么宽是因为万一日后换了模型、`reasoning_effort` 不被认（参数被静默忽略），
# 有 400 的余地标题还能出来；真卡到 40 的话推理会把额度吃光、content 变成空串，
# 表现就是「标题莫名其妙没了」。上限宽着不花钱——`max_tokens` 是天花板不是目标，
# 计费只按实际生成算。
title_model = build_model(temperature=0.2, max_tokens=400, disable_reasoning=True)


# 「深度思考」这个开关当前这个 provider 到底认不认。
#
# 显式列出来而不是默认 True，是为了让「不认」这件事**看得见**：
# 换到一个不支持关推理的 provider 时，宁可前端把按钮置灰，也不要让用户
# 点了一个没效果的按钮（这正是标题功能踩过的坑——GLM key 一撤，标题静默消失）。
REASONING_TOGGLEABLE = os.getenv("MODEL_PROVIDER", "deepseek").lower() in {
    "deepseek",
    "glm",
}


_model_fast = None


def _model_for(reasoning: bool = True):
    """按「本轮要不要深度思考」取模型实例。

    只有两个实例、不是每轮现建：模型客户端本身无状态，建一次就够；
    而 `bind_tools` 的结果要挂在实例上缓存（见 `_McpToolset._bound_models`），
    实例换来换去那些缓存就全废了。
    """
    global _model_fast
    if reasoning:
        return model
    if _model_fast is None:
        _model_fast = build_model(disable_reasoning=True)
    return _model_fast


# --------------------------------------------------------------------------
# 图
# --------------------------------------------------------------------------

class AgentState(dict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    # 更早对话的压缩摘要。**不走消息通道**，所以不参与 add_messages、不进历史展示；
    # 只在发送给模型前并进系统消息（见 `_messages_for_model`）。
    summary: str


# --------------------------------------------------------------------------
# 上下文窗口策略
# --------------------------------------------------------------------------
#
# 判据是 **token 估算**，不是轮数，也不是条数。
#
# 为什么不用轮数：一轮的体量差得太远。纯聊天一轮（问一句、答一句）不到 200 token；
# 教材问答一轮带 3 次检索、每次 6 段 chunk，能到 6000 token——**同样算「一轮」，
# 差 30 倍**。按轮裁的结果是两头不讨好：前者才攒了几千 token 就把历史丢了，
# 后者留 20 轮就是 12 万 token。
#
# 为什么不用条数：同一个毛病，工具调用会让「一轮等于几条」剧烈变化。
#
# token 数是怎么来的：**估的，不额外调 API**。DeepSeek 官方文档给的换算是
# 「1 个汉字 ≈ 0.6 token，1 个 ASCII 字符 ≈ 0.3 token」，`_estimate_tokens`
# 在这之上按字符类别细分了一下。要精确就得上 tokenizer（装 transformers，或者随包
# 附一份词表），为「要不要裁剪」这种软阈值不值得。实测偏差
# （`tests/probes/probe-token-estimate.py`，对着真实 `usage.input_tokens` 对）：
#
#     中文正文          -5%    英文散文            +48%（偏高，方向安全）
#     一轮完整问答      -8%    公式密集 markdown   -26%
#     32 条真实历史     -8%    ← 这条才是有意义的那个数
#
# 也就是说：单条内容能偏到两三成，但**整段会话累计下来在 10% 以内**，因为
# 不同形状的误差互相抵消。软阈值需要的正是这个精度。
#
# 三个数，各管一件事：
#   LIMIT   —— 不能超的线（200K，1M 窗口的五分之一，留足余量）
#   TRIGGER —— 「接近了」的判定（LIMIT 的 75%），到了它就在**下一个轮边界**压
#   TARGET  —— 压到这里为止（10K）
#
# **为什么不等到 200K 才压**：超了再压是在事故之后救火，而压缩要调一次模型、
# 会占掉那一轮的首字节。所以判定提前到 75%，但**动作落在轮边界上**——
# 一条用户消息刚进来的那一刻。那时候压，用户看到的是「这一轮回答稍微慢了半秒」，
# 而不是「任务跑到一半卡住了」。
#
# **为什么中途不压**（除了硬顶兜底）：`truncate` 在每次工具返回后也会跑，
# 那时任务还没做完。此刻丢掉旧轮次在技术上安全（永远不丢当前轮），但代价是
# 摘要模型插在两次模型调用之间，把一个连贯的「查 → 想 → 再查」拖出一次停顿。
# 宁可让它把这一轮跑完，下一轮开始时再收拾——一轮最多再加十几 K（6 轮工具、
# 每次结果按 4000 字预览算），150K + 十几 K 离 200K 还有余量，来得及。
#
# 迟滞（TRIGGER 与 TARGET 之间的 140K）也是必需的：一超限就裁到刚好达标，
# 下一轮又超，等于每轮都多调一次摘要模型。140K 折合教材问答二十来轮，
# 压缩是「隔很久发生一次的停顿」，不是常态。
#
# 60K → 200K 是 2026-10-22 按用户要求调的。代价要说清楚：TARGET 10K 意味着
# 压缩后只剩一两轮原文，其余全靠那 800 字摘要，所以**摘要的质量直接等于
# 「它记不记得之前聊过什么」**。要更保守就把 TARGET 调大，别动 TRIGGER。
CONTEXT_LIMIT_TOKENS = 200_000
CONTEXT_TRIGGER_RATIO = 0.75
CONTEXT_TRIGGER_TOKENS = int(CONTEXT_LIMIT_TOKENS * CONTEXT_TRIGGER_RATIO)
CONTEXT_TARGET_TOKENS = 10_000
# 条数硬顶，兜「估算本身失准」的情况：token 估算按字符数算，对纯文本准，
# 对内容里塞了图片/表格的 content block 会严重低估（一张图几百 token，
# 字符数却可能只有几十）。正常文本会话是撞不到这条线的。
MAX_KEPT_MESSAGES = 200
MAX_SUMMARY_CHARS = 800
SUMMARY_SOURCE_CHARS = 300  # 单条消息丢给摘要模型前先截断到这里

# DeepSeek 文档给的换算比例。**只是比例**：真实分词按词切，误差见探针输出。
# 方向刻意取保守——估高只是早裁一点（少留两轮），估低会撑爆窗口。
#
# 分四类而不是两类（汉字 / 非汉字）是被实测逼出来的：JSON、markdown、LaTeX 里
# 的标点（`{` `}` `"` `:` `$` `\` `_`）几乎一个字符就吃一个 token，按「非汉字
# 一律 0.3」算会把工具返回的结果低估到实际的三分之一。反过来英文散文是
# 四五个字符才一个 token，用 0.3 已经偏高（保守方向，认了）。
_WIDE_TOKENS = 0.6  # 汉字及中文标点
_ASCII_WORD_TOKENS = 0.3  # 英文字母、数字
_ASCII_PUNCT_TOKENS = 0.6  # ASCII 标点：结构化文本的主要成本
_ASCII_SPACE_TOKENS = 0.1  # 空格换行：多数会被并进相邻的 token
_ALNUM_RE = re.compile(r"[A-Za-z0-9_]")
# 每条消息在对话模板里还要加角色标签和分隔符，实测 3~4 个。
_MESSAGE_OVERHEAD_TOKENS = 4
# 带工具调用的那两条（AI 的 tool_calls、ToolMessage）模板开销大得多：
# DeepSeek 要给函数名、调用编号、返回值各包一层标记，实测每轮多出 40~50 个。
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

    **这不是优化，是修一个正在发生的浪费。** MCP 工具返回的是 content-block 列表
    （`[{"type": "text", "text": "..."}]`）。langchain 的 deepseek 适配层在把
    ToolMessage 转成请求体时，对非字符串 content 直接 `json.dumps`——**带默认的
    `ensure_ascii=True`**，于是每个汉字变成 `\\u5faa` 六个字节：

        "content": "[{\\"type\\": \\"text\\", \\"text\\": \\"\\u5faa\\u73af\\u524d...\\"}]"

    实测代价（668 字的中文 chunk，`tests/probes/probe-token-estimate.py`）：
    摊平后 455 token，转义后 **2408 token（5.3 倍）**。教材问答一轮要返回 3~6 段
    这样的 chunk，等于每轮白烧上万 token，而且模型是在 `\\u5faa\\u73af` 这种噪声里
    自己还原中文——既贵又难读。摊平成普通字符串之后两件事一起没了。

    只改**发出去的内容**，不动 `state`：历史展示、回退、标题生成看到的还是原样。
    摊不干净的（有图片等非文本块）保持原样，宁可不省也不能丢内容。
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
    # 非 ASCII 字符数 = 总长 − 纯 ASCII 长度：encode 把非 ASCII 整个丢掉，剩下的就是 ASCII。
    ascii_text = text.encode("ascii", "ignore").decode()
    wide_chars = len(text) - len(ascii_text)
    # 去掉字母数字，剩下的就是空白 + 标点，再从里面把空白摘出来。
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

    工具 schema 每个请求都要发一遍，9 个工具加起来是几千 token，不能不算。
    但**不写死数字**：工具表随注入的后端变（记忆 / 教材），写死会随工具增减静默
    失真。这里直接按当前工具表现算，并按工具名缓存——同一套工具只算一次。

    会话还没建起来时返回 0。那时候本来也没有历史要裁，估不准不影响任何决策；
    硬要为了算准它去建 MCP 会话，反而把「第一个请求才拉起子进程」这个前提破坏了。

    关掉联网时实际发的是 `_tools_no_web`（少两个工具），这里按全量算会略高一点。
    几百 token 的量级，对 150K 的预警线没有意义，不值得为此维护两份缓存。
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
    """每次请求里「不随对话增长」的那部分 token：系统提示附加段 + 工具 schema。

    `state` 里存的第 0 条 SystemMessage 只有 `SYSTEM_PROMPT` 一句话，记忆引导和
    教材引导都是 `_messages_for_model` 在发送前现拼的，不在这里算就会系统性低估。

    取「两段引导都在」的最坏情况：实际差几百 token，对 150K 的预警线没有意义，
    但漏算的方向是坏的那一边（估低 → 压得太晚）。
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
        # 摘要失败**照常裁剪**，只是退化成「直接丢弃」。功能不能因为摘要挂掉而失效，
        # 但必须留下痕迹——否则「它怎么忘了」会被当成模型变笨。
        _log.warning("生成摘要失败，被裁掉的内容将直接丢弃", exc_info=True)
        return None
    summary = _plain_text(response.content).strip()
    return summary[:MAX_SUMMARY_CHARS] or None


# 一轮对话里最多允许几次「模型决定调工具 → 工具返回 → 再问模型」的往返。
#
# 为什么需要这个硬闸：提示词里写的是「一次查不到就换个说法再查一次，最多两三次」，
# 但那只是**建议**。`should_continue` 的唯一判据是「最后一条消息有没有 tool_calls」，
# 模型只要一直吐 tool_calls 就能一直调下去——某次抽风连查十几次，用户干等一分钟，
# 配额和账单一起烧，而且没有任何一层会拦住它。
#
# 6 的依据：正常教材问答 1 次检索就够（检索层已经做了融合 + rerank），
# 「换个说法再查」留 2-3 次余量，6 宽到不该误伤正常路径。
#
# 超限后的处理是「**不给工具**」而不是报错：用户至少能拿到一个基于已有检索结果的
# 答案，比一句「出错了」有用。这也是 `bound_model(tools=False)` 存在的唯一原因。
MAX_TOOL_ROUNDS = 6


def _tool_rounds_this_turn(messages: Sequence[BaseMessage]) -> int:
    """**本轮**已经发生过几次「模型要调工具」。

    只数最后一条 HumanMessage 之后的部分。拿整段历史的总量去比上限会出大事：
    聊到第 7 轮时历史里早就超过 6 条 tool_calls，于是**每一轮都不给工具**，
    功能看起来像坏了。

    按「带 tool_calls 的 AI 消息条数」而不是「tool_calls 的个数」：一次并行查
    三个工具，仍然是同一个决策、一次往返，不该被算成三次。
    """
    rounds = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
            rounds += 1
    return rounds


def _messages_for_model(
    state: AgentState, *, memory_enabled: bool = True, answer_now: bool = False
) -> list[BaseMessage]:
    """发送前把摘要并进系统消息。

    **只改发出去的内容，不写回 state**：`state["messages"]` 保持原样，
    历史展示、回退、标题生成都不受摘要影响。

    `memory_enabled` 一路透传给 `_system_prompt`：这条路径也会重建系统消息，
    忘了透传就会出现「第一轮游客没有记忆引导，第二轮摘要生效后记忆引导又冒出来」。

    `answer_now=True` 是「工具轮数用尽」的收尾指令（见 `MAX_TOOL_ROUNDS`）。
    它**必须重建系统消息**——包括没有摘要的时候，所以不能走下面那条
    「没有摘要就原样返回」的快路径。

    每次都会先过一遍 `_flatten_for_model`（工具结果摊平，理由见那个函数），
    所以返回的一定是新列表。但没被摊平的消息对象仍与 `state` 共享，
    「不写回 state」靠的是**不去改这些对象**，不是靠拷贝。
    """
    messages = _flatten_for_model(state["messages"])
    summary = (state.get("summary") or "").strip()
    if not summary and not answer_now:
        return messages

    extra = ""
    if summary:
        # 措辞有讲究：光把一段事实陈述挂在系统提示后面，模型（尤其是小模型）会把它
        # 当成「该接着说的话」原样吐给用户。实测 deepseek-flash 就会这样。
        # 所以要显式说明这是背景资料、不要复述、也不要提摘要本身的存在。
        extra += (
            "\n\n以下是本次会话更早内容的摘要，作为背景资料供你参考。"
            "把它当成你已经知道的事实即可，不要原样复述它，"
            "也不要向用户提及「摘要」这件事；"
            "回答时用你自己的口吻直接对用户说话，不要照抄摘要的措辞。\n"
            f"<摘要>\n{summary}\n</摘要>"
        )
    if answer_now:
        # 不说「你不能再调工具」——这一轮本来就没给工具表，说了它也不懂为什么。
        # 要说的只有「该收尾了，以及信息不够时怎么办」，后者比前者的价值更大：
        # 没有这句，模型倾向于硬编一个看起来完整的答案。
        extra += (
            "\n\n（这一轮的工具调用次数已经用完，请立刻基于上面已有的信息给出最终回答。"
            "如果这些信息还不足以回答，就明确说明缺什么、建议用户怎么补充，"
            "不要凭记忆编造教材内容。）"
        )

    system = SystemMessage(
        content=f"{_system_prompt(memory_enabled=memory_enabled)}{extra}"
    )
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = system
    else:
        messages.insert(0, system)
    return messages


async def model_node(state: AgentState, runtime: ToolRuntime[Context]):
    """模型节点。

    这里要 `runtime` 是为了拿本轮的 `memory_enabled` 和两个开关。
    节点函数声明 `ToolRuntime[Context]` 参数和工具是同一套注入机制，不会出现在
    模型看到的 schema 里。

    **工具轮数的硬闸也在这里落地**（见 `MAX_TOOL_ROUNDS`）：数一下本轮已经调过
    几次工具，到顶就换一个不绑工具的模型，并给一句「用现有信息收尾」的指令。
    放在节点里而不是图结构里，是因为它只是「这一次绑不绑工具」的差别，
    不该为它单独编一张图。
    """
    rounds = _tool_rounds_this_turn(state["messages"])
    exhausted = rounds >= MAX_TOOL_ROUNDS
    if exhausted:
        # 记一条：出现它说明模型在打转（或者问题确实需要反复检索）。
        # 用户只会看到「回答短了/说信息不足」，不知道背后发生了什么。
        _log.warning(
            "工具轮数用尽（%s 轮），本轮不再提供工具", rounds
        )

    # 两个开关也在这一层落地：图是编译好复用的，换不了；
    # 而「绑定了哪些工具、用哪个模型」是每次进节点现取的，所以逐轮的差别
    # 在这里生效就够了，不需要为每个开关各编译一张图。
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
        )
    )

    if exhausted and getattr(reply, "tool_calls", None):
        # 没绑工具时模型理论上不会吐 tool_calls，但小模型凭上下文里的历史学着吐
        # 是真实存在的。留着它，`should_continue` 就会把这一轮送去 tools 节点，
        # 硬闸等于没闸——而且那个工具还不在这一轮的绑定表里。
        reply = reply.model_copy(update={"tool_calls": []})
    return {"messages": [reply]}


async def tool_node(state: AgentState, runtime: ToolRuntime[Context]):
    node = await _mcp.tool_node(web=_flag_of(runtime, "web_search"))
    return await node.ainvoke(state, runtime=runtime)


async def truncate_node(state: AgentState):
    """裁掉过长的历史，并把裁掉的部分压成摘要留下来。

    判据是 **token 估算**，不是轮数——理由见上面「上下文窗口策略」那段注释。

    这个节点在图的**入口**、以及**每次工具返回之后**都会跑
    （`truncate -> model -> tools -> truncate`）。两个位置的含义不一样：

    - 入口 = 一条用户消息刚写进 state，**这是一轮的边界**。在这里压，用户只会
      觉得这一轮慢了一点；
    - 工具返回之后 = 任务跑到一半。压掉旧轮次本身安全（永远不动当前轮），
      但会让摘要模型插在两次调用之间，把连贯的任务拖出一次停顿。所以这里
      只有撞上硬顶才动手，其余情况留给下一个轮边界。

    改成 `async` 是因为要 `await` 摘要模型——按「底层要不要 await」这条判据，
    会 await 的就必须是 async 函数。不需要压的时候它一次都不让出执行权。
    """
    messages = state["messages"]
    if len(messages) <= 1:
        return {}
    tail = messages[1:]  # 第 0 条是 SystemMessage，永远保留

    # 切点只能落在 HumanMessage 上：一轮不能被劈成两半，否则会留下没有前置
    # tool_calls 的 ToolMessage，OpenAI 协议会直接拒绝这种请求。
    turn_starts = [i for i, m in enumerate(tail) if isinstance(m, HumanMessage)]
    if not turn_starts:
        return {}

    # 累加和先算好。候选切点有几十个，每个都从切点往后重新累加虽然也是 O(n)，
    # 但 n 是几十万字符，能省就省。
    prefix = [0]
    for message in tail:
        prefix.append(prefix[-1] + _message_tokens(message))

    summary = state.get("summary") or ""
    overhead = _prompt_overhead_tokens() + _estimate_tokens(summary)
    total = overhead + prefix[-1]

    # 没到预警线就什么都不做。少了这一步，一裁完刚好达标、下一轮又超，
    # 等于每轮都要多花一次摘要模型调用（那次调用还算进首字节）。
    if total <= CONTEXT_TRIGGER_TOKENS:
        return {}

    # 到了预警线，但要挑时机。判据是「这一轮还在不在跑」：
    # 最后一条是工具结果、或者是一条还在等工具的 AI 消息 → 任务正跑着；
    # 其余情况（一条用户消息刚进来，或者上一轮已经答完）→ 轮边界，安全。
    # 用状态本身判断而不是「第几次被调用」，调用顺序将来变了也不会判错。
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

    # 从最早的一轮开始丢，取**第一个**落进目标线以内的切点——也就是「丢得最少」
    # 的那种丢法。剩下越来越小是单调的，所以遇到第一个满足的就是最优解。
    cut = None
    for start in turn_starts:
        kept_tokens = overhead + prefix[-1] - prefix[start]
        if kept_tokens <= CONTEXT_TARGET_TOKENS and len(tail) - start <= MAX_KEPT_MESSAGES:
            cut = start
            break
    if cut is None:
        # 连「只留当前这一轮」都超目标（单轮塞得太多，或者目标定得太小）。
        # 没有更细的切法了——一轮不能从中间劈开——就保当前这一轮，认了。
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
        # 摘要失败**照常裁剪**，只是退化成「直接丢弃」。功能不能因为摘要挂掉而失效。
        # 注意别把「这批消息本来就没有可归纳的文字」也算成失败——那种情况
        # `summarize_history` 返回 None 是正常的，不该刷告警。
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


# --------------------------------------------------------------------------
# 图与 checkpoint：懒加载的异步资源
# --------------------------------------------------------------------------
#
# **为什么不能像改造前那样在模块级建连接**：AsyncSqliteSaver.from_conn_string
# 是一个异步上下文管理器，import 时没有 running event loop，拿不到连接；
# 而且连接的建立必须发生在将来真正跑图的那个事件循环里——aiosqlite 每条连接
# 背后一个线程，跨事件循环复用会出问题。
#
# **为什么上面的状态操作函数全都变成了 async**：LangGraph 的同步 checkpointer
# 和异步 checkpointer 是硬绑定的。图要用 astream 跑（异步 MCP 工具要求），
# 就必须配 AsyncSqliteSaver；而它只提供异步接口（aget_tuple / aput），
# 于是 graph.get_state / update_state 也不能再同步调用——改造前那套同步写法
# 会直接抛 NotImplementedError。

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
    # 客户端也一并作废，下次要会话时会按当前环境变量重新构造
    # （评测靠这个特性在同一次进程里换 cassette 模式）。
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
    """让标题模型给一段对话上下文起个短标题。

    调用失败时返回 None——回退文案和长度截断由调用方（service.py）决定，
    这里只负责「用模型产出标题」。

    没有「没配标题模型」这个分支了：`title_model` 现在跟着 `MODEL_PROVIDER` 一起
    在模块加载时建出来，不再依赖 `GLM_API_KEY` 是否设置。真出问题（key 无效、
    限流）会走下面那条 `except`，效果一样是退回用户原话。
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
    """回退到第 turn 条用户消息发出之前。

    turn 是当前会话里用户消息的序号，从 0 开始。该条用户消息以及它之后的所有消息
    都会被删除，被删除的那条用户消息原文会返回给前端，方便放回输入框再编辑。

    删除必须从 HumanMessage 的位置切起：一个回合以 HumanMessage 开头，
    这样切不会留下没有前置 tool_calls 的 ToolMessage。
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
    # **必须显式 `as_node="truncate"`**：图里 `truncate` 和 `model` 都会写
    # `messages` 通道，不指定来源时 LangGraph 判不出这次更新算谁的，直接抛
    # `InvalidUpdateError: Ambiguous update, specify as_node`。
    # 选 `truncate` 而不是 `model`：它是入口节点、每轮都写 messages，
    # 语义上就是「整理消息的那个环节」，回退删消息属于同一件事。
    # （2026-10-22 修：以前只有 model 写 messages，不指定也能推断出来。）
    await graph.aupdate_state(
        config,
        {"messages": [RemoveMessage(id=message.id) for message in removed]},
        as_node="truncate",
    )

    # emptied 表示回退后整个会话一条消息都不剩。要不要把标题还原成默认值属于
    # 会话元数据的事，交给 service 决定——这里只报告事实，不写元数据。
    return {
        "removed": len(removed),
        "text": str(removed[0].content),
        "emptied": not any(isinstance(message, HumanMessage) for message in stored[:cut]),
    }


# --------------------------------------------------------------------------
# 历史消息
# --------------------------------------------------------------------------

# search_textbook 的工具返回文本里，每条命中都以 `[cN] 出处` 开头、原文跟在
# 换行后面。历史重建（C5：刷新页面引用不丢）就靠把它解析回结构化条目——
# 引用编号不落库，这是唯一还留着「cN ↔ 哪一段」对应关系的地方。
_TOOL_RESULT_CITE_RE = re.compile(r"^\[(c\d+)\] (.+)$", re.MULTILINE)

# 工具文本里 kind 用中文显示（图/公式/表），重建时换回事件里的英文原值，
# 前端拿到两种来源（实时事件 / 历史重建）的结构就完全一致。
_CITE_KIND_OF_LABEL = {"图": "figure", "公式": "formula", "表": "table"}
_CITE_PAGE_RE = re.compile(r"^第 (\d+)(?:-(\d+))? 页$")


def _citations_from_tool_text(text: str) -> list[dict]:
    """从一次 search_textbook 的返回文本解析出引用条目。

    每条命中是 `[{code}] {出处}\n{原文}`，出处形如「书名 · 章节 · 第 X 页 · 图」，
    各段用 ` · ` 连接、章节和页码都可能缺。解析规则跟着拼接处的格式走
    （`search_textbook` 里的 `lines.append`），两处必须一起改。

    同一轮模型可能查两次书，前一次的编号会在后一次的文本里原样再出现
    （复用同一编号），调用方按 code 去重即可。
    """
    matches = list(_TOOL_RESULT_CITE_RE.finditer(text))
    items: list[dict] = []
    for index, match in enumerate(matches):
        code = match.group(1)
        place = match.group(2).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end():end].strip()
        # 「本节附近有插图」是给模型看的检索标注，不是书上的原文，
        # 历史引用原文里不该出现（图片行本身保留，前端要靠它出图）。
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
    # 当前这条助手消息之前累积的引用条目。一轮里「工具返回 → 助手作答」，
    # 工具返回里解析出的引用挂到下一条有正文的助手消息上，挂完清空。
    pending_citations: list[dict] = []
    for message in snapshot.values.get("messages", []):
        if isinstance(message, HumanMessage):
            history.append({"role": "user", "content": str(message.content)})
            assistant_entry = None
        elif isinstance(message, AIMessage):
            # 思维链**不用另外存**：它一直躺在 additional_kwargs 里跟着 checkpoint
            # 走，而且存的是完整一段——分片合并时同名 key 是字符串**相加**而不是
            # 覆盖（实测逐片之和与聚合结果逐字相等）。这里顺手读出来塞进 operations，
            # 重开页面时「思考过程」就能补回去。
            #
            # 顺序有意放在工具调用之前：一轮里模型永远是「先想，再决定调什么」。
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
                # 这一轮查过书 → 引用挂在这条回答上。没有就不写字段，
                # 和实时事件「没引用不发事件」保持同一个约定。
                if pending_citations:
                    assistant_entry["citations"] = pending_citations
                    pending_citations = []
        elif isinstance(message, ToolMessage):
            # `status` 是 langchain 的标准字段（success / error）。以前这里把它丢了，
            # 后果和前端丢掉流事件里的 status 是同一个：**刷新页面之后「工具失败」就
            # 再也看不出来**，历史里只剩一行「工具完成：xxx」——那是在骗人。
            # 用 `or "success"` 兜住 None：老 checkpoint 里的消息可能没有这个字段。
            # 顺手把教材检索的返回解析成引用条目（C5：刷新后引用面板还在）。
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
                # 工具返回原文（截断），和实时流的 tool_result 事件同一份内容，
                # 刷新后「工具完成」那行才展开得出来。
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
):
    """跑一遍图，产出 Agent 自己的事件流（异步生成器）。

    `reasoning=False` 关掉深度思考（模型不再吐思维链，直接给答案），
    `web_search=False` 把联网工具从本轮的工具表里摘掉。两个默认 True，
    也就是**不加这两个参数时行为和以前完全一致**——CLI 和评测脚本不用改。

    只做「跑图 + 转发事件」：不碰会话元数据，也不发 title 事件。
    按需生成并推送标题是 service.chat_events() 的职责。

    `location` 是**已经解析好的**可读位置文本（如「广东省广州市」），
    由调用方负责准备。这里既不解析坐标也不兜底猜测——传 None 就是没有位置，
    定位工具会如实告诉模型「用户没有提供位置」。位置通过 `Context` 每次请求
    注入，不写进 checkpoint（它不是会话状态，用户换个地方下一轮就该变）。

    `memory_enabled=False` 表示这次请求不许读写长期记忆（游客）。它同样走
    `Context`，同样**不进 checkpoint**——「这次是谁在说话」是逐请求的事实，
    不是会话的状态。注意系统提示只在会话第一轮写进 state，所以中途登出/登录
    不会改掉已经写下的提示；真正拦人的是两个记忆工具里的那道检查。
    """
    config = _config(thread_id)
    graph = await _ensure_graph()
    has_history = bool((await graph.aget_state(config)).values.get("messages", []))
    messages = [HumanMessage(content=user_input)]
    if not has_history:
        messages.insert(0, SystemMessage(content=_system_prompt(memory_enabled=memory_enabled)))

    # 先把 MCP 会话准备好再开跑。否则第一个节点会现等子进程启动 + 工具加载，
    # 那段时间会被算进 TTFT，把时延指标污染掉。
    await _mcp.tools()

    # 两条通道各管一件事，职责不重叠：
    #
    #   messages 通道 → 逐字输出（正文 + 思维链）。它是「模型正在写」的实时视图，
    #                   所以正文和思维链都从这里走，前端才能边生成边显示。
    #   updates  通道 → 结构化事实（调了哪个工具、工具返回了什么）。它只有在
    #                   节点**跑完**之后才给，拿到的工具参数是完整的。
    #
    # 工具调用只从 updates 通道播报一次。原先两条通道都播报，却用两套不相等的键
    # 去重：messages 通道用 tool_call_chunks 的 index（整数），updates 通道用
    # tool_call 的 id（字符串 "call_xxx"），两者永不相等，于是同一次调用被报两遍，
    # 前端「调用工具」那行会重复显示。现在只留 updates：按 id 去重天然唯一。
    emitted_tool_calls: set[str] = set()
    # Context 提到外面来：教材检索工具会往 `context.citations` 里追加编号，
    # 流结束时要把它交出去（前端据此把 [c1] 换成可点开的原文/图片）。
    context = Context(
        user_id=user_id,
        location=location,
        thread_id=thread_id,
        memory_enabled=memory_enabled,
        reasoning=reasoning,
        web_search=web_search,
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
                # 思维链和正文**都要**按节点过滤，判据只算一次。
                #
                # 这条通道捕获的是「这个图里所有被调用的模型」，不只是主模型。
                # `truncate` 是入口节点、每轮都跑，它在需要裁剪时会 `await
                # model.ainvoke(...)` 生成摘要——那一次调用的 token 同样会从这条
                # 通道冒出来（实测拍到的分片：truncate 347 片 / model 65 片）。
                # 摘要既不是「助手在回答」，更不是「助手在想」，直接放行会让用户
                # 在思考过程里看到一段与本次提问无关的摘要草稿，正文那条更糟：
                # 摘要会被当成回答，平白出现在真正的答案前面（实测混进 80 字）。
                # 判断写成「明确是别的节点才跳过」，而不是「必须是 model 才播」：
                # `langgraph_node` 这个键万一某天不给了，后者会让回答**静默变空**，
                # 前者只是退回加过滤之前的行为（照播）。宁可偶尔漏过滤，不能整段丢回答。
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
                        # id 是模型为这次调用生成的唯一标识。万一某个 provider 没给，
                        # 退回「名字 + 在这批里的位置」；同一批里的两次同名调用
                        # 位置不同，仍能区分开。
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
                    # status 是 langchain 的标准字段（success / error）。带上它有两个用处：
                    # 前端能区分「工具跑失败」和「工具正常返回」；评测脚本靠它算工具调用成功率。
                    # content 是工具返回的原文（截断）：前端「调用工具」那行只有名字，
                    # 用户看不到工具实际拿回了什么——展示出来才能看懂模型为什么这么答。
                    yield {
                        "type": "tool_result",
                        "tool": getattr(message, "name", "tool"),
                        "status": getattr(message, "status", "success"),
                        "content": str(message.content or "")[:_TOOL_RESULT_PREVIEW_CHARS],
                    }
    # 引用编号 → 段落。**必须在 done 之前发**：前端收到 done 就会收尾，
    # 晚到的事件会被丢掉。没有引用时不发，避免前端多一个空状态。
    if context.citations:
        yield {"type": "citations", "items": context.citations}
    yield {"type": "done"}
