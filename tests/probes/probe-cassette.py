"""验证录制回放：record / replay / 未命中 / off 四条路径。

只调工具，不调模型 → 零 LLM token 消耗。

关键点：MCP 子进程的环境变量是在**会话建立时**从父进程继承的，所以要换模式就得
先关掉旧会话（agent_core.shutdown() 会把 MCP 客户端作废），再让下次调用重新拉起
子进程。这也是 eval/run.py 一次只跑一种模式的原因。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import agent_core  # noqa: E402

CASSETTE = ROOT / ".workbuddy" / "tmp" / "cassette-probe.json"
QUERY_A = "录制回放探针 固定查询 A"
QUERY_B = "录制回放探针 固定查询 B"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def call_search(query: str) -> str:
    """走一遍 agent 实际用的路径，把工具输出取成纯文本。

    MCP 适配器不会原样返回字符串，而是包成 content-block 列表
    [{"type": "text", "text": ..., "id": "lc_<uuid>"}]。cassette 是在**工具函数内部**
    录的，存的是工具的原始返回值，所以比较时要先把包装剥掉。
    """
    tools = {t.name: t for t in await agent_core.load_tools()}
    result = await tools["web_search"].ainvoke({"query": query, "topic": "general"})
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "".join(
            block.get("text", "")
            for block in result
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return json.dumps(result, ensure_ascii=False)


async def use_mode(mode: str) -> None:
    """切换模式。必须先 shutdown，才能让子进程拿到新的环境变量。"""
    await agent_core.shutdown()
    os.environ["TOOL_CASSETTE_MODE"] = mode
    os.environ["TOOL_CASSETTE_PATH"] = str(CASSETTE)


def counters() -> dict:
    if not CASSETTE.exists():
        return {"records": 0, "hits": 0, "misses": 0}
    return json.loads(CASSETTE.read_text(encoding="utf-8")).get("counters", {})


async def main() -> None:
    if CASSETTE.exists():
        CASSETTE.unlink()

    print("=== 1. record：真实调用并落盘 ===")
    await use_mode("record")
    value_a = await call_search(QUERY_A)
    print(f"  返回长度 {len(value_a)} 字符")
    check("cassette 文件已创建", CASSETTE.exists())
    check("记录数 = 1", counters().get("records") == 1, str(counters()))

    print("\n=== 2. record 再来一次：不重复写（只增不改）===")
    await use_mode("record")
    value_a2 = await call_search(QUERY_A)
    check("记录数仍为 1", counters().get("records") == 1, str(counters()))
    print(f"  两次真实调用的返回是否相同：{value_a == value_a2}（外部内容本来就该会变）")

    print("\n=== 3. replay：命中，返回录下来的内容 ===")
    await use_mode("replay")
    replayed = await call_search(QUERY_A)
    check("返回值与录制时逐字一致", replayed == value_a)
    check("命中数 = 1", counters().get("hits") == 1, str(counters()))

    print("\n=== 4. replay：未命中的入参，照常真实调用并计数 ===")
    await use_mode("replay")
    live = await call_search(QUERY_B)
    check("未命中数 = 1", counters().get("misses") == 1, str(counters()))
    check("未命中时仍拿到了真实结果", len(live) > 0, f"{len(live)} 字符")
    check("未命中不写入 cassette（interactions 仍为 1）",
          len(json.loads(CASSETTE.read_text(encoding="utf-8"))["interactions"]) == 1)

    print("\n=== 5. off：不读不写 ===")
    await use_mode("off")
    before = counters()
    await call_search(QUERY_A)
    after = counters()
    check("计数完全没有变化", before == after, f"{before} -> {after}")

    print("\n=== 6. replay 命中返回的内容，跨进程也是一致的 ===")
    await use_mode("replay")
    again = await call_search(QUERY_A)
    check("第二次 replay 与第一次 replay 逐字一致", again == replayed)

    print("\n=== 结论 ===")
    if failures:
        print(f"  {len(failures)} 项未通过：{failures}")
    else:
        print("  全部通过。replay 模式下同一个入参拿到的工具输出逐字一致，")
        print("  改动前后的差异只可能来自代码和模型，不再被外部内容淹没。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        try:
            asyncio.run(agent_core.shutdown())
        except Exception:
            pass
    sys.exit(1 if failures else 0)
