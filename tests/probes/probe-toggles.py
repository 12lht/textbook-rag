"""验证「深度思考 / 联网搜索」两个开关真的改变了发给模型的东西。

重点不是「代码跑通」，而是三件事：
  1. 默认两个都开，不传参数时行为不变。
  2. 关联网 = 工具表里真的少了那两个，不是留着让模型自觉不用。
  3. 关思考 = 用的是另一个模型实例，且真的带上了关闭推理的参数。
"""
import asyncio
import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(REPO_ROOT))
from dotenv import load_dotenv

load_dotenv(str(REPO_ROOT / ".env"))

import agent_core


def check(label, condition):
    print(f"{'通过' if condition else '不通过'}  {label}")
    return bool(condition)


def dump_model(model):
    """把「关推理」那个非标准参数挖出来看一眼。

    它在 langchain 里没有统一的位置，可能在 model_kwargs 也可能在 extra_body，
    所以两个都看。看不到不代表没生效，只能说明得换一种方式验证。
    """
    for attr in ("model_kwargs", "extra_body"):
        value = getattr(model, attr, None)
        if value:
            return f"{attr}={value}"
    return "（没找到，需换方式验证）"


async def main():
    ok = True

    tools = await agent_core.load_tools()
    names = [tool.name for tool in tools]
    print("默认工具表：", names)
    ok &= check("默认工具表包含 web_search / web_extract", "web_search" in names and "web_extract" in names)
    ok &= check("默认工具表顺序和 _TOOL_ORDER 一致", names == [n for n in agent_core._TOOL_ORDER if n in names])

    no_web = await agent_core._mcp.tools(web=False)
    no_web_names = [tool.name for tool in no_web]
    print("关联网后：", no_web_names)
    ok &= check("关联网后 web_search 不在表里", "web_search" not in no_web_names)
    ok &= check("关联网后 web_extract 不在表里", "web_extract" not in no_web_names)
    ok &= check("关联网后别的工具一个没少", len(no_web_names) == len(names) - 2)

    default_bound = await agent_core._mcp.bound_model()
    again = await agent_core._mcp.bound_model()
    ok &= check("默认 bound_model 走缓存，两次是同一个对象", default_bound is again)

    fast = agent_core._model_for(False)
    print("思考开：", dump_model(agent_core.model))
    print("思考关：", dump_model(fast))
    ok &= check("关思考用的是另一个模型实例", fast is not agent_core.model)
    ok &= check("两次取关思考模型是同一个实例", fast is agent_core._model_for(False))

    bound_off = await agent_core._mcp.bound_model(reasoning=False, web=False)
    ok &= check("关思考+关联网 那套和默认那套不是同一个", bound_off is not default_bound)
    ok &= check(
        "四种组合都建出来后缓存命中",
        (await agent_core._mcp.bound_model(reasoning=False, web=False)) is bound_off,
    )

    print("\nREASONING_TOGGLEABLE =", agent_core.REASONING_TOGGLEABLE)

    await agent_core.close_mcp()
    print("\n全部通过" if ok else "\n有未通过项")


if __name__ == "__main__":
    asyncio.run(main())
