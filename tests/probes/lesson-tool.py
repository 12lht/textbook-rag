"""教学探针：一个普通函数 vs 一个 @tool，到底差在哪。

不看抽象名词，直接把两个对象摊出来比。
"""
from __future__ import annotations

import asyncio
from datetime import datetime


# ---------- 版本一：普通 Python 函数 ----------
def plain_time():
    """获取当前时间。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 版本二：只加一行 @tool ----------
from langchain.tools import tool


@tool
def tooled_time() -> str:
    """获取当前时间。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


print("=" * 60)
print("一、一个普通函数身上有什么")
print("=" * 60)
print(f"   type        : {type(plain_time)}")
print(f"   __name__    : {plain_time.__name__}")
print(f"   __doc__     : {plain_time.__doc__!r}")
print(f"   有 .name 吗 : {hasattr(plain_time, 'name')}")
print(f"   有 .invoke 吗: {hasattr(plain_time, 'invoke')}")
print(f"   直接调用     : {plain_time()}")

print()
print("=" * 60)
print("二、加了 @tool 之后，同一个函数变成了什么")
print("=" * 60)
print(f"   type          : {type(tooled_time)}")
print(f"   .name         : {tooled_time.name!r}")
print(f"   .description  : {tooled_time.description!r}")
print(f"   .invoke(空参数): {tooled_time.invoke({})}")
print(f"   .func（同步实现）: {tooled_time.func!r}"[:100])
print(f"   .coroutine（异步实现）: {tooled_time.coroutine}")

print()
print("=" * 60)
print("三、包在里面的『名片』——模型看到的就是这个")
print("=" * 60)
from langchain_core.utils.function_calling import convert_to_openai_tool

import json

print(json.dumps(convert_to_openai_tool(tooled_time), ensure_ascii=False, indent=2))

print()
print("=" * 60)
print("四、模型『调用』它的时候，实际发生什么")
print("=" * 60)
tool_call = {"name": "tooled_time", "args": {}, "id": "call_abc", "type": "tool_call"}
result = tooled_time.invoke(tool_call)
print(f"   我给进去的（模型的请求）: {tool_call}")
print(f"   拿回来的（框架的答复）  : {type(result).__name__}")
print(f"      content : {result.content!r}")
print(f"      tool_call_id : {result.tool_call_id!r}")
print(f"      name    : {result.name!r}")
print()
print("   注意：回来的不是字符串，是一条『工具消息』——它可以直接塞回对话历史里。")

print()
print("=" * 60)
print("五、不写 @tool 会怎样？")
print("=" * 60)

try:
    spec = convert_to_openai_tool(plain_time)
    print(f"   当名片转给模型：能转 -> {spec['function']['name']}")
    print("      （langchain 也会替它生成，但不是所有场景都认）")
except Exception as exc:
    print(f"   当名片转给模型：失败 -> {type(exc).__name__}: {exc}")

from langgraph.prebuilt import ToolNode

try:
    node = ToolNode([plain_time])
    print(f"   塞进 ToolNode（真正执行工具的那个环节）：能塞 -> {node}")
except Exception as exc:
    print(f"   塞进 ToolNode：失败 -> {type(exc).__name__}: {exc}")

try:
    node = ToolNode([tooled_time])
    print(f"   换成 @tool 版再塞 ToolNode：成功了 -> {type(node).__name__}")
except Exception as exc:
    print(f"   换成 @tool 版再塞 ToolNode：失败 -> {type(exc).__name__}: {exc}")
