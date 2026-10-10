"""Agent 编排层：LangGraph 图、工具、模型，以及读写会话「消息状态」的操作。

这里只管 Agent 本身。会话元数据（id、标题、重命名标记）在 `conversations.py`，
把两层拼成用例在 `service.py`。所以本模块不 import conversations——
标题策略这类业务判断由 service 决定，这里只提供「读状态」和「跑图」。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from dotenv import load_dotenv
from tavily import TavilyClient
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
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

load_dotenv()

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_PATH = DATA_DIR / "agent_checkpoints.sqlite"

SYSTEM_PROMPT = """你是一个 AI 助手。"""


@dataclass
class Context:
    user_id: str


@tool
def get_weather_for_location(
    city: str,
    mode: Literal["current", "forecast"],
) -> str:
    """查询中国城市天气。mode=current 查询实时天气，mode=forecast 查询未来几天预报；city 可以是城市名称或高德 adcode。"""
    api_key = os.getenv("AMAP_API_KEY")
    if not api_key:
        return "天气服务未配置 AMAP_API_KEY。"
    if mode not in {"current", "forecast"}:
        return "天气查询模式必须是 current 或 forecast。"

    try:
        city_code = city.strip()
        if not city_code.isdigit():
            params = urlencode({"address": city_code, "key": api_key})
            with urlopen(f"https://restapi.amap.com/v3/geocode/geo?{params}", timeout=10) as response:
                geocode_data = json.load(response)
            geocodes = geocode_data.get("geocodes", [])
            if geocode_data.get("status") != "1" or not geocodes:
                return f"找不到城市“{city_code}”的高德地图编码。"
            city_code = geocodes[0].get("adcode", "")

        params = urlencode({
            "city": city_code,
            "key": api_key,
            "extensions": "base" if mode == "current" else "all",
        })
        with urlopen(f"https://restapi.amap.com/v3/weather/weatherInfo?{params}", timeout=10) as response:
            weather_data = json.load(response)
        if weather_data.get("status") != "1":
            return f"高德天气查询失败：{weather_data.get('info', '未知错误')}"

        if mode == "current":
            lives = weather_data.get("lives", [])
            if not lives:
                return "高德天气没有返回实时数据。"
            current = lives[0]
            return (
                f"{current.get('province', '')}{current.get('city', '')}当前天气："
                f"{current.get('weather', '未知')}，气温 {current.get('temperature', '未知')}°C，"
                f"湿度 {current.get('humidity', '未知')}%，风向 {current.get('winddirection', '未知')}，"
                f"风力 {current.get('windpower', '未知')}级，更新时间：{current.get('reporttime', '未知')}。"
            )

        forecasts = weather_data.get("forecasts", [])
        if not forecasts or not forecasts[0].get("casts"):
            return "高德天气没有返回预报数据。"
        forecast = forecasts[0]
        lines = [
            f"{cast.get('date', '未知日期')}：白天{cast.get('dayweather', '未知')}，"
            f"夜间{cast.get('nightweather', '未知')}，{cast.get('nighttemp', '未知')}~"
            f"{cast.get('daytemp', '未知')}°C，{cast.get('daywind', '未知')}风"
            f"{cast.get('daypower', '未知')}级"
            for cast in forecast["casts"]
        ]
        return f"{forecast.get('province', '')}{forecast.get('city', '')}天气预报：" + "；".join(lines)
    except (HTTPError, URLError, TimeoutError):
        return "无法连接高德天气服务。"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return "高德天气数据格式异常。"


@tool
def get_user_location(runtime: ToolRuntime[Context]) -> str:
    """通过高德 IP 定位服务获取当前用户所在的省、市和 adcode。"""
    api_key = os.getenv("AMAP_API_KEY")
    if not api_key:
        return "定位服务未配置 AMAP_API_KEY。"
    try:
        params = urlencode({"key": api_key})
        with urlopen(f"https://restapi.amap.com/v3/ip?{params}", timeout=10) as response:
            data = json.load(response)
        if data.get("status") != "1":
            return f"高德定位失败：{data.get('info', '未知错误')}"
        province = data.get("province", "")
        city = data.get("city", "")
        if not province and not city:
            return "高德定位成功，但没有返回省市信息。"
        return f"当前位置：{province}{city}，adcode：{data.get('adcode', '')}"
    except (HTTPError, URLError, TimeoutError):
        return "无法连接高德定位服务。"
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return "高德定位数据格式异常。"


@tool
def get_current_time() -> str:
    """获取当前时间。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@tool
def web_search(query: str, topic: Literal["general", "news"] = "general") -> str:
    """使用 Tavily 搜索互联网。general 用于普通搜索，news 用于新闻和近期事件。"""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "搜索服务未配置 TAVILY_API_KEY。"
    try:
        response = TavilyClient(api_key=api_key).search(
            query=query,
            topic=topic,
            search_depth="basic",
            max_results=5,
            include_answer=False,
        )
        results = response.get("results", [])
        if not results:
            return f"没有找到与“{query}”相关的搜索结果。"
        return json.dumps([
            {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "content": result.get("content", ""),
                "score": result.get("score"),
            }
            for result in results
        ], ensure_ascii=False)
    except Exception:
        return "Tavily 搜索暂时失败。"


