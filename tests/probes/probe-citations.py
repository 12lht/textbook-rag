# -*- coding: utf-8 -*-
"""引用溯源后端探针：解析函数、get_history 重建、_decorate_history。

checkpoint 用临时库，不碰生产 data/*.sqlite3。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "D:/agent")

import agent_core  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{('  ' + str(detail)) if detail else ''}")
    if not condition:
        failures.append(label)


# ---------------------------------------------------------------------------
# 1. _citations_from_tool_text：工具返回文本 → 引用条目
# ---------------------------------------------------------------------------
TOOL_TEXT = (
    "[c1] 移动通信 · 第3章 分集接收 · 第 45-46 页 · 图\n"
    "![图 3-4 MRC 合并](as_1f3a.png)\n"
    "最大比值合并的示意图。\n"
    "\n"
    "[c2] 通信原理（第7版）\n"
    "抽样定理：如果模拟信号的带宽有限……\n"
    "（第二行，中间有空行也算同一条的原文）\n"
    "\n"
    "[c3] 移动通信 · 第 12 页 · 公式\n"
    "γ_MRC = sum(|h_i|^2) 的推导过程。\n"
)

items = agent_core._citations_from_tool_text(TOOL_TEXT)
check("解析出 3 条引用", len(items) == 3, len(items))

c1 = items[0]
check("c1 编号", c1["code"] == "c1", c1["code"])
check("c1 书名", c1["doc_title"] == "移动通信", c1["doc_title"])
check("c1 章节", c1["chapter"] == "第3章 分集接收", c1["chapter"])
check("c1 页码区间", c1["page_start"] == 45 and c1["page_end"] == 46, (c1["page_start"], c1["page_end"]))
check("c1 kind=figure", c1["kind"] == "figure", c1["kind"])
check("c1 原文含图引用", "![图 3-4 MRC 合并](as_1f3a.png)" in c1["content"], c1["content"][:40])

c2 = items[1]
check("c2 只有书名", c2["doc_title"] == "通信原理（第7版）" and c2["chapter"] is None, c2)
check("c2 无页码", c2["page_start"] is None, c2["page_start"])
check("c2 无 kind", c2["kind"] == "", c2["kind"])
check("c2 原文跨空行完整", "第二行" in c2["content"] and "\n" in c2["content"], c2["content"])

c3 = items[2]
check("c3 单页码", c3["page_start"] == 12 and c3["page_end"] == 12, (c3["page_start"], c3["page_end"]))
check("c3 kind=formula", c3["kind"] == "formula", c3["kind"])

# 空 / 无命中文本
check("空文本给空列表", agent_core._citations_from_tool_text("教材里没有找到相关内容。") == [])

# ---------------------------------------------------------------------------
# 1b. _figure_markdown：图类命中补 markdown 图片行
# ---------------------------------------------------------------------------
md = agent_core._figure_markdown(
    {"kind": "figure", "asset_id": "as_1f3a", "content": "图 9.2 系统提示(校准)流程\n其他行"},
    "图 9.2 系统提示(校准)流程\n其他行",
)
check("图类命中补出图片行", md == "\n![图 9.2 系统提示（校准）流程](as_1f3a.png)", md)
check("alt 清洗了 ] 和括号", "]" not in md.split("](")[0] and "(" not in md.split("](")[0].replace("![", "", 1), md)
check("非图类不补", agent_core._figure_markdown({"kind": "text", "asset_id": "as_1f3a"}, "正文") == "")
check("无 asset_id 不补", agent_core._figure_markdown({"kind": "figure", "content": "图 1"}, "图 1") == "")
check("空题注兜底", agent_core._figure_markdown({"kind": "figure", "asset_id": "as_9"}, "") ==
      "\n![教材插图](as_9.png)", agent_core._figure_markdown({"kind": "figure", "asset_id": "as_9"}, ""))


# ---------------------------------------------------------------------------
# 2. get_history 端到端：合成一个带教材检索的 checkpoint（临时库）
# ---------------------------------------------------------------------------
async def history_case():
    # 不用 TemporaryDirectory：AsyncSqliteSaver 的连接要等 graph_stack 关掉才释放，
    # 自动清理会撞 WinError 32。固定探针路径，用完显式关闭再删。
    db_path = Path("D:/agent/.workbuddy/tmp/probe-cite-checkpoint.sqlite3")
    # checkpoint 是固定路径，上次运行的消息还在（add_messages 是累加的），
    # 不删的话 get_history 会翻倍。
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    agent_core.CHECKPOINT_PATH = db_path
    graph = await agent_core._ensure_graph()
    config = agent_core._config("probe-cite-thread")

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    msgs = [
        HumanMessage(content="第3章的分集接收讲了什么？"),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "search_textbook",
                "args": {"query": "分集接收 MRC"},
                "id": "call_1",
                "type": "tool_call",
            }],
        ),
        ToolMessage(content=TOOL_TEXT, name="search_textbook", tool_call_id="call_1"),
        AIMessage(content="书里用 [c1] 讲了 MRC，见 [c2] 抽样定理。"),
        HumanMessage(content="谢谢"),
        AIMessage(content="不客气，有其他问题随时问。"),
    ]
    await graph.aupdate_state(config, {"messages": msgs}, as_node="model")
    history = await agent_core.get_history("probe-cite-thread")
    try:
        await agent_core._graph_stack.aclose()
    except Exception:
        pass
    agent_core._graph = None
    return history


history = asyncio.run(history_case())
check("历史 4 条消息", len(history) == 4, len(history))
ans = history[1]
check("助手消息带 operations", [o["kind"] for o in ans.get("operations", [])] == ["call", "result"],
      ans.get("operations"))
cites = ans.get("citations") or []
check("助手消息带 3 条引用", len(cites) == 3, len(cites))
check("引用编号完整", [c["code"] for c in cites] == ["c1", "c2", "c3"], [c["code"] for c in cites])
check("引用内容非空", all(c["content"] for c in cites))
check("c1 图引用保留在原文里", any("as_1f3a.png" in c["content"] for c in cites))

check("普通轮没有 citations 字段", "citations" not in history[3])
check("最后一轮 user 无 citations", "citations" not in history[2])


# ---------------------------------------------------------------------------
# 3. _decorate_history：图片 URL 内联 + 引用条目补 URL
# ---------------------------------------------------------------------------
import service  # noqa: E402

messages = [
    {"role": "user", "content": "讲讲图 3-4"},
    {
        "role": "assistant",
        "content": "如图 [c1] 所示：\n\n![图 3-4 MRC](as_1f3a.png)",
        "citations": [{
            "code": "c1", "doc_title": "移动通信", "kind": "figure",
            "asset_id": "as_1f3a",
            "content": "![图 3-4 MRC](as_1f3a.png)\n示意图",
        }],
    },
    {"role": "assistant", "content": "普通回答，没有图。"},
]
decorated = service._decorate_history(messages, "u-probe")
body = decorated[1]["content"]
check("正文图引用换成签名 URL", "/api/assets/as_1f3a?u=u-probe&t=" in body, body)
check("正文引用编号保留", "[c1]" in body)
cite = decorated[1]["citations"][0]
check("引用条目补了 asset_url", cite.get("asset_url", "").startswith("/api/assets/as_1f3a?u=u-probe&t="),
      cite.get("asset_url"))
check("引用原文里的图也内联了", "/api/assets/as_1f3a" in cite["content"], cite["content"])
check("普通助手消息原样", decorated[2]["content"] == "普通回答，没有图。")
check("user 消息不被动", decorated[0]["content"] == "讲讲图 3-4")

# 签名能验回去（防的是「前端拿到 URL 却 403」）
url = cite["asset_url"]
from urllib.parse import parse_qs, urlparse  # noqa: E402

parsed = urlparse(url)
qs = parse_qs(parsed.query)
check("签名验签通过", service.verify_asset("as_1f3a", qs["u"][0], qs["t"][0]))

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部通过")
