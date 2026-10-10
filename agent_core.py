"""Agent 编排层：LangGraph 图、模型，以及读写会话「消息状态」的操作。

**工具分成两处。** 4 个「纯函数」工具（天气 / 时间 / 搜索 / 网页提取）的真实实现在
`tools.py`（那一层零 langchain 依赖），由 `mcp_server.py` 暴露成 stdio 传输的
MCP server；本模块作为 MCP client 通过 JSON-RPC 调用它们（见 `_McpToolset`）。
第 5 个工具 `get_user_location` 留在本模块，因为它是唯一需要**每次请求的
运行时上下文**的工具——原因见它自己的注释。

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
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Sequence

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

SYSTEM_PROMPT = """你是一个 AI 助手。"""


@dataclass
class Context:
    """随每次请求注入图的运行时上下文。它不是会话状态，不进 checkpoint。"""

    user_id: str
    # 用户授权提供的当前位置（已解析成可读文本，如「广东省广州市」）。
    # 为 None 表示这次请求没有位置可用——**不要退回任何猜测**（比如 IP 定位），
    # 如实告诉模型没有就是没有。
    location: str | None = None


# --------------------------------------------------------------------------
# 唯一留在进程内的工具：定位
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
]


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
        self._tool_node: ToolNode | None = None
        self._bound_model = None
        self._error: BaseException | None = None

    async def _serve(self) -> None:
        """常驻任务：建立会话、把工具交给外面，然后一直等到被要求关闭。"""
        try:
            async with get_mcp_client().session(MCP_SERVER_NAME) as session:
                # 合并两处工具：MCP 子进程里的 4 个纯函数工具 + 进程内的定位工具。
                # 合并后必须重排，否则定位工具会被追加到末尾，
                # 发给模型的工具列表顺序就和改造前不一致了。
                tools = _order_tools([*await load_mcp_tools(session), get_user_location])
                self._tools = tools
                self._tool_node = ToolNode(tools)
                self._bound_model = model.bind_tools(tools)
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

    async def tools(self) -> list:
        await self._ensure()
        assert self._tools is not None
        return self._tools

    async def tool_node(self) -> ToolNode:
        await self._ensure()
        assert self._tool_node is not None
        return self._tool_node

    async def bound_model(self):
        await self._ensure()
        return self._bound_model

    async def aclose(self) -> None:
        task, self._task = self._task, None
        self._tools = None
        self._tool_node = None
        self._bound_model = None
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

def build_glm_model(temperature: float = 0.5, max_tokens: int = 5000):
    api_key = os.getenv("GLM_API_KEY")
    if not api_key:
        raise RuntimeError("使用 GLM 时必须配置 GLM_API_KEY。")
    return ChatOpenAI(
            model=os.getenv("GLM_MODEL", "glm-4.7-flash"),
            api_key=api_key,
            base_url="https://open.bigmodel.cn/api/paas/v4/",
            temperature=temperature,
            max_tokens=max_tokens,
        )


def build_model():
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    if provider == "glm":
        return build_glm_model()
    return init_chat_model("deepseek:deepseek-flash", temperature=0.5, max_tokens=5000)


model = build_model()
title_model = build_glm_model(temperature=0.2, max_tokens=40) if os.getenv("GLM_API_KEY") else None


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
# 原来的做法是「超过 100 条就丢 1 条」，丢掉的部分永久消失——长对话里前面说过的
# 事实，到后面就查不到了。现在改成两件事：
#
#   1. **批量裁**：超限时一次裁到 `TRIM_TO`，而不是每轮丢 1 条。这是为了让下面的
#      摘要调用天然变稀疏——如果每轮裁 1 条就摘要一次，模型调用的成本会摊成常态。
#   2. **把裁掉的那批压成摘要**，和已有摘要累积合并，存进 `state["summary"]`。
#
# 摘要失败时**照常裁剪**，只是退化成原来的行为。功能不能因为摘要挂掉而失效。
#
# 已知代价：摘要调用发生在 `truncate` 节点里，而它是图的入口，所以这一次调用会
# 计进 TTFT——但频率很低（大约每十几轮一次）。真要消掉，得把摘要挪到响应之后
# 的收尾步骤，那是另一件事。
MAX_HISTORY = 100
TRIM_TO = 60
MAX_SUMMARY_CHARS = 800
SUMMARY_SOURCE_CHARS = 300  # 单条消息丢给摘要模型前先截断到这里

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
        return None
    summary = _plain_text(response.content).strip()
    return summary[:MAX_SUMMARY_CHARS] or None


def _messages_for_model(state: AgentState) -> list[BaseMessage]:
    """发送前把摘要并进系统消息。

    **只改发出去的内容，不写回 state**：`state["messages"]` 保持原样，
    历史展示、回退、标题生成都不受摘要影响。
    """
    messages = list(state["messages"])
    summary = (state.get("summary") or "").strip()
    if not summary:
        return messages
    # 措辞有讲究：光把一段事实陈述挂在系统提示后面，模型（尤其是小模型）会把它
    # 当成「该接着说的话」原样吐给用户。实测 deepseek-flash 就会这样。
    # 所以要显式说明这是背景资料、不要复述、也不要提摘要本身的存在。
    system = SystemMessage(
        content=(
            f"{SYSTEM_PROMPT}\n\n"
            "以下是本次会话更早内容的摘要，作为背景资料供你参考。"
            "把它当成你已经知道的事实即可，不要原样复述它，"
            "也不要向用户提及「摘要」这件事；"
            "回答时用你自己的口吻直接对用户说话，不要照抄摘要的措辞。\n"
            f"<摘要>\n{summary}\n</摘要>"
        )
    )
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = system
    else:
        messages.insert(0, system)
    return messages


async def model_node(state: AgentState):
    bound = await _mcp.bound_model()
    return {"messages": [await bound.ainvoke(_messages_for_model(state))]}


async def tool_node(state: AgentState, runtime: ToolRuntime[Context]):
    node = await _mcp.tool_node()
    return await node.ainvoke(state, runtime=runtime)


async def truncate_node(state: AgentState):
    """裁掉过长的历史，并把裁掉的部分压成摘要留下来。

    改成 `async` 是因为要 `await` 摘要模型——按「底层要不要 await」这条判据，
    会 await 的就必须是 async 函数。不需要裁的时候它一次都不让出执行权，
    开销和原来的同步版本没有区别。
    """
    messages = state["messages"]
    if len(messages) <= 1:
        return {}
    tail = messages[1:]  # 第 0 条是 SystemMessage，永远保留
    if len(tail) <= MAX_HISTORY:
        return {}

    start = len(tail) - TRIM_TO
    while start < len(tail) and not isinstance(tail[start], HumanMessage):
        start += 1  # 从人类消息切，避免留下没有前置 tool_calls 的 ToolMessage
    if start <= 0:
        return {}

    dropped = list(tail[:start])
    update: dict[str, object] = {
        "messages": [RemoveMessage(id=message.id) for message in dropped]
    }
    summary = await summarize_history(state.get("summary"), dropped)
    if summary:
        update["summary"] = summary
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

    没配标题模型、或调用失败时返回 None——回退文案和长度截断由调用方
    （service.py）决定，这里只负责「用模型产出标题」。
    """
    if title_model is None:
        return None
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
    await graph.aupdate_state(
        config,
        {"messages": [RemoveMessage(id=message.id) for message in removed]},
    )

    # emptied 表示回退后整个会话一条消息都不剩。要不要把标题还原成默认值属于
    # 会话元数据的事，交给 service 决定——这里只报告事实，不写元数据。
    return {
        "removed": len(removed),
        "text": str(removed[0].content),
        "emptied": not any(isinstance(message, HumanMessage) for message in stored[:cut]),
    }


