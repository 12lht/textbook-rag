"""指标口径：只有算术，没有网络、没有图、没有 IO。

放在单独文件里是为了让每个数字都能被单测钉住——bench 本身要花钱、要等模型，
不能靠「跑一遍看输出」来验证算法对不对。

指标分三层，名字都带后缀区分：

    采集（turn 级）    一次用户提问产出的原始数字，直接从 usage / 事件时间戳取。
    派生（turn 级）    采集层算出来的比率，如 cache_hit_rate。
    聚合（scenario 级） 多次重复、多轮对话压成 P50 / P95 / 均值。

**为什么成本用「权重当量」而不是人民币**：单价会变、模型会换，写死金额的报告过几天
就是错的。命中缓存省的钱本质是「这段 input 便宜了几倍」，所以只报相对权重，
要换算成钱时乘上当时的单价即可（见 COST_WEIGHTS 的注释）。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

# 相对权重：以「命中缓存的 input」为 1。
# 比例取自 DeepSeek 官方定价（缓存命中 0.5 / 未命中 2 / 输出 8 元每百万 token），
# 只保留比例关系，不写死货币。换 provider 时改这三个数，历史报告仍然可比。
COST_WEIGHTS = {"cache_hit": 1.0, "input_miss": 4.0, "output": 16.0}

# usage_metadata 里出现过的字段名，集中在这里，换 provider 只改这一处。
# DeepSeek 走 langchain 的适配层，命中量落在 input_token_details.cache_read；
# 原始字段 prompt_cache_hit_tokens 也保留一份做交叉校验。
_FIELD_INPUT = "input_tokens"
_FIELD_OUTPUT = "output_tokens"
_FIELD_REASONING = "output_token_details"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_of_message(message: Any) -> dict[str, int]:
    """从一条 AIMessage 的 usage_metadata 抽出四个数。

    缺字段一律当 0，不抛异常：不是每个 provider 都回传缓存字段，bench 不能因为
    某个字段没实现就整体崩掉——那样会把「这个 provider 不支持缓存」误报成「测试失败」。
    """
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    reasoning = (usage.get(_FIELD_REASONING) or {}).get("reasoning")

    # reasoning 字段在某些 provider 里叫 reasoning_tokens、位置也不同，两处都试。
    if reasoning is None:
        raw = getattr(message, "response_metadata", None) or {}
        token_usage = raw.get("token_usage") or {}
        reasoning = (token_usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        )

    return {
        "input_tokens": _int(usage.get(_FIELD_INPUT)),
        "output_tokens": _int(usage.get(_FIELD_OUTPUT)),
        "reasoning_tokens": _int(reasoning),
        "cache_read_tokens": _int(details.get("cache_read")),
    }


def usage_of_messages(messages: Iterable[Any]) -> dict[str, int]:
    """把一轮里所有模型调用的 usage 相加。

    一轮可能有多次模型调用（每次工具返回后再问一遍），token 必须累加——
    只取最后一次会低估成本，而工具轮恰好是 token 最贵的地方。
    """
    total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "model_calls": 0,
    }
    for message in messages:
        single = usage_of_message(message)
        if not single["input_tokens"] and not single["output_tokens"]:
            continue  # 没带 usage 的消息（历史消息、被裁掉的部分）不算调用次数
        for key, value in single.items():
            total[key] += value
        total["model_calls"] += 1
    return total


def derive(usage: dict[str, int]) -> dict[str, float]:
    """采集层 → 派生比率。分母为 0 时返回 0.0，不返回 NaN。

    NaN 会污染下游所有统计（sum / 排序 / JSON 都不认它），而「没有 input」
    在这个场景里就是「这一轮没有意义」，报 0 让人看得懂。
    """
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_tokens", 0)

    hit_rate = cache_read / input_tokens if input_tokens else 0.0
    miss = max(0, input_tokens - cache_read)
    cost = (
        cache_read * COST_WEIGHTS["cache_hit"]
        + miss * COST_WEIGHTS["input_miss"]
        + output_tokens * COST_WEIGHTS["output"]
    )
    return {
        "cache_hit_rate": hit_rate,
        "cache_miss_tokens": miss,
        "cost_units": round(cost, 1),
        "reasoning_ratio": (
            usage.get("reasoning_tokens", 0) / output_tokens if output_tokens else 0.0
        ),
    }


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值百分位。样本不足时退化成最值，不报错。

    为什么要 P95 而不是只看均值：时延是长尾分布，均值会被少数快请求拉低，
    用户投诉的永远是那 5% 的慢请求。
    """
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return 0.0
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return clean[int(position)]
    return clean[low] + (clean[high] - clean[low]) * (position - low)