@tool
def web_extract(url: str) -> str:
    """打开并读取一个网页 URL 的正文内容，适合深入阅读搜索结果。"""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "网页读取服务未配置 TAVILY_API_KEY。"
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return "网页地址无效，请提供完整的 http 或 https URL。"
    try:
        response = TavilyClient(api_key=api_key).extract(urls=[url])
        results = response.get("results", [])
        if not results:
            return f"无法读取网页：{url}"
        return json.dumps([
            {"url": result.get("url", url), "content": result.get("raw_content", "")[:12000]}
            for result in results
        ], ensure_ascii=False)
    except Exception:
        return f"读取网页失败：{url}"


tools = [get_weather_for_location, get_user_location, get_current_time, web_search, web_extract]


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
model_with_tools = model.bind_tools(tools)
title_model = build_glm_model(temperature=0.2, max_tokens=40) if os.getenv("GLM_API_KEY") else None


class AgentState(dict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


def model_node(state: AgentState):
    return {"messages": [model_with_tools.invoke(state["messages"])]}


def tool_node(state: AgentState, runtime: ToolRuntime[Context]):
    from langgraph.prebuilt import ToolNode
    return ToolNode(tools).invoke(state, runtime=runtime)


def truncate_node(state: AgentState):
    max_len = 100
    messages = state["messages"]
    if len(messages) <= 1:
        return {}
    tail = messages[1:]
    if len(tail) <= max_len:
        return {}
    start = len(tail) - max_len
    while start < len(tail) and not isinstance(tail[start], HumanMessage):
        start += 1
    if start >= len(tail):
        return {}
    return {"messages": [RemoveMessage(id=message.id) for message in tail[:start]]}


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

_sqlite_context = SqliteSaver.from_conn_string(str(CHECKPOINT_PATH))
checkpointer = _sqlite_context.__enter__()
graph = builder.compile(checkpointer=checkpointer)


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def user_messages(thread_id: str) -> list[str]:
    """会话里所有用户消息的原文，按先后顺序。

    只暴露「读状态」：第几轮开始起标题、拿最近几条做上下文，这些业务判断
    属于应用层，由 service.py 决定。
    """
    state_values = graph.get_state(_config(thread_id)).values
    return [
        str(message.content)
        for message in state_values.get("messages", [])
        if isinstance(message, HumanMessage)
    ]


def delete_checkpoint_thread(thread_id: str) -> None:
    """只删 LangGraph 的 checkpoint，不碰会话元数据。"""
    checkpointer.delete_thread(thread_id)


def generate_title(context: str) -> str | None:
    """让标题模型给一段对话上下文起个短标题。

    没配标题模型、或调用失败时返回 None——回退文案和长度截断由调用方
    （service.py）决定，这里只负责「用模型产出标题」。
    """
    if title_model is None:
        return None
    try:
        response = title_model.invoke(
            "请根据下面的对话主题生成一个简短的中文会话标题。"
            "只输出标题，不要引号、标点解释或 Markdown，长度不超过 20 个字。\n\n"
            f"对话中的用户问题：\n{context}"
        )
    except Exception:
        return None
    title = str(response.content).strip().replace("\n", " ").strip("`\"'“”‘’")
    return title or None


def rewind_thread(thread_id: str, turn: int) -> dict[str, object]:
    """回退到第 turn 条用户消息发出之前。

    turn 是当前会话里用户消息的序号，从 0 开始。该条用户消息以及它之后的所有消息
    都会被删除，被删除的那条用户消息原文会返回给前端，方便放回输入框再编辑。

    删除必须从 HumanMessage 的位置切起：一个回合以 HumanMessage 开头，
    这样切不会留下没有前置 tool_calls 的 ToolMessage。
    """
    config = _config(thread_id)
    stored = list(graph.get_state(config).values.get("messages", []))
    human_positions = [
        index for index, message in enumerate(stored) if isinstance(message, HumanMessage)
    ]
    if turn < 0 or turn >= len(human_positions):
        raise ValueError("要回退的消息不存在")

    cut = human_positions[turn]
    removed = stored[cut:]
    graph.update_state(
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


def get_history(thread_id: str) -> list[dict[str, object]]:
    snapshot = graph.get_state(_config(thread_id))
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


def stream_agent(user_id: str, thread_id: str, user_input: str):
    """跑一遍图，产出 Agent 自己的事件流。

    只做「跑图 + 转发事件」：不碰会话元数据，也不发 title 事件。
    按需生成并推送标题是 service.chat_events() 的职责。
    """
    config = _config(thread_id)
    has_history = bool(graph.get_state(config).values.get("messages", []))
    messages = [HumanMessage(content=user_input)]
    if not has_history:
        messages.insert(0, SystemMessage(content=SYSTEM_PROMPT))

    # 工具调用只从 updates 通道播报一次。
    # 原先两条通道都播报，却用两套不相等的键去重：messages 通道用 tool_call_chunks
    # 的 index（整数），updates 通道用 tool_call 的 id（字符串 "call_xxx"），
    # 两者永不相等，于是同一次调用被报两遍，前端「调用工具」那行会重复显示。
    # 现在只留 updates 通道：它拿到的 AIMessage 是模型节点跑完后的完整 tool_calls
    # （参数齐全，而不是流式半截），按 id 去重也天然唯一。
    emitted_tool_calls: set[str] = set()
    for mode, chunk in graph.stream(
        {"messages": messages},
        config=config,
        context=Context(user_id=user_id),
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
