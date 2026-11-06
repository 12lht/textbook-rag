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

import ast
import json
import math
import os
from typing import Callable, Literal
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
    """查询中国城市天气。

    mode=current 查询实时天气，mode=forecast 查询未来几天预报；
    city 可以是城市名称或高德 adcode。
    """
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

_MAX_EXPRESSION_LENGTH = 2000   # 表达式长度上限
_MAX_INT_EXPONENT = 4096        # 整数幂的指数上限，拦住 9**9**9 这类天文数字
_MAX_FACTORIAL_ARG = 100_000    # 阶乘参数上限


class _CalculatorError(ValueError):
    """表达式里出现了白名单之外的语法。message 会原样返回给模型。"""


# 白名单：教材题目够用的函数集。字典而不是 if 链——加函数只改一行。
_ALLOWED_FUNCTIONS: dict[str, Callable[..., int | float]] = {
    "sqrt": math.sqrt, "exp": math.exp, "ln": math.log, "log": math.log,
    "log2": math.log2, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "degrees": math.degrees, "radians": math.radians,
    "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
    "gcd": math.gcd, "lcm": math.lcm, "hypot": math.hypot,
    "abs": abs, "round": round,
}

# 常量和函数分开两个字典：pi 是个值，不能被“调用”；混在一起就会出 pi(2) 这种怪调用。
_ALLOWED_CONSTANTS: dict[str, int | float] = {
    "pi": math.pi, "e": math.e, "tau": math.tau,
}


def _apply_binop(op: ast.operator, left: int | float, right: int | float) -> int | float:
    """二元运算。乘方在这里做指数守卫——它是唯一“结果会爆炸”的运算。"""
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Div):
        return left / right
    if isinstance(op, ast.FloorDiv):
        return left // right
    if isinstance(op, ast.Mod):
        return left % right
    if isinstance(op, ast.Pow):
        if isinstance(left, int) and isinstance(right, int) and abs(right) > _MAX_INT_EXPONENT:
            raise _CalculatorError("幂运算的指数太大，结果会是一个天文数字。")
        return left ** right
    raise _CalculatorError("不支持的运算符。")


def _evaluate(node: ast.expr) -> int | float:
    """递归求值。只放行：数字常量 / 二元运算 / 正负号 / 常量名 / 白名单函数调用。

    任何其他节点类型（属性访问、下标、lambda、import……）走到最后的 raise，
    不认识的语法根本不会被执行——这是和 eval 的本质区别。未知节点绝不静默
    返回 None：对 LLM 工具，明确的报错是反馈，假结果是毒药。
    """
    if isinstance(node, ast.Constant):
        # bool 是 int 的子类，True/False 不是数字，要先排掉
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _CalculatorError(f"只支持数字，不支持 {node.value!r}。")
        return node.value
    if isinstance(node, ast.BinOp):
        return _apply_binop(node.op, _evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
        raise _CalculatorError("不支持的一元运算符。")
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTANTS:
            return _ALLOWED_CONSTANTS[node.id]
        raise _CalculatorError(f"不认识的名字“{node.id}”，只支持常量 pi、e、tau。")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
            raise _CalculatorError("只支持计算器内置的数学函数。")
        if node.keywords:
            raise _CalculatorError("函数参数按位置传，不用关键字形式。")
        arguments = [_evaluate(argument) for argument in node.args]
        if node.func.id == "factorial" and arguments and arguments[0] > _MAX_FACTORIAL_ARG:
            raise _CalculatorError("阶乘的参数太大。")
        return _ALLOWED_FUNCTIONS[node.func.id](*arguments)
    raise _CalculatorError("表达式里含有不支持的语法。")


def _format_number(value: int | float) -> str:
    """整数原样输出；浮点数保留 15 位有效数字，抹掉 0.1+0.2 之类的浮点毛刺。"""
    if isinstance(value, int):
        return str(value)
    if math.isfinite(value) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return format(value, ".15g")


@recorded("calculator")
def calculator(expression: str) -> str:
    """精确计算一个数学表达式。凡是数值计算——四则运算、乘方、开方、对数、
    三角函数、阶乘、取余等——都必须调用本工具，不要心算或手算。

    expression 用 Python 算式书写，一次一个表达式，例如：
    "(1234 + 5678) * 2"；乘方写 "2 ** 10" 或 "2 ^ 10"；开方 "sqrt(2)"；
    对数 "log(1000, 10)"（省略底数时是自然对数 ln）；阶乘写 "factorial(6)"，
    不支持 6! 这种写法；三角函数用弧度，角度先转换：sin(radians(30))。
    支持常量 pi、e、tau，以及 floor / ceil / round / abs / gcd / lcm / hypot / exp。
    多步计算请逐步调用，把上一步结果代进下一步。
    """
    text = (expression or "").strip()
    if not text:
        return "请提供一个数学表达式，例如“(3.5 + 7) * 2”或“sqrt(2)”。"
    if len(text) > _MAX_EXPRESSION_LENGTH:
        return "表达式太长，请拆成多次计算。"
    # 模型常把乘方写成 ^（异或），先统一翻译成 **，避免 2 ^ 10 被静默算成 8
    cleaned = text.replace("^", "**")
    try:
        tree = ast.parse(cleaned, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return f"无法解析的表达式：“{text}”。请写成 Python 算式，例如“(1 + 2) * 3”或“log(1000, 10)”。"
    try:
        value = _evaluate(tree.body)
    except _CalculatorError as error:
        return str(error)
    except ZeroDivisionError:
        return "错误：除数（或模）为零。"
    except OverflowError:
        return "错误：中间结果或参数超出了浮点数范围。"
    except (ValueError, TypeError):
        return "错误：参数超出函数定义域或类型不对（例如对负数开方、对 0 取对数）。"
    except RecursionError:
        return "错误：表达式嵌套太深，请拆成多次计算。"
    # (-8) ** 0.5 这类运算 Python 会返回复数，同样不算“计算成功”
    if isinstance(value, complex):
        return "错误：结果不是实数（可能是对负数做非整数次幂）。"
    if not math.isfinite(value):
        return "错误：结果无穷大或不是数。"
    return _format_number(value)


ALL_TOOLS = [
    get_weather_for_location,
    get_current_time,
    web_search,
    web_extract,
    calculator,
]