def _mean(values: Sequence[float]) -> float:
    clean = [float(v) for v in values if v is not None]
    return sum(clean) / len(clean) if clean else 0.0


def _round(value: float, digits: int = 1) -> float:
    return round(float(value), digits)


def aggregate_turns(turns: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把一次场景的所有轮压成一行结果。

    所有 token 类是**求和**再算比率（不是比率的平均）——比率平均会让小轮次
    和大轮次权重相同，算出来的命中率偏乐观。
    """
    if not turns:
        return {"turns": 0}

    usage = {
        "input_tokens": sum(t["input_tokens"] for t in turns),
        "output_tokens": sum(t["output_tokens"] for t in turns),
        "reasoning_tokens": sum(t["reasoning_tokens"] for t in turns),
        "cache_read_tokens": sum(t["cache_read_tokens"] for t in turns),
        "model_calls": sum(t["model_calls"] for t in turns),
        "tool_calls": sum(t["tool_calls"] for t in turns),
    }
    derived = derive(usage)
    ttfts = [t["ttft_ms"] for t in turns if t.get("ttft_ms") is not None]
    totals = [t["total_ms"] for t in turns]

    return {
        "turns": len(turns),
        **usage,
        **derived,
        "tokens_per_turn": _round(
            (usage["input_tokens"] + usage["output_tokens"]) / len(turns)
        ),
        "input_per_turn": _round(usage["input_tokens"] / len(turns)),
        "output_per_turn": _round(usage["output_tokens"] / len(turns)),
        "model_calls_per_turn": _round(usage["model_calls"] / len(turns), 2),
        "tool_calls_per_turn": _round(usage["tool_calls"] / len(turns), 2),
        "ttft_p50_ms": _round(percentile(ttfts, 0.5)),
        "ttft_p95_ms": _round(percentile(ttfts, 0.95)),
        "total_p50_ms": _round(percentile(totals, 0.5)),
        "total_p95_ms": _round(percentile(totals, 0.95)),
        "tool_ms_mean": _round(_mean([t.get("tool_ms", 0) for t in turns])),
        "title_ms_mean": _round(
            _mean([t["title_ms"] for t in turns if t.get("title_ms")])
        ),
        "ms_per_output_token": _round(
            _mean(
                [
                    (t["total_ms"] - (t["ttft_ms"] or 0)) / max(1, t["output_tokens"])
                    for t in turns
                ]
            )
        ),
    }


def split_cold_warm(
    runs: Sequence[Sequence[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """第 1 次运行 vs 之后各次，分开统计。

    **这是缓存指标的关键切分**：provider 的前缀缓存是按「请求前缀」命中的，
    第一次问某个场景必然全 miss（前缀刚被写进去），第二次起才可能命中。
    混在一起算会得到一个既不代表冷启动、也不代表稳态的中间值。
    """
    cold = list(runs[0]) if runs else []
    warm = [turn for run in runs[1:] for turn in run]
    return {
        "cold": aggregate_turns(cold),
        "warm": aggregate_turns(warm),
    }


def summary_row(name: str, stats: dict[str, Any]) -> dict[str, Any]:
    """统一成一个固定列集的字典，供 JSON 落盘和 Markdown 表格共用。

    cost_units_per_turn：cold 档往往只有 1 轮、warm 有多轮，总量直接比会误导，
    表格里展示的是按轮归一后的值；总量仍在 JSON 里（key 为 cost_units）。
    """
    turns = stats.get("turns", 0) or 1
    return {
        "name": name,
        "turns": stats.get("turns", 0),
        "input_tokens": stats.get("input_tokens", 0),
        "cache_read_tokens": stats.get("cache_read_tokens", 0),
        "cache_hit_rate": stats.get("cache_hit_rate", 0.0),
        "output_tokens": stats.get("output_tokens", 0),
        "reasoning_tokens": stats.get("reasoning_tokens", 0),
        "tokens_per_turn": stats.get("tokens_per_turn", 0),
        "model_calls_per_turn": stats.get("model_calls_per_turn", 0),
        "tool_calls_per_turn": stats.get("tool_calls_per_turn", 0),
        "ttft_p50_ms": stats.get("ttft_p50_ms", 0),
        "ttft_p95_ms": stats.get("ttft_p95_ms", 0),
        "total_p50_ms": stats.get("total_p50_ms", 0),
        "total_p95_ms": stats.get("total_p95_ms", 0),
        "tool_ms_mean": stats.get("tool_ms_mean", 0),
        "ms_per_output_token": stats.get("ms_per_output_token", 0),
        "cost_units": stats.get("cost_units", 0),
        "cost_units_per_turn": round(stats.get("cost_units", 0) / turns, 1),
    }
