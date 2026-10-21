"""探针：主模型的思维链到底藏在流式分片的哪里。

要回答三个问题，**不靠猜**：
1. `messages` 流模式下，分片里能不能逐字拿到 `reasoning_content`？（前端实时展示要用）
2. 把分片拼起来之后，`additional_kwargs["reasoning_content"]` 是完整的一段，
   还是只剩最后一片？（重开页面后从 checkpoint 补显示要用）
3. 思维链会不会混进 `content`？（混进去就会被当成正文渲染到气泡里，是要命的 bug）

第 3 点尤其要查：langchain-core 1.x 有「把推理塞进 content 块」的新形态
（`{"type": "reasoning", ...}`），如果这个模型走的是那条路，现有的
`assistant_delta` 分支就会把思维链当正文发出去。
"""
import asyncio
import os
import sys

os.environ.setdefault("no_proxy", "*")
sys.path.insert(0, r"D:\agent")

from langchain_core.messages import HumanMessage  # noqa: E402

import agent_core  # noqa: E402

QUESTION = "用一句话说明什么是可重构智能表面（RIS）。"


async def main() -> int:
    failures = 0
    model = agent_core.model
    print(f"provider = {os.getenv('MODEL_PROVIDER', 'deepseek')}")

    total = 0
    text_chunks = 0
    reasoning_chunks = 0
    agg = None
    content_block_types = set()
    piece_len = 0  # 逐片思维链长度之和

    # 一次请求里**同时**算逐片长度和聚合结果。分两次跑没意义：模型两次说的
    # 话本来就不一样长，比出来的差异分不清是「合并丢了内容」还是「模型换了说法」。
    async for chunk in model.astream([HumanMessage(content=QUESTION)]):
        total += 1
        raw = (chunk.additional_kwargs or {}).get("reasoning_content")
        if raw:
            reasoning_chunks += 1
            piece_len += len(raw)
        if chunk.content:
            text_chunks += 1
        # content 是字符串还是块列表？块里有没有 reasoning？
        if isinstance(chunk.content, list):
            for block in chunk.content:
                if isinstance(block, dict):
                    content_block_types.add(block.get("type"))
        agg = chunk if agg is None else agg + chunk

    print(f"分片总数            = {total}")
    print(f"带正文的分片        = {text_chunks}")
    print(f"带思维链的分片      = {reasoning_chunks}")
    print(f"content 块类型      = {sorted(content_block_types) or '（content 是纯字符串）'}")

    if reasoning_chunks == 0:
        print("[FAIL] 分片里拿不到 reasoning_content —— 前端做不了实时思维链")
        failures += 1
    else:
        print("[ok]   分片里能逐片拿到 reasoning_content")

    # ---- 聚合后的完整消息 ----
    full_reasoning = (agg.additional_kwargs or {}).get("reasoning_content") or ""
    content = agg.content if isinstance(agg.content, str) else str(agg.content)
    print(f"聚合后思维链长度    = {len(full_reasoning)} 字符")
    print(f"聚合后正文长度      = {len(content)} 字符")

    # 逐片长度之和 vs 聚合长度：相等说明 merge_dicts 是「相加」而不是「覆盖」。
    # 这两个数来自**同一次请求**，所以任何差异都只可能是合并导致的。
    print(f"逐片长度之和        = {piece_len} 字符")

    if not full_reasoning:
        print("[FAIL] 聚合后思维链丢了 —— 重开页面补不出思维链")
        failures += 1
    elif len(full_reasoning) != piece_len:
        print(f"[FAIL] 聚合长度 {len(full_reasoning)} ≠ 逐片之和 {piece_len}，合并丢内容了")
        failures += 1
    else:
        print("[ok]   聚合后思维链是完整的（与逐片之和逐字相等）")

    # ---- content 是否被污染 ----
    leaked = "reasoning" in content_block_types
    if leaked:
        print("[FAIL] 思维链混进了 content 块 —— 会被当正文渲染！")
        failures += 1
    else:
        print("[ok]   正文里没有混入思维链")

    print(f"\n思维链前 80 字：{full_reasoning[:80]!r}")
    print(f"\n失败项：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
