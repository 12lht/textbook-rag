"""标定 `_estimate_tokens` 的误差，并盯住「工具结果被转义」这个坑。

裁剪阈值按 token 算，但 token 是**估**出来的——没装 tokenizer，用的是
DeepSeek 文档给的「1 汉字 ≈ 0.6 token、1 个英文字母 ≈ 0.3 token」。这里拿真实
`usage.input_tokens` 去对，回答三个问题：

1. 那段换算比例到底偏多少？中英混排、markdown、工具返回分别怎么样？
2. 请求里那些「不随对话增长」的开销（系统提示附加段 + 工具 schema）估得准不准？
3. **工具返回的 content-block 有没有被 `json.dumps` 转义成 `\\u5faa` 发出去？**
   （2026-10-22 发现的真问题，`_flatten_for_model` 就是为它写的）

第 1 组是纯本地断言，不需要 API key；其余要真实发请求，打 `requires_model`。
原 `tests/probes/probe-token-estimate.py`，2026-11-03 迁到 pytest。
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import agent_core

SAMPLES = {
    "纯中文 200 字": (
        "正交频分复用是一种多载波传输技术，它把高速数据流分解成若干个低速子流，"
        "让它们在不同的子载波上并行传输。子载波之间保持正交，因此频谱可以重叠，"
        "频谱利用率显著提高。为了对抗多径时延扩展，每个符号前要插入循环前缀，"
        "只要循环前缀的长度大于最大时延扩展，符号间干扰就可以完全消除。"
        "接收端通过快速傅里叶变换完成解调，实现复杂度远低于单载波均衡。"
    ),
    "纯英文 200 字符": (
        "Orthogonal frequency division multiplexing splits a high-rate data stream "
        "into many low-rate subcarriers that stay orthogonal, so their spectra may "
        "overlap and the spectral efficiency improves a lot."
    ),
    "中英混排 markdown": (
        "## 3.2 MIMO 信道容量\n\n"
        "对于 $N_t$ 发 $N_r$ 收的 MIMO 系统，信道容量为\n\n"
        "$$C = \\log_2 \\det\\left(I + \\frac{SNR}{N_t} H H^H\\right)$$\n\n"
        "其中 `H` 是 $N_r \\times N_t$ 的信道矩阵。当收发天线数相同时，容量随天线数"
        "线性增长（Telatar, 1999）。注意：这里的 `SNR` 是**总**发射功率归一化之后的"
        "每根接收天线的信噪比，不是单流 SNR。\n"
    ),
}

TOOL_PAYLOAD = {
    "status": "success",
    "chunks": [
        {
            "chunk_id": "doc-0001-0012",
            "text": "循环前缀的长度必须大于信道的最大时延扩展，否则相邻符号之间会产生干扰。",
        },
        {
            "chunk_id": "doc-0001-0013",
            "text": "OFDM 的峰均功率比偏高，工程上常用削峰、预失真或者编码来缓解。",
        },
    ],
}


def estimate(messages, *, system: str | None = None) -> int:
    """按 `agent_core` 的口径估一次请求的输入 token。"""
    total = agent_core._estimate_tokens(system) if system else 0
    for message in messages:
        total += agent_core._message_tokens(message)
    return total


def sent(messages, *, system: str | None = None):
    """走生产代码那条路：摊平工具结果 → 并系统消息。量的必须是真发出去的东西。"""
    state = {"messages": ([SystemMessage(content=system)] if system else []) + list(messages)}
    return agent_core._messages_for_model(state)


def tool_turn(index: int = 0) -> list:
    """一轮教材问答：问 → 调检索 → 返回两段 chunk → 答。"""
    return [
        HumanMessage(content=f"第 {index} 个问题：OFDM 的循环前缀要多长？"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_textbook",
                    "args": {"query": f"循环前缀 {index}", "top_k": 6},
                    "id": f"c{index}",
                }
            ],
        ),
        ToolMessage(
            content=[{"type": "text", "text": json.dumps(TOOL_PAYLOAD, ensure_ascii=False)}],
            tool_call_id=f"c{index}",
            name="search_textbook",
        ),
        AIMessage(content="循环前缀要大于信道的最大时延扩展，否则会有符号间干扰。"),
    ]


@pytest.fixture
def model(monkeypatch):
    """只构造不调用；没有 key 时给个假的，仅用于拿 `_get_request_payload`。

    function scope 是必须的：httpx 的 client 绑在创建它的事件循环上，
    跨 `asyncio.run` 复用同一个实例会报 "Event loop is closed"。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", os.getenv("DEEPSEEK_API_KEY") or "dummy-key")
    return agent_core.build_model(temperature=0, max_tokens=1, disable_reasoning=True)


async def measure(model, messages) -> int:
    """发一次真实请求，只取 input_tokens。`max_tokens=1` 让输出几乎不花钱。"""
    reply = await model.ainvoke(messages)
    usage = getattr(reply, "usage_metadata", None) or {}
    return int(usage.get("input_tokens") or 0)


def test_tool_result_content_blocks_are_flattened(model):
    """工具结果的 content-block 不能被 `json.dumps` 转义成 `\\u5faa` 发出去。"""
    raw = tool_turn(0)
    flattened = sent(raw)
    body = json.dumps(model._get_request_payload(flattened)["messages"], ensure_ascii=False)

    assert "\\u" not in body, f"请求体里有转义：{body[:120]}…"
    assert isinstance(flattened[2].content, str), type(flattened[2].content).__name__
    assert isinstance(raw[2].content, list), "原来的 state 被就地改了"


