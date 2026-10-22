"""工具实现层：MCP server 里那 4 个工具的真实逻辑，与 Agent 框架完全解耦。

这一层不 import langchain、不 import langgraph，只有标准库和 tavily。
它唯一的消费方是 `mcp_server.py`。

改造前这些函数是写在 `agent_core.py` 里、用 langchain 的 `@tool` 装饰器包装的，
`import` 那一刻就绑死在 Agent 进程里；现在它们跑在独立的 MCP server 进程，
`agent_core` 通过 MCP 协议调用（对比 `git show pre-mcp:agent_core.py`）。

**这里的工具只能是「纯函数」——返回值只由参数决定。**
`get_user_location` 曾经也在这里，后来被搬回了 `agent_core.py`，原因见下面它原来的位置。

注意：每个函数的 docstring 就是模型看到的工具描述，改它就等于改提示词。

每个工具还套了一层 `cassette.recorded`，用于评测录制回放。默认模式是 off，
直接调用外部 API，行为和没套一样；只有评测时把 `TOOL_CASSETTE_MODE` 设成
record / replay 才会生效。装饰器用 `functools.wraps`，不影响 docstring 和签名。
"""
from __future__ import annotations

import json
import os
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from tavily import TavilyClient

from cassette import recorded


@recorded("get_weather_for_location")
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




@recorded("get_current_time")
def get_current_time() -> str:
    """获取当前时间。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@recorded("web_search")
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


@recorded("web_extract")
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


ALL_TOOLS = [
    get_weather_for_location,
    get_current_time,
    web_search,
    web_extract,
]
