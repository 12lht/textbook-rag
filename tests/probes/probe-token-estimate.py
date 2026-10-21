"""探针：标定 `_estimate_tokens` 的误差，并盯住「工具结果被转义」这个坑（真实调用，成本几分钱）。

裁剪阈值现在是按 token 算的，但 token 是**估**出来的——没装 tokenizer，用的是
DeepSeek 文档给的「1 汉字 ≈ 0.6 token、1 个英文字母 ≈ 0.3 token」。这支探针拿真实
`usage.input_tokens` 去对，回答三个问题：

1. 那段换算比例到底偏多少？中英混排、markdown、工具返回分别怎么样？
2. 请求里那些「不随对话增长」的开销（系统提示附加段 + 工具 schema）估得准不准？
3. **工具返回的 content-block 有没有被 `json.dumps` 转义成 `\\u5faa` 发出去？**
   （这一条是 2026-10-22 发现的真问题，`_flatten_for_model` 就是为它写的；
   下面第 0 节拿真实请求体断言，改坏了会立刻红。）

结论只用来决定估算系数要不要补；跑完请把数字抄到 `agent_core` 的注释里。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import agent_core

# 样本刻意覆盖四种形状：纯中文、纯英文、中英混排的 markdown（教材 chunk 的样子）、
# 工具返回的 content-block（content 是列表，不是字符串）。
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
    """走生产代码那条路：摊平工具结果 → 并系统消息。探针量的必须是真发出去的东西。"""
    state = {"messages": ([SystemMessage(content=system)] if system else []) + list(messages)}
    return agent_core._messages_for_model(state)


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return condition


async def measure(model, messages) -> int:
    """发一次真实请求，只取 input_tokens。`max_tokens=1` 让输出几乎不花钱。"""
    reply = await model.ainvoke(messages)
    usage = getattr(reply, "usage_metadata", None) or {}
    return int(usage.get("input_tokens") or 0)


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


async def main() -> int:
    ok = True
    # 关掉思考、输出卡到 1 个 token：只要 usage，不要内容。
    model = agent_core.build_model(temperature=0, max_tokens=1, disable_reasoning=True)

    print("== 0. 工具结果的 content-block 不能被转义成 \\u5faa 发出去 ==")
    raw = tool_turn(0)
    flattened = sent(raw)
    body = json.dumps(
        model._get_request_payload(flattened)["messages"], ensure_ascii=False
    )
    ok &= check("请求体里没有 \\u 转义", "\\u" not in body, body[:120] + "…")
    ok &= check("工具结果变成了纯字符串",
                isinstance(flattened[2].content, str), type(flattened[2].content).__name__)
    ok &= check("原来的 state 没被改（仍是 content-block）",
                isinstance(raw[2].content, list))

    print()
    print("== 1. 字符数 → token 的换算误差（单个用户消息）==")
    print(f"   {'样本':<18}{'估算':>8}{'实际':>8}{'偏差':>10}")
    # 容差按样本形状单独给，理由写在旁边——这里要的是「知道偏多少、往哪偏」，
    # 不是把所有样本调到一个数上（一个系数做不到，见下面的实测）。
    # 纯英文散文：真实分词四五个字符才一个 token，系数按文档取 0.3 必然偏高；
    #   偏高的方向安全（早裁一点），不值得为英文再分一套系数。
    # 公式密集的 markdown：`\log_2` `\frac{SNR}{N_t}` 这类 LaTeX 一段能切出十几个
    #   token，靠字符分类估不出来（要把标点权重提到 1.5 才凑得上，那样 JSON 就会
    #   高估三成）。这里是**已知低估**，不影响结论：真实会话里的大头是工具返回的
    #   JSON 和中文正文，见第 4 节。
    tolerances = {
        "纯英文 200 字符": (0.0, 0.55),
        "中英混排 markdown": (-0.35, 0.25),
    }
    for name, text in SAMPLES.items():
        payload = sent([HumanMessage(content=text)])
        got = await measure(model, payload)
        est = estimate([HumanMessage(content=text)])
        low, high = tolerances.get(name, (-0.25, 0.25))
        ratio = (est - got) / max(got, 1)
        print(f"   {name:<18}{est:>8}{got:>8}{ratio:>9.0%}")
        ok &= check(
            f"{name} 偏差在 {low:+.0%}~{high:+.0%} 内",
            low <= ratio <= high,
            f"est={est} actual={got}",
        )

    print()
    print("== 2. 一轮带检索的教材问答 ==")
    plain_turn = tool_turn(0)
    got = await measure(model, sent(plain_turn))
    est = estimate(plain_turn)
    print(f"   估算 {est} / 实际 {got}（偏差 {(est - got) / max(got, 1):+.0%}）")
    ok &= check("带工具调用的一轮偏差在 ±30% 内", abs(est - got) / max(got, 1) <= 0.30)

    print()
    print("== 3. 固定开销：系统提示附加段 + 工具 schema ==")
    system = agent_core._system_prompt(memory_enabled=True)
    tools = await agent_core.load_tools()
    bound = model.bind_tools(tools)
    plain_actual = await measure(model, sent([HumanMessage(content="你好")], system=system))
    bound_actual = await measure(bound, sent([HumanMessage(content="你好")], system=system))
    actual_schema = bound_actual - plain_actual
    est_schema = agent_core._tool_schema_tokens()
    print(f"   工具 {len(tools)} 个；系统提示 {len(system)} 字符")
    print(f"   工具 schema：估算 {est_schema} / 实测增量 {actual_schema}"
          f"（偏差 {(est_schema - actual_schema) / max(actual_schema, 1):+.0%}）")
    ok &= check("工具 schema 估算在 ±30% 内",
                abs(est_schema - actual_schema) / max(actual_schema, 1) <= 0.30)
    # 这支脚本没有注入记忆 / 检索后端，所以系统提示只有一句 `SYSTEM_PROMPT`，
    # 而 `_prompt_overhead_tokens` 刻意按「两段引导都在」的最坏情况算——
    # 在这里必然偏高，在生产里才是准的。打出来只为看清差了多少。
    print(f"   `_prompt_overhead_tokens()` = {agent_core._prompt_overhead_tokens()}"
          f"（本脚本没注入后端，引导段并未真的发出去，偏高是设计如此）")

    print()
    print("== 4. 一段典型教材问答历史（按裁剪阈值判断，估算要撑得住）==")
    history = []
    for index in range(8):
        history.extend(tool_turn(index))
    got = await measure(bound, sent(history, system=system))
    # 这次模型绑了工具，所以估算里必须把 schema 算进来，否则比的不是同一件事。
    est = estimate(history, system=system) + est_schema
    print(f"   {len(history)} 条消息：估算 {est} / 实际 {got}"
          f"（偏差 {(est - got) / max(got, 1):+.0%}）")
    ok &= check("多轮累计偏差仍在 ±25% 内", abs(est - got) / max(got, 1) <= 0.25)

    await agent_core.close_mcp()
    print()
    print("结果:", "全部通过" if ok else "有失败（要么修系数，要么接受更大误差）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
