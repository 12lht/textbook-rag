"""独立验证 mcp_server.py：工具名 / 参数 schema / 描述是否与改造前一致。

对比基准是改造前用 langchain tool_call_schema 导出的
.workbuddy/tmp/schema-inprocess.json。如果这里不一致，
后面 agent_core 的 A/B 对比就不公平——模型看到的接口变了。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 关键：agent_core 在 import 时会 load_dotenv()，所以它传给 MCP 子进程的
# os.environ 里带着 API key。这个探针必须做同样的事，否则子进程拿不到
# AMAP_API_KEY，工具会静默退化成一堆「服务未配置」。
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASELINE = ROOT / ".workbuddy" / "tmp" / "schema-inprocess.json"

# 改造前 langchain 的函数名 -> 基线里的顺序
BEFORE_ORDER = [
    "get_weather_for_location",
    "get_user_location",
    "get_current_time",
    "web_search",
    "web_extract",
]


async def main() -> int:
    before = {s["title"]: s for s in json.loads(BASELINE.read_text(encoding="utf-8"))}

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py")],
        env=dict(os.environ),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"连接成功：server={init.serverInfo.name} "
                  f"version={init.serverInfo.version} "
                  f"protocol={init.protocolVersion}")

            listed = await session.list_tools()
            after = {t.name: t for t in listed.tools}

            print(f"\n工具数：改造前 {len(before)} → 现在 {len(after)}")

            missing = sorted(set(before) - set(after))
            added = sorted(set(after) - set(before))
            print(f"丢失的工具：{missing or '无'}")
            print(f"新增的工具：{added or '无'}")

            print("\n=== 逐个对比 ===")
            all_ok = not missing and not added
            for name in BEFORE_ORDER:
                if name not in after:
                    continue
                b = before[name]
                a_schema = after[name].inputSchema
                b_props = sorted(b.get("properties", {}).keys())
                a_props = sorted(a_schema.get("properties", {}).keys())
                b_req = sorted(b.get("required", []))
                a_req = sorted(a_schema.get("required", []))
                a_desc = (after[name].description or "").strip()
                b_desc = (b.get("description") or "").strip()

                ok = b_props == a_props and b_req == a_req and a_desc == b_desc
                all_ok = all_ok and ok
                print(f"\n{name}  {'一致' if ok else '不一致'}")
                print(f"  参数    改造前 {b_props}  →  现在 {a_props}")
                print(f"  必填    改造前 {b_req}  →  现在 {a_req}")
                print(f"  描述长度 改造前 {len(b_desc)}  →  现在 {len(a_desc)}")
                print(f"  描述文本 {a_desc}")
                if a_desc != b_desc:
                    print(f"  !! 描述被改动，改造前是：{b_desc[:80]}")

            # 真跑一次工具，确认 stdio 通道能把结果带回来（含中文）
            print("\n=== 实调 get_current_time ===")
            call = await session.call_tool("get_current_time", {})
            text = call.content[0].text if call.content else "(空)"
            print(f"  isError={call.isError}  返回={text}")

            print("\n=== 实调 get_user_location（需要网络与 AMAP key）===")
            call2 = await session.call_tool("get_user_location", {})
            text2 = call2.content[0].text if call2.content else "(空)"
            print(f"  isError={call2.isError}  返回={text2}")

            schema_chars = len(json.dumps(
                [after[n].inputSchema for n in BEFORE_ORDER if n in after],
                ensure_ascii=False,
            ))
            print(f"\nMCP inputSchema 总字符数 = {schema_chars}（改造前 langchain 为 999）")

            print("\n=== schema 差异定位：MCP 比 langchain 少了什么 ===")
            for name in BEFORE_ORDER:
                if name not in after:
                    continue
                b = before[name]
                a = after[name].inputSchema
                b_len = len(json.dumps(b, ensure_ascii=False, sort_keys=True))
                a_len = len(json.dumps(a, ensure_ascii=False, sort_keys=True))
                print(f"\n{name}: langchain {b_len} → MCP {a_len} 字符")
                extra_b = sorted(set(b.keys()) - set(a.keys()))
                extra_a = sorted(set(a.keys()) - set(b.keys()))
                print(f"  顶层键：仅 langchain {extra_b} / 仅 MCP {extra_a}")
                b_prop = b.get("properties", {})
                a_prop = a.get("properties", {})
                for p in sorted(set(b_prop) | set(a_prop)):
                    bk = sorted(set(b_prop.get(p, {}).keys()))
                    ak = sorted(set(a_prop.get(p, {}).keys()))
                    flag = "" if bk == ak else "   <<< 不同"
                    print(f"  参数 {p}: langchain {bk} / MCP {ak}{flag}")
                    if bk != ak:
                        print(f"      langchain: {json.dumps(b_prop.get(p), ensure_ascii=False)}")
                        print(f"      MCP      : {json.dumps(a_prop.get(p), ensure_ascii=False)}")

            print(f"\n工具接口一致性：{'全部通过' if all_ok else '存在差异'}")
            return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
