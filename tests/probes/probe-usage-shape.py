"""一次性勘察：usage_metadata 里到底带了什么字段（尤其是缓存命中）。

结论会决定 bench 模块能否算「缓存命中率」。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import agent_core  # noqa: E402


LONG = "这是一段用于填充上下文的教材内容。" * 400


async def main() -> None:
    print("provider =", os.getenv("MODEL_PROVIDER", "deepseek"))
    print("model id =", getattr(agent_core.model, "model_name", "?"))
    print()

    for round_no in (1, 2):
        messages = [
            ("system", f"你是一个助手。下面是背景资料，请记住它：\n{LONG}"),
            ("user", f"第 {round_no} 次提问：请用一句话回答「收到」。" ),
        ]
        reply = await agent_core.model.ainvoke(messages)
        usage = getattr(reply, "usage_metadata", None)
        print(f"--- round {round_no} ---")
        print("usage_metadata =", json.dumps(usage, ensure_ascii=False, default=str, indent=2))
        meta = getattr(reply, "response_metadata", None) or {}
        token_usage = meta.get("token_usage") or meta.get("usage") or {}
        print("response_metadata.token_usage =", json.dumps(token_usage, ensure_ascii=False, default=str, indent=2))
        print("response_metadata keys =", list(meta.keys()))
        print()


asyncio.run(main())