@pytest.fixture
def bare_backend(monkeypatch):
    """标定时没有注入任何后端——引导段既不算进 est，也不真发出去。

    不隔离的话结果会被导入顺序左右：`test_asset_render` 那边 `import api` 会顺手
    调用 `set_retrieval_backend` 和 `set_memory_backend`，之后 `_messages_for_model`
    就把「关于长期记忆……」那段拼进 system（11 字符 → 110 字符），而 est 仍按
    没有引导算，实测 185 vs 486、偏差 60%。**全量跑才暴露，单跑这个文件是绿的。**

    四个全局都要清：检索后端一个，记忆后端拆成 read / write / forget 三个。
    """
    monkeypatch.setattr(agent_core, "_retrieval_search", None)
    monkeypatch.setattr(agent_core, "_memory_read", None)
    monkeypatch.setattr(agent_core, "_memory_write", None)
    monkeypatch.setattr(agent_core, "_memory_forget", None)


@pytest.mark.requires_model
def test_estimate_error_within_tolerance(model, bare_backend):
    """字符数 → token 的换算误差。偏差只用来决定系数要不要补，不是精确断言。"""
    asyncio.run(_check_estimate_error(model))


async def _check_estimate_error(model) -> None:
    tolerances = {
        # 纯中文按 0.6 估会偏高：实际 tokenizer 对常用汉字更省
        "纯中文 200 字": (-0.25, 0.25),
        # 英文按 0.3 估偏低：BPE 对长单词的切分比字符比例更粗
        "纯英文 200 字符": (0.0, 0.55),
        "中英混排 markdown": (-0.35, 0.25),
    }
    outliers = []
    for name, text in SAMPLES.items():
        actual = await measure(model, sent([HumanMessage(content=text)]))
        est = estimate([HumanMessage(content=text)])
        low, high = tolerances.get(name, (-0.25, 0.25))
        ratio = (est - actual) / max(actual, 1)
        if not (low <= ratio <= high):
            outliers.append(f"{name}：est={est} actual={actual} 偏差 {ratio:+.0%} 超出 {low:+.0%}~{high:+.0%}")
    assert not outliers, "；".join(outliers)


@pytest.mark.requires_model
def test_tool_turn_and_long_history_stay_within_tolerance(model, bare_backend):
    """一轮带检索的问答、以及 8 轮累计历史，估算都要撑得住。"""
    asyncio.run(_check_tool_turn(model))


async def _check_tool_turn(model) -> None:
    system = agent_core._system_prompt(memory_enabled=True)
    tools = await agent_core.load_tools()
    bound = model.bind_tools(tools)
    schema_tokens = agent_core._tool_schema_tokens()
    try:
        plain_turn = tool_turn(0)
        actual = await measure(model, sent(plain_turn))
        est = estimate(plain_turn)
        assert abs(est - actual) / max(actual, 1) <= 0.30, f"单轮：est={est} actual={actual}"

        history = [message for index in range(8) for message in tool_turn(index)]
        actual = await measure(bound, sent(history, system=system))
        est = estimate(history, system=system) + schema_tokens
        assert abs(est - actual) / max(actual, 1) <= 0.25, f"多轮：est={est} actual={actual}"
    finally:
        await agent_core.close_mcp()


@pytest.mark.requires_model
def test_tool_schema_overhead_is_estimated_reasonably(model):
    """工具 schema 的固定开销：估算和实测增量（绑定前后差值）要对得上。"""
    asyncio.run(_check_tool_schema(model))


async def _check_tool_schema(model) -> None:
    system = agent_core._system_prompt(memory_enabled=True)
    tools = await agent_core.load_tools()
    bound = model.bind_tools(tools)
    one = [HumanMessage(content="你好")]
    try:
        # 必须在 close_mcp 之前取：它读的是 MCP 客户端缓存的工具表，关掉会话就清空了。
        est_schema = agent_core._tool_schema_tokens()
        plain = await measure(model, sent(one, system=system))
        with_tools = await measure(bound, sent(one, system=system))
    finally:
        await agent_core.close_mcp()

    actual_schema = with_tools - plain
    assert actual_schema > 0, f"绑定工具后 token 没增加（{plain} → {with_tools}）"
    assert abs(est_schema - actual_schema) / max(actual_schema, 1) <= 0.30, (
        f"工具 {len(tools)} 个：est={est_schema} actual={actual_schema}"
    )


@pytest.mark.requires_model
def test_array_content_format_is_accepted():
    """数组格式的 tool content 认不认——用来定「摊平还是透传」，所以绕过适配层直接问。"""
    from openai import OpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("没有 DEEPSEEK_API_KEY，无法直连 API 验证")

    raw = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    sample = "循环前缀要大于信道的最大时延扩展，否则相邻符号之间会产生干扰。"

    def payload(tool_content):
        return [
            {"role": "user", "content": "书上怎么讲循环前缀"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "c1",
                        "function": {"name": "search_textbook", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": tool_content},
        ]

    def ask(tool_content) -> int | None:
        try:
            reply = raw.chat.completions.create(
                model="deepseek-flash",
                messages=payload(tool_content),
                max_tokens=1,
                extra_body={"reasoning_effort": "none"},
            )
            return reply.usage.prompt_tokens
        except Exception:  # noqa: BLE001 — 被拒也是一条结论，由断言去判
            return None

    as_string = ask(sample)
    as_array = ask([{"type": "text", "text": sample}])

    assert as_array is not None, "数组格式被拒了"
    assert as_string is not None and as_array == as_string, (
        f"两种写法 token 数不一致：{as_string} vs {as_array}"
    )