async def get_history(thread_id: str) -> list[dict[str, object]]:
    graph = await _ensure_graph()
    snapshot = await graph.aget_state(_config(thread_id))
    history = []
    operations = []
    assistant_entry = None
    for message in snapshot.values.get("messages", []):
        if isinstance(message, HumanMessage):
            history.append({"role": "user", "content": str(message.content)})
            assistant_entry = None
        elif isinstance(message, AIMessage):
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
        elif isinstance(message, ToolMessage):
            operations.append({
                "kind": "result",
                "tool": getattr(message, "name", "tool"),
            })

    if operations and assistant_entry is not None:
        assistant_entry.setdefault("operations", []).extend(operations)
    return history


async def stream_agent(
    user_id: str,
    thread_id: str,
    user_input: str,
    location: str | None = None,
):
    """跑一遍图，产出 Agent 自己的事件流（异步生成器）。

    只做「跑图 + 转发事件」：不碰会话元数据，也不发 title 事件。
    按需生成并推送标题是 service.chat_events() 的职责。

    `location` 是**已经解析好的**可读位置文本（如「广东省广州市」），
    由调用方负责准备。这里既不解析坐标也不兜底猜测——传 None 就是没有位置，
    定位工具会如实告诉模型「用户没有提供位置」。位置通过 `Context` 每次请求
    注入，不写进 checkpoint（它不是会话状态，用户换个地方下一轮就该变）。
    """
    config = _config(thread_id)
    graph = await _ensure_graph()
    has_history = bool((await graph.aget_state(config)).values.get("messages", []))
    messages = [HumanMessage(content=user_input)]
    if not has_history:
        messages.insert(0, SystemMessage(content=SYSTEM_PROMPT))

    # 先把 MCP 会话准备好再开跑。否则第一个节点会现等子进程启动 + 工具加载，
    # 那段时间会被算进 TTFT，把时延指标污染掉。
    await _mcp.tools()

    # 工具调用只从 updates 通道播报一次。
    # 原先两条通道都播报，却用两套不相等的键去重：messages 通道用 tool_call_chunks
    # 的 index（整数），updates 通道用 tool_call 的 id（字符串 "call_xxx"），
    # 两者永不相等，于是同一次调用被报两遍，前端「调用工具」那行会重复显示。
    # 现在只留 updates 通道：它拿到的 AIMessage 是模型节点跑完后的完整 tool_calls
    # （参数齐全，而不是流式半截），按 id 去重也天然唯一。
    emitted_tool_calls: set[str] = set()
    async for mode, chunk in graph.astream(
        {"messages": messages},
        config=config,
        context=Context(user_id=user_id, location=location),
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            message_chunk, _metadata = chunk
            if isinstance(message_chunk, AIMessageChunk):
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
                    yield {
                        "type": "tool_result",
                        "tool": getattr(message, "name", "tool"),
                        "status": getattr(message, "status", "success"),
                    }
    yield {"type": "done"}
