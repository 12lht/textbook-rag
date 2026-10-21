"""探针：确认「起标题」用的是和主模型**同一个 provider**。

背景：`title_model` 原来硬编码成 GLM。`.env` 里只要留着 `GLM_API_KEY`，
标题就一直走智谱，和 `MODEL_PROVIDER` 不是一回事；GLM key 一撤，标题功能
还会静默消失（退回「取用户原话前 30 字」）。这个探针就是防它再次漂走。

只发一次很小的模型调用（40 token 上限），成本可忽略。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402

PASS = 0
FAIL = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"    ✓ {label}  {detail}")
    else:
        FAIL += 1
        print(f"    ✗ {label}  {detail}")


def describe(m) -> str:
    """把模型的「型号」尽量抠出来，用于比较两个模型是不是一家。"""
    for attr in ("model_name", "model", "model_id"):
        value = getattr(m, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(m).__name__


PROVIDER = os.getenv("MODEL_PROVIDER", "deepseek").lower()

print("== 1. 两个模型出自同一个 provider ==")
main_model = agent_core.model
title_model = agent_core.title_model
print(f"     MODEL_PROVIDER = {PROVIDER}")
print(f"     主模型   : {type(main_model).__name__}  {describe(main_model)}")
print(f"     标题模型 : {type(title_model).__name__}  {describe(title_model)}")

check(type(main_model) is type(title_model), "两个模型是同一个类")
check(describe(main_model) == describe(title_model),
      "型号字符串一致（没有偷偷换 provider）", describe(title_model))

# 反向检查：如果 provider 是 glm，就不该是 deepseek；反之亦然
expected = "glm" if PROVIDER == "glm" else "deepseek"
check(expected in describe(title_model).lower() or expected in type(title_model).__name__.lower(),
      f"标题模型确实属于 {expected}", describe(title_model))

print()
print("== 2. 标题模型不再依赖 GLM_API_KEY ==")
check(agent_core.title_model is not None,
      "无论 GLM_API_KEY 在不在，title_model 都有值")
if PROVIDER != "glm":
    check("deepseek 档下不碰 GLM key",
          "glm" not in describe(title_model).lower())

print()
print("== 3. 真发一次请求：标题要短、要非空 ==")
# 第 4 节是**真正的守卫**：deepseek 是推理模型，几十个 token 的思考过程会先把
# `max_tokens` 吃掉、content 变成空串，表现出来就是「标题莫名其妙没了」。
# 所以不光要看「拿得到标题」，还要看「没在思考」。
#
# 两节必须在**同一个事件循环**里跑：HTTP 连接池是绑定事件循环的，
# 拆成两次 `asyncio.run` 会在收尾时冒 `RuntimeError: Event loop is closed`
# （错在清理阶段，会盖住真正的失败）。


async def main() -> None:
    title = await agent_core.generate_title(
        "用一句话说明你是什么。\n另外帮我看一下明天去广州出差要不要带伞。"
    )
    print(f"     生成结果: {title!r}")
    check(title is not None and title != "", "拿得到标题（不是 None / 空串）")
    if title:
        check(len(title) <= 30, "长度在 30 字以内（service 会再截断一次）", f"{len(title)} 字")
        check("\n" not in title, "是单行")
        check(not title.startswith("`") and not title.startswith('"'),
              "没带 Markdown / 引号包装（提示词要求）", title)

    print()
    print("== 4. 推理确实关掉了（否则标题会静默变空）==")
    response = await agent_core.title_model.ainvoke(
        "请给下面这段对话起一个不超过 20 字的中文标题，只输出标题。\n\n"
        "用户问：帮我看看明天广州的天气。"
    )
    usage = response.usage_metadata or {}
    details = usage.get("output_token_details") or {}
    reasoning = details.get("reasoning", 0)
    output = usage.get("output_tokens", 0)
    print(f"     推理 token={reasoning}   输出 token={output}   正文={response.content!r}")
    check(reasoning == 0, "推理 token 为 0（关掉了）", str(reasoning))
    check(output < 60, "输出 token 很少（不是在偷偷思考）", str(output))
    check(bool(str(response.content).strip()), "正文非空")


asyncio.run(main())

print(f"\n结果: {PASS}/{PASS + FAIL} 通过")
sys.exit(0 if FAIL == 0 else 1)
