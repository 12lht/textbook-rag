"""评测闭环：用金标集驱动真实 LangGraph，产出可对比的量化指标。

为什么直接调 agent_core 而不是 service：
    service.chat_events() 在前面还会调标题模型。那是应用层的功能，但会把一次 GLM
    调用算进 TTFT，让「首字延迟」这个指标失真。评测要测的是 Agent 本身，所以这里
    只走 agent_core.stream_agent()，消费的仍然是前端同款事件流。

用法：
    python eval/run.py --dry-run          # 只校验 cases.jsonl（查工具名、查重），不调模型
    python eval/run.py --limit 3          # 只跑前 3 个用例，冒烟用
    python eval/run.py                    # 跑全量，写入 eval/baselines/<时间戳>.json
    python eval/run.py --tag baseline     # 指定基线名
    python eval/run.py --repeat 3         # 每个用例重复 3 次，降低单次采样的抖动
    python eval/run.py --compare eval/baselines/baseline.json    # 和旧基线对比

指标口径（都是「样本」级别，repeat>1 时样本数 = 用例数 × repeat）：

    工具选择合规率      必备工具全部调到，且没有任何「不被允许」的额外调用。
                        这是主指标 —— 判据是语义合规，不是集合相等。
    严格集合相等率      实际集合 == 必备集合。保留作参考，但它会把合法的多工具组合
                        判成错误（原因见下面「为什么不用严格集合相等」）。
    工具召回率          仅在有必备工具的样本上算，|必备∩实际| / |必备|
    工具精确率          仅在实际调用了工具的样本上算，|必备∩实际| / |实际|
    负例抑制率          必备集合为空时，确实一个工具都没调的占比（专治「凡事都调工具」）
    配额违规调用数      多调了「会消耗外部配额」的工具的次数。这是真正的浪费，也是
                        「工具选择合规率」判负的唯一原因。
    冗余调用数          多调了「被允许且零配额」的工具的次数。这是成本不是错误，
                        单独报出来（实测每次约 +100 token，不增加模型往返）。
    工具调用成功率      tool_result 事件里 status == success 的占比（langchain 标准字段）
    端到端通过率        选择合规 + expect_contains 满足 + 有回答文本 + 没抛异常

为什么不用严格集合相等（2026-10-10 修正）：
    最初用「实际调用集合 == 期望集合」。跑下来天气类只有 0.5，看着像模型在乱调工具。
    但把 6 次运行的记录逐条摊开才发现，额外调用是**按措辞触发的稳定策略**：问
    「上海现在的气温」时 6/6 都不调 get_current_time，问「明天适合去广州出差吗」时
    6/6 都调。相对时间（今天 / 明天 / 未来三天）本来就需要一个锚点，这不是错误。
    再实测代价：get_current_time 是纯本地函数、零外部配额，而且和天气工具同批并行
    下发，模型往返 +0.00 次、token +19~165。所以它既不错也不贵。
    于是改成三档：必备 / 允许（逐例在 cases.jsonl 的 allow_tools 里声明）/ 违规。
    判负只看「必备没调全」和「调了不被允许的东西」；而额外调用算不算浪费，按它烧
    不烧外部配额来定 —— get_current_time 免费所以放行，get_user_location / web_search /
    web_extract 走的是高德或 Tavily，有配额，不能白送。

录制回放（让改动前后真正可比）：
    外部 API 返回的内容每次都不一样，搜索正文长度实测波动 1.1~2.5 倍，比被测的代码
    改动大得多。所以对比前先把工具输出冻结下来：

    # 1. 录一轮（真实调用外部 API，把返回值存进 cassette）
    python eval/run.py --cassette-mode record --cassette frozen-v1 --tag rec

    # 2. 之后所有对比都走 replay，两次运行拿到的工具输出逐字一致
    python eval/run.py --cassette-mode replay --cassette frozen-v1 --tag a
    python eval/run.py --cassette-mode replay --cassette frozen-v1 --tag b --compare ...

    replay 未命中时会照常真实调用并计入「外部调用未命中」。这个数不为 0 就说明
    有些工具入参是新的，那些样本的前后对比仍然不干净——要么再跑一轮 record 把
    覆盖面加宽（record 只增不改，可以反复跑），要么把未命中的样本挑出来单独看。
"""
from __future__ import annotations

import argparse
import importlib.metadata
import hashlib
import json
import math
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402  （必须在 sys.path 调整之后导入）
import cassette  # noqa: E402

EVAL_DIR = ROOT / "eval"
DEFAULT_CASES = EVAL_DIR / "cases.jsonl"
DEFAULT_BASELINE_DIR = EVAL_DIR / "baselines"
DEFAULT_CASSETTE_DIR = EVAL_DIR / "cassettes"

# 只统计这些工具名，把流式分片可能产生的半截名字挡在指标之外。
# 改造后工具是从 MCP server 动态加载的，所以这个集合要在跑之前异步取回来
# （由 _load_known_tools() 填），不能像改造前那样在 import 时静态算出来。
KNOWN_TOOLS: set[str] = set()
# 会消耗外部配额的工具体。高德的天气和 Tavily 的搜索/提取都是计量的。
# 这份分类放在评测侧而不是 tools.py：它回答的是「多调一次算不算浪费」，
# 是评测口径问题。
#
# 注意 `get_user_location` **不在**这里了：它改成读客户端传来的位置之后，
# 工具本身不再发任何外部请求（坐标解析在 service 层做一次，还带缓存），
# 所以它现在是零配额工具，和 `get_current_time` 同类。
METERED_TOOLS = {
    "get_weather_for_location",
    "web_search",
    "web_extract",
}
# cases.jsonl 没写 allow_tools 时的兜底：只放行零配额的本地工具。
DEFAULT_ALLOW_TOOLS = ["get_current_time"]
# 评测里模拟「客户端授权提供的位置」。走 Context 不进工具参数，所以不碰 cassette；
# 给一个固定值是为了让定位类用例可复现（原来靠服务器 IP 定位，每次跑可能都不同）。
DEFAULT_EVAL_LOCATION = "北京市"
# 这些 key 名里只出现名字、不出现值，用来确认评测环境装没装齐。
KEY_NAMES = ["DEEPSEEK_API_KEY", "AMAP_API_KEY", "TAVILY_API_KEY", "GLM_API_KEY"]


async def _load_known_tools() -> set[str]:
    """从 MCP 取回工具名，顺带把「改造后工具接口是否与改造前一致」暴露出来。"""
    global KNOWN_TOOLS
    KNOWN_TOOLS = {tool.name for tool in await agent_core.load_tools()}
    return KNOWN_TOOLS


# ---------------------------------------------------------------- 小工具

def _percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数。样本量为 1 时直接返回该值。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rate(flags: list[bool]) -> float | None:
    return (sum(1 for f in flags if f) / len(flags)) if flags else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _normalize(text: str) -> str:
    """去掉所有空白再小写，让 expect_contains 不受排版影响。"""
    return "".join(text.split()).lower()


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.{digits}f}"


def _line(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title) * 2)


# ---------------------------------------------------------------- 用例读取与校验

def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            cases.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
    return cases


def validate_cases(cases: list[dict]) -> list[str]:
    """把金标集自身的问题挑出来。用例写错比模型答错更值得先修。"""
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id")
        if not case_id:
            problems.append("有用例缺少 id")
            continue
        if case_id in seen:
            problems.append(f"{case_id}: id 重复")
        seen.add(case_id)
        if not case.get("query"):
            problems.append(f"{case_id}: 缺少 query")
        expected = case.get("expect_tools")
        if not isinstance(expected, list):
            problems.append(f"{case_id}: expect_tools 必须是列表")
        else:
            unknown = [name for name in expected if name not in KNOWN_TOOLS]
            if unknown:
                problems.append(f"{case_id}: 期望了不存在的工具 {unknown}")

        allowed = case.get("allow_tools", DEFAULT_ALLOW_TOOLS)
        if not isinstance(allowed, list):
            problems.append(f"{case_id}: allow_tools 必须是列表")
        elif not expected:
            # 负例：一个工具都不该调，写任何 allow 都是自相矛盾。
            if allowed:
                problems.append(f"{case_id}: 负例的 allow_tools 必须为空（任何调用都算违规）")
        else:
            unknown = [name for name in allowed if name not in KNOWN_TOOLS]
            if unknown:
                problems.append(f"{case_id}: allow_tools 里有不存在的工具 {unknown}")
            # allow_tools 的语义是「**额外**允许」，必备工具已经在 expect_tools 里，
            # 再写一遍说明写的人对语义理解错了，值得直接拦下来。
            duplicated = sorted(set(allowed) & set(expected))
            if duplicated:
                problems.append(
                    f"{case_id}: allow_tools 只写额外允许的工具，{duplicated} 已经在 expect_tools 里"
                )
        contains = case.get("expect_contains")
        if contains is not None and (
            not isinstance(contains, list) or not all(isinstance(x, str) for x in contains)
        ):
            problems.append(f"{case_id}: expect_contains 必须是字符串列表")
    return problems


# ---------------------------------------------------------------- 三档判定

def judge_selection(expected: list[str], allowed: list[str], called: list[str]) -> dict:
    """按「必备 / 额外允许 / 违规」三档判定一次工具选择。

    抽成纯函数是为了能对历史基线重算：旧的基线记录里只有 expected_tools 和
    called_tools（那时还没有 allow_tools 字段），拿这里的规则 + cases.jsonl 里
    现补的 allow_tools 就能复现新口径，不必重跑模型花钱。

    注意 allowed 的语义是「**额外**允许」，不含必备 —— 必备已经在 expected 里了，
    所以判合规时要把两者并起来才是一次完整的「允许集合」。
    """
    expected_set, called_set = set(expected), set(called)

    missing = sorted(expected_set - called_set)        # 必备没调全 —— 真错
    extra = sorted(called_set - expected_set)          # 必备之外多调的
    violations = sorted(set(extra) - set(allowed))     # 多调了没被允许的 —— 真错
    redundant = sorted(set(extra) & set(allowed))      # 允许的额外调用 —— 成本不是错

    return {
        "allow_tools": list(allowed),
        "missing_tools": missing,
        "violation_tools": violations,
        "redundant_tools": redundant,
        "metered_violations": [n for n in violations if n in METERED_TOOLS],
        "selection_ok": not missing and not violations,
        "exact_match": expected_set == called_set,
    }


# ---------------------------------------------------------------- 单例跑一次

async def _read_usage(thread_id: str) -> tuple[dict, str | None]:
    """从 checkpoint 里读真实 token 用量和工具输出指纹。

    model_node 走的是 .ainvoke()（不是流式），所以落库的 AIMessage 带着
    usage_metadata，读一次状态就能拿到确切 token 数——不需要额外发请求，
    也不用给 agent_core 加回调。必须在删 thread 之前调用。

    指纹 = 按顺序把所有 ToolMessage 的内容拼起来做 sha256。它是「录制回放到底
    有没有生效」的判据：replay 模式下两次运行的指纹逐字相同，就说明模型拿到的
    外部内容完全一致，剩下的差异只可能来自代码或模型本身，不再被外部内容污染。
    """
    empty = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_calls": 0}
    try:
        graph = await agent_core.get_graph()
        messages = (await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )).values.get("messages", [])
    except Exception:  # noqa: BLE001 — 读不到用量不该让用例算失败
        return empty, None

    prompt = completion = calls = 0
    outputs: list[str] = []
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if usage:
            calls += 1
            prompt += usage.get("input_tokens", 0) or 0
            completion += usage.get("output_tokens", 0) or 0
        if getattr(message, "type", "") == "tool" or type(message).__name__ == "ToolMessage":
            outputs.append(_content_text(getattr(message, "content", "")))

    digest = None
    if outputs:
        # 排序后再拼：指纹要反映「模型从工具那儿看到了什么内容」，
        # 不该因为工具调用顺序变了就报不一致。顺序差异由 called_tools 反映。
        joined = "\x1e".join(sorted(outputs))
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
        "model_calls": calls,
    }, digest


def _content_text(content: object) -> str:
    """把消息内容压成纯文本。MCP 工具返回的是 content-block 列表，
    内层 text 还是转义过的字符串，所以不能直接 str()。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", block)))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


async def run_case(case: dict, repeat_index: int, run_slug: str, location: str | None = None) -> dict:
    """在一个全新会话里跑一条用例，返回该样本的记录。

    每条用例都用独立的 thread_id，跑完立刻删掉 checkpoint——
    评测不能污染 data/agent_checkpoints.sqlite，也不能让上一条用例的历史
    影响下一条（否则「多工具」用例会因为前文而白拿分）。

    `location` 是模拟「客户端授权提供的位置」。它走的是 `Context` 而不是工具参数，
    所以**完全不碰 cassette**——也不计入工具输出指纹。固定一个值就能让定位类用例
    可复现，比原来靠服务器 IP 定位（每次跑都可能不同）反而更干净。
    """
    thread_id = f"eval-{run_slug}-{case['id']}-{repeat_index}-{uuid.uuid4().hex[:6]}"
    started = time.perf_counter()
    ttft_ms: float | None = None
    tool_call_events: list[str] = []
    tool_results: list[dict] = []
    answer_parts: list[str] = []
    error: str | None = None
    usage: dict = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_calls": 0}
    tool_output_digest: str | None = None

    try:
        async for event in agent_core.stream_agent(
            "eval-user", thread_id, case["query"], location=location
        ):
            kind = event.get("type")
            if kind == "assistant_delta":
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - started) * 1000
                answer_parts.append(str(event.get("content", "")))
            elif kind == "tool_call":
                tool_call_events.append(str(event.get("tool", "")))
            elif kind == "tool_result":
                tool_results.append({
                    "tool": str(event.get("tool", "")),
                    "status": str(event.get("status", "success")),
                })
        usage, tool_output_digest = await _read_usage(thread_id)
    except Exception as exc:  # noqa: BLE001 — 单条用例崩溃不该中断整轮评测
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await agent_core.delete_checkpoint_thread(thread_id)
        except Exception:  # noqa: BLE001 — 清理失败不值得让整轮评测失败
            pass

    total_ms = (time.perf_counter() - started) * 1000
    answer = "".join(answer_parts)

    raw_selected = [name for name in tool_call_events if name]
    executed = [result["tool"] for result in tool_results if result["tool"]]
    # 流式分片理论上可能在名字还没拼完时就报一个半截名字，统一用它和真实执行结果
    # 求并集、再按已知工具名过滤，既能补上 updates 通道漏掉的一次，也不会被半截名字污染。
    unrecognized = sorted({n for n in raw_selected + executed if n not in KNOWN_TOOLS})
    called = sorted({n for n in raw_selected + executed if n in KNOWN_TOOLS})

    expected = [name for name in case.get("expect_tools", [])]
    allowed = [name for name in case.get("allow_tools", DEFAULT_ALLOW_TOOLS if expected else [])]
    verdict = judge_selection(expected, allowed, called)

    expected_set, called_set = set(expected), set(called)
    recall = len(expected_set & called_set) / len(expected_set) if expected_set else None
    precision = (
        len(expected_set & called_set) / len(called_set) if called_set else None
    )

    contains = case.get("expect_contains")
    if contains:
        haystack = _normalize(answer)
        contains_ok: bool | None = all(_normalize(x) in haystack for x in contains)
    else:
        contains_ok = None

    success_statuses = [r["status"] == "success" for r in tool_results]

    return {
        "case_id": case["id"],
        "category": case.get("category", "未分类"),
        "repeat": repeat_index,
        "query": case["query"],
        "thread_id": thread_id,
        # 记下这一轮模拟的客户端位置，否则「定位类用例跑的是什么位置」事后无从考证
        "client_location": location,
        "expected_tools": expected,
        "called_tools": called,
        "unrecognized_tool_names": unrecognized,
        **verdict,
        "recall": _round(recall),
        "precision": _round(precision),
        "contains_ok": contains_ok,
        "e2e_ok": bool(
            verdict["selection_ok"]
            and contains_ok is not False
            and answer.strip()
            and error is None
        ),
        "tool_call_events": len(tool_call_events),
        "tool_result_events": len(tool_results),
        "tool_results": tool_results,
        "tool_output_digest": tool_output_digest,
        "tool_success": success_statuses,
        "usage": usage,
        "ttft_ms": _round(ttft_ms, 1),
        "total_ms": _round(total_ms, 1),
        "answer_chars": len(answer),
        "answer": answer[:600],
        "error": error,
    }


# ---------------------------------------------------------------- 聚合

def aggregate(records: list[dict], cases: list[dict], config: dict, cassette_stats: dict | None = None) -> dict:
    # 兼容 2026-10-10 之前跑的基线：那些记录里没有三档字段。按 cases.jsonl 里现补的
    # allow_tools 重算一遍，历史基线就能直接用新口径汇总，不必重跑模型花钱。
    allow_by_case = {
        case["id"]: case.get(
            "allow_tools", DEFAULT_ALLOW_TOOLS if case.get("expect_tools") else []
        )
        for case in cases
    }
    for record in records:
        if "selection_ok" in record:
            continue
        verdict = judge_selection(
            record.get("expected_tools", []),
            allow_by_case.get(record["case_id"], []),
            record.get("called_tools", []),
        )
        record.update(verdict)
        # e2e_ok 必须一起重算。旧记录里它是用严格口径算出来的，只补 selection_ok
        # 会让「端到端通过率 / 未通过样本」和新的「合规率」自相矛盾。
        record["e2e_ok"] = bool(
            verdict["selection_ok"]
            and record.get("contains_ok") is not False
            and str(record.get("answer", "")).strip()
            and record.get("error") is None
        )

    samples = len(records)
    positive = [r for r in records if r["expected_tools"]]
    negative = [r for r in records if not r["expected_tools"]]
    with_tools = [r for r in records if r["called_tools"]]

    statuses = [ok for r in records for ok in r["tool_success"]]
    ttfts = [r["ttft_ms"] for r in records if r["ttft_ms"] is not None]
    totals = [r["total_ms"] for r in records if r["total_ms"] is not None]
    total_tokens = sum(r["usage"]["total_tokens"] for r in records)

    # 工具调用事件数 vs 真实执行数。两者不等说明事件流重复上报了同一调用。
    duplicate_events = sum(
        max(0, r["tool_call_events"] - r["tool_result_events"]) for r in records
    )

    metrics = {
        "样本数": samples,
        "用例数": len(cases),
        "工具选择合规率": _round(_rate([r["selection_ok"] for r in records])),
        "严格集合相等率": _round(_rate([r["exact_match"] for r in records])),
        "工具召回率": _round(_mean([r["recall"] for r in positive if r["recall"] is not None])),
        "工具精确率": _round(_mean([r["precision"] for r in with_tools if r["precision"] is not None])),
        "负例抑制率": _round(_rate([not r["called_tools"] for r in negative])),
        "配额违规调用数": sum(len(r["metered_violations"]) for r in records),
        "冗余调用数": sum(len(r["redundant_tools"]) for r in records),
        "平均冗余调用数": _round(
            _mean([len(r["redundant_tools"]) for r in records])
        ),
        "工具调用成功率": _round(_rate(statuses)),
        "端到端通过率": _round(_rate([r["e2e_ok"] for r in records])),
        "平均调用工具数": _round(_mean([len(r["called_tools"]) for r in records])),
        "TTFT P50 (ms)": _round(_percentile(ttfts, 0.5), 1),
        "TTFT P95 (ms)": _round(_percentile(ttfts, 0.95), 1),
        "端到端 P50 (ms)": _round(_percentile(totals, 0.5), 1),
        "端到端 P95 (ms)": _round(_percentile(totals, 0.95), 1),
        "总 token 消耗": total_tokens,
        "平均每用例 token": _round(_mean([r["usage"]["total_tokens"] for r in records]), 1),
        "平均模型调用次数": _round(_mean([r["usage"]["model_calls"] for r in records])),
        "输入 token 占比": _round(
            sum(r["usage"]["input_tokens"] for r in records) / total_tokens
            if total_tokens
            else None
        ),
        "重复的 tool_call 事件数": duplicate_events,
    }

    if cassette_stats is not None:
        # 这三个是「本次运行」的增量，不是样本级指标。未命中不为 0 说明有工具的
        # 入参没被录过，那些样本的外部内容仍然不可控，对比时要把它们挑出来。
        metrics["外部调用录制"] = cassette_stats["records"]
        metrics["外部调用命中"] = cassette_stats["hits"]
        metrics["外部调用未命中"] = cassette_stats["misses"]

    by_category: dict[str, dict] = {}
    # 累加先放在 _tools 里，最后才换算成「平均工具数」——避免同一个 key
    # 一会儿是 list、一会儿是数字，读的时候要回头看类型。
    for record in records:
        bucket = by_category.setdefault(
            record["category"],
            {"样本数": 0, "通过数": 0, "合规数": 0, "完全匹配数": 0, "_tools": []},
        )
        bucket["样本数"] += 1
        bucket["通过数"] += 1 if record["e2e_ok"] else 0
        bucket["合规数"] += 1 if record["selection_ok"] else 0
        bucket["完全匹配数"] += 1 if record["exact_match"] else 0
        bucket["_tools"].append(len(record["called_tools"]))
    for bucket in by_category.values():
        bucket["完全匹配率"] = _round(bucket["完全匹配数"] / bucket["样本数"])
        bucket["合规率"] = _round(bucket["合规数"] / bucket["样本数"])
        bucket["端到端通过率"] = _round(bucket["通过数"] / bucket["样本数"])
        bucket["平均工具数"] = _round(_mean(bucket.pop("_tools")))

    return {
        "metrics": metrics,
        "by_category": by_category,
        "config": config,
        "failed_case_ids": sorted({r["case_id"] for r in records if not r["e2e_ok"]}),
        # 严格口径下不通过的用例。和上面的差集就是「被三档口径救回来」的那批，
        # 出对照表时用得上。
        "strict_failed_case_ids": sorted({r["case_id"] for r in records if not r["exact_match"]}),
        "unrecognized_tool_names": sorted({n for r in records for n in r["unrecognized_tool_names"]}),
        "errored_case_ids": sorted({r["case_id"] for r in records if r["error"]}),
    }


def environment() -> dict:
    import os

    def _version(package: str) -> str | None:
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return None

    # 直接问真实模型对象，别自己拼字符串——有 GLM_API_KEY 时标题模型也叫 glm，
    # 按环境变量猜会报出一个评测里根本没用到的模型名。
    main_model = agent_core.model
    model_name = (
        getattr(main_model, "model_name", None)
        or getattr(main_model, "model", None)
        or type(main_model).__name__
    )

    return {
        "provider": os.getenv("MODEL_PROVIDER", "deepseek").lower(),
        "model": model_name,
        "temperature": getattr(main_model, "temperature", None),
        "title_model": (
            getattr(agent_core.title_model, "model_name", None)
            if agent_core.title_model is not None
            else None
        ),
        "keys_present": {name: bool(os.getenv(name)) for name in KEY_NAMES},
        "python": sys.version.split()[0],
        "langgraph": _version("langgraph"),
        "langchain": _version("langchain"),
    }


# ---------------------------------------------------------------- 输出

def print_report(summary: dict, records: list[dict], verbose: bool) -> None:
    metrics = summary["metrics"]
    _line("总体指标")
    for name, value in metrics.items():
        print(f"  {name:<18} {_fmt(value)}")

    _line("分类表现")
    print(
        f"  {'分类':<18} {'样本':>4} {'合规率':>8} {'严格相等':>8} "
        f"{'端到端通过率':>12} {'平均工具数':>10}"
    )
    for category, bucket in summary["by_category"].items():
        print(
            f"  {category:<18} {bucket['样本数']:>4} "
            f"{_fmt(bucket['合规率']):>8} {_fmt(bucket['完全匹配率']):>8} "
            f"{_fmt(bucket['端到端通过率']):>12} {_fmt(bucket['平均工具数']):>10}"
        )

    failures = [r for r in records if not r["e2e_ok"]]
    _line(f"未通过的样本（{len(failures)}/{len(records)}）")
    if not failures:
        print("  无")
    for record in failures:
        detail = []
        if record["error"]:
            detail.append(f"异常={record['error']}")
        if record["missing_tools"]:
            detail.append(f"缺少必备工具 {record['missing_tools']}")
        if record["violation_tools"]:
            detail.append(f"调了没被允许的工具 {record['violation_tools']}")
        if record["contains_ok"] is False:
            detail.append("缺少期望关键词")
        if not record["answer"].strip():
            detail.append("回答为空")
        print(f"  [{record['case_id']}] {record['query']}")
        print(f"      {'；'.join(detail) or '未分类原因'}")

    # 「允许但非必备」的额外调用单独摊开。这不是错误，是成本：要么多一次模型往返，
    # 要么多烧一次外部配额（配额那部分已经计进「配额违规调用数」，这里只看被放行的）。
    redundant = [r for r in records if r["redundant_tools"]]
    if redundant:
        _line(f"被允许的冗余调用（{sum(len(r['redundant_tools']) for r in redundant)} 次，成本不是错误）")
        for record in redundant:
            print(f"  [{record['case_id']}] 多调 {record['redundant_tools']}")

    if summary["unrecognized_tool_names"]:
        _line("识别不了的工具名（可能是流式半截名）")
        print(f"  {summary['unrecognized_tool_names']}")

    if verbose:
        _line("逐条明细")
        for record in records:
            status = "OK " if record["e2e_ok"] else "FAIL"
            print(
                f"  {status} [{record['case_id']}] 期望={record['expected_tools']} "
                f"实际={record['called_tools']} ttft={_fmt(record['ttft_ms'], 1)}ms"
            )
            print(f"       {record['answer'][:160]}")


def print_compare(current: dict, previous: dict, previous_label: str) -> None:
    """和旧基线逐项对比。任务 2（记忆摘要）之后靠这张表报 before/after。"""
    _line(f"对比基线：{previous_label}")
    print(f"  {'指标':<18} {'旧':>12} {'新':>12} {'变化':>12}")
    old_metrics = previous.get("metrics", {})
    new_metrics = current.get("metrics", {})
    for name, new_value in new_metrics.items():
        if name not in old_metrics:
            continue
        old_value = old_metrics[name]
        if not isinstance(old_value, (int, float)) or not isinstance(new_value, (int, float)):
            continue
        delta = new_value - old_value
        marker = "+" if delta > 0 else ""
        print(f"  {name:<18} {_fmt(old_value):>12} {_fmt(new_value):>12} {marker + _fmt(delta):>12}")

    if "工具选择合规率" not in old_metrics:
        print("\n  提示：对比基线是 2026-10-10 之前的旧口径（没有「工具选择合规率」这类指标），")
        print("        所以上面只有两边都有的指标能对比。旧记录可以按新规则重算，不用重跑模型：")
        print("        aggregate(旧基线的 records, cases, config) 会用 cases.jsonl 里的")
        print("        allow_tools 现算三档判定，并同步修正 e2e_ok。")

    old_categories = previous.get("by_category", {})
    if old_categories:
        _line("分类通过率变化（左：合规率  右：严格集合相等率）")
        print(f"  {'分类':<18} {'旧合规':>8} {'新合规':>8} {'旧严格':>8} {'新严格':>8}")
        for category, bucket in current["by_category"].items():
            old_bucket = old_categories.get(category)
            if not old_bucket:
                continue
            print(
                f"  {category:<18} {_fmt(old_bucket.get('合规率')):>8} "
                f"{_fmt(bucket.get('合规率')):>8} "
                f"{_fmt(old_bucket.get('完全匹配率')):>8} "
                f"{_fmt(bucket.get('完全匹配率')):>8}"
            )

    # 「严格口径不通过、三档口径通过」的样本：这些就是被口径修正救回来的。
    # 如果新基线是旧基线用严格口径跑出来的，这里能直接看出差异来自哪几条。
    old_strict_failed = set(previous.get("strict_failed_case_ids", []))
    new_failed = set(current.get("failed_case_ids", []))
    if old_strict_failed:
        rescued = sorted(old_strict_failed - new_failed)
        if rescued:
            _line("被三档口径救回来（严格口径判错、按配额口径其实合规）")
            for case_id in rescued:
                print(f"  {case_id}")

    # 工具输出指纹：录制回放模式下，同一个入参应该拿到逐字一致的返回值。这里要把
    # 两种「不一致」分开，否则会把模型行为差异误判成冻结失败：
    #   工具调用序列也变了      → 模型这一轮少调/多调了工具，是行为差异，属于要测的信号
    #   调用序列一样但内容不同   → 外部内容没被冻结住，这次对比不干净，必须报出来
    old_records = {(r["case_id"], r.get("repeat", 0)): r for r in previous.get("records", [])}
    new_records = {(r["case_id"], r.get("repeat", 0)): r for r in current.get("records", [])}
    common = sorted(set(old_records) & set(new_records))
    compared = same = 0
    frozen_ok_failed: list[str] = []
    behavior_changed: list[str] = []
    for key in common:
        old_record, new_record = old_records[key], new_records[key]
        old_digest = old_record.get("tool_output_digest")
        new_digest = new_record.get("tool_output_digest")
        if old_digest is None and new_digest is None:
            continue
        compared += 1
        if old_digest == new_digest:
            same += 1
            continue
        same_calls = (
            old_record.get("called_tools") == new_record.get("called_tools")
            and old_record.get("tool_result_events") == new_record.get("tool_result_events")
        )
        if same_calls:
            frozen_ok_failed.append(key[0])
        else:
            behavior_changed.append(key[0])
    if compared:
        _line("工具输出指纹（外部内容有没有被冻结住）")
        print(f"  逐字一致的样本 {same} / {compared}")
        if frozen_ok_failed:
            print(f"  冻结失效：{frozen_ok_failed}")
            print("  → 这些样本工具调用次数一样但内容不同，token 和时延的差异不能直接归给代码")
        else:
            print("  冻结失效：无（相同调用拿到的内容逐字一致）")
        if behavior_changed:
            print(f"  工具调用序列不同：{behavior_changed}")
            print("  → 模型这一轮多调/少调了工具，属于要测的行为差异，不是外部内容问题")


# ---------------------------------------------------------------- main

async def main() -> int:
    parser = argparse.ArgumentParser(description="跑金标集，产出 Agent 量化指标")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="金标集路径（jsonl）")
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR, help="基线输出目录")
    parser.add_argument("--tag", default=None, help="基线名，默认用时间戳")
    parser.add_argument("--repeat", type=int, default=1, help="每个用例重复次数（默认 1）")
    parser.add_argument("--concurrency", type=int, default=1, help="并发样本数（默认 1，串行；>1 时只重叠「等模型返回」的时间）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个用例（冒烟）")
    parser.add_argument("--category", default=None, help="只跑指定分类，逗号分隔（如 搜索,网页提取,天气）")
    parser.add_argument("--compare", type=Path, default=None, help="与指定基线对比")
    parser.add_argument("--dry-run", action="store_true", help="只校验用例，不调模型")
    parser.add_argument("--verbose", action="store_true", help="打印逐条明细")
    parser.add_argument("--no-save", action="store_true", help="不写基线文件")
    parser.add_argument(
        "--cassette-mode",
        choices=["off", "record", "replay"],
        default="off",
        help="工具录制回放：off 直连外部 API；record 真实调用并把返回值存进 cassette；"
             "replay 只读 cassette（未命中仍真实调用，并计入「外部调用未命中」）",
    )
    parser.add_argument("--cassette", default="default", help="cassette 名，对应 eval/cassettes/<名>.json")
    parser.add_argument("--cassette-dir", type=Path, default=DEFAULT_CASSETTE_DIR, help="cassette 目录")
    parser.add_argument(
        "--location",
        default=DEFAULT_EVAL_LOCATION,
        help=f"模拟客户端授权提供的位置，用于定位类用例（默认 {DEFAULT_EVAL_LOCATION}；传空字符串表示不提供）",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 终端不支持就算了，不影响评测
            pass

    # 必须在建立 MCP 会话之前设好：工具跑在子进程里，是靠继承父进程环境变量
    # 才知道要不要录制回放的。agent_core 里 MCP 客户端是延迟构造的（见 _build_mcp_client），
    # 就是为了让这个顺序成立。
    cassette_path = (args.cassette_dir / f"{args.cassette}.json").resolve()
    os.environ["TOOL_CASSETTE_MODE"] = args.cassette_mode
    os.environ["TOOL_CASSETTE_PATH"] = str(cassette_path)
    cassette_before = cassette.counters()
    if args.cassette_mode == "off":
        print("工具录制回放：off（直连外部 API，外部内容不可控，前后对比仅供参考）")
    else:
        print(
            f"工具录制回放：{args.cassette_mode}  {cassette_path.name}"
            f"（已有 {cassette.interaction_count()} 条记录）"
        )
        if args.cassette_mode == "replay" and not cassette_path.exists():
            print(f"  警告：cassette 不存在，本次会全部真实调用并计入未命中。先跑 record 生成：{cassette_path}")

    if not args.cases.exists():
        raise SystemExit(f"找不到金标集：{args.cases}")
    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    if args.category:
        # 录制回放只对外部内容敏感的用例有意义（搜索/提取/天气/定位）。
        # 只跑这几个分类，A/B 的 token 成本能砍掉一大半。
        wanted = {name.strip() for name in args.category.split(",") if name.strip()}
        available = sorted({case.get("category", "未分类") for case in load_cases(args.cases)})
        unknown = wanted - set(available)
        if unknown:
            raise SystemExit(f"未知分类 {sorted(unknown)}；可用分类：{available}")
        cases = [case for case in cases if case.get("category") in wanted]
    if not cases:
        raise SystemExit("金标集是空的（或分类筛选后为空）。")

    # 工具名要从 MCP server 动态取回来，validate_cases 才能校验 expect_tools。
    # 改造前是 import 时静态算的，现在必须先建立 MCP 会话。
    await _load_known_tools()

    problems = validate_cases(cases)
    if problems:
        _line(f"金标集有问题（{len(problems)} 条）")
        for problem in problems:
            print(f"  - {problem}")
        await agent_core.shutdown()
        return 2

    env = environment()
    _line("环境")
    print(f"  用例数          {len(cases)}")
    print(f"  模型            {env['provider']} / {env['model']} (temperature={env['temperature']})")
    if env["title_model"]:
        print(f"  标题模型        {env['title_model']}（评测不走 service.py，不会调用它）")
    print("  工具来源        MCP server ×4（stdio 子进程）+ 进程内 ×1（get_user_location）")
    print(
        "  录制回放        "
        + (
            "off（外部内容不可控）"
            if args.cassette_mode == "off"
            else f"{args.cassette_mode} / {cassette_path.name}"
        )
    )
    print(f"  已配置的密钥    {[k for k, v in env['keys_present'].items() if v]}")
    missing = [k for k, v in env['keys_present'].items() if not v]
    if missing:
        print(f"  缺失的密钥      {missing}  → 相关用例会拿到「服务未配置」，指标会偏低")

    # 固定开销：每次模型调用都要重发的工具 schema。这是纯本地计算，不花钱。
    schemas = []
    for tool in await agent_core.load_tools():
        schema = getattr(tool, "tool_call_schema", None)
        if schema is None:
            continue
        # 两种来源的形态不同：进程内 @tool 给的是 pydantic 模型（要调
        # model_json_schema()），MCP 适配器给的是已经是 dict 的 JSON Schema。
        schemas.append(schema if isinstance(schema, dict) else schema.model_json_schema())
    fixed_chars = len(json.dumps(schemas, ensure_ascii=False)) + len(agent_core.SYSTEM_PROMPT)
    print(f"  每次调用的固定开销  {fixed_chars} 字符（工具 schema + 系统提示，纯本地统计）")
    print("  token 用量          跑完从 checkpoint 读取真实值，不额外发请求")

    if args.dry_run:
        counts: dict[str, int] = {}
        for case in cases:
            counts[case.get("category", "未分类")] = counts.get(case.get("category", "未分类"), 0) + 1
        _line("按分类的用例数")
        for category, count in counts.items():
            print(f"  {category:<18} {count}")
        print("\n用例校验通过，dry-run 结束（没有调用模型）。")
        await agent_core.shutdown()
        return 0

    tag = args.tag or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_slug = uuid.uuid4().hex[:6]
    config = {
        "tag": tag,
        "repeat": args.repeat,
        "concurrency": args.concurrency,
        "cases_file": str(args.cases.relative_to(ROOT)) if args.cases.is_relative_to(ROOT) else str(args.cases),
        "category_filter": args.category,
        "cassette_mode": args.cassette_mode,
        "cassette": str(cassette_path) if args.cassette_mode != "off" else None,
        "client_location": args.location or None,
    }

    tasks = [(case, index) for index in range(args.repeat) for case in cases]
    _line(f"开始评测：{len(tasks)} 个样本（{len(cases)} 用例 × {args.repeat} 次）")
    print("  跑真实模型，请稍候……")

    records: list[dict] = []
    wall_started = time.perf_counter()
    if args.concurrency > 1:
        # 用 asyncio 并发。注意 MCP 会话和 checkpoint 连接都是进程内共享的单一资源，
        # aiosqlite 会把同一连接上的写操作排队，所以并发提高的是「等待模型返回」的
        # 重叠度，不会把 sqlite 写坏。给每个样本自己的 thread_id，互不干扰。
        semaphore = asyncio.Semaphore(args.concurrency)

        async def limited(case: dict, index: int) -> dict:
            async with semaphore:
                return await run_case(case, index, run_slug, args.location)

        pending = [asyncio.create_task(limited(case, index)) for case, index in tasks]
        for done, task in enumerate(asyncio.as_completed(pending), start=1):
            record = await task
            records.append(record)
            status = "OK " if record["e2e_ok"] else "FAIL"
            print(f"  [{done:>3}/{len(tasks)}] {status} {record['case_id']}")
    else:
        for done, (case, index) in enumerate(tasks, start=1):
            record = await run_case(case, index, run_slug, args.location)
            records.append(record)
            status = "OK " if record["e2e_ok"] else "FAIL"
            print(f"  [{done:>3}/{len(tasks)}] {status} {record['case_id']}")

    wall_ms = (time.perf_counter() - wall_started) * 1000
    records.sort(key=lambda r: (r["case_id"], r["repeat"]))

    # 工具子进程每次调用都会即时把计数写回 cassette 文件，所以这里读到的是准确值。
    cassette_after = cassette.counters()
    cassette_stats = {
        name: cassette_after[name] - cassette_before[name]
        for name in ("records", "hits", "misses")
    }

    summary = aggregate(records, cases, config, cassette_stats)
    summary.update({
        "tag": tag,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "environment": env,
        "wall_ms": _round(wall_ms, 1),
        "cassette": {
            "mode": args.cassette_mode,
            "path": str(cassette_path) if args.cassette_mode != "off" else None,
            "hits": cassette_stats["hits"],
            "misses": cassette_stats["misses"],
            "records": cassette_stats["records"],
            "total_interactions": cassette.interaction_count(),
        },
        "records": records,
    })
    if args.cassette_mode == "replay" and cassette_stats["misses"]:
        _line("录制回放未命中")
        print(
            f"  有 {cassette_stats['misses']} 次工具入参没被录过，这些样本的外部内容仍不可控。"
        )
        print(f"  再跑一轮 record 可以把覆盖面加宽（record 只增不改）：")
        print(f"    --cassette-mode record --cassette {args.cassette}")

    print_report(summary, records, args.verbose)

    if args.compare:
        if not args.compare.exists():
            print(f"\n对比基线不存在，跳过：{args.compare}")
        else:
            print_compare(summary, json.loads(args.compare.read_text(encoding="utf-8")), str(args.compare))

    if not args.no_save:
        args.baseline_dir.mkdir(parents=True, exist_ok=True)
        target = args.baseline_dir / f"{tag}.json"
        payload = json.dumps(summary, ensure_ascii=False, indent=2)
        target.write_text(payload, encoding="utf-8")
        (args.baseline_dir / "latest.json").write_text(payload, encoding="utf-8")
        _line("基线已保存")
        print(f"  {target}")
        print(f"  {args.baseline_dir / 'latest.json'}")

    # 收掉 MCP 子进程和 checkpoint 连接。不显式收的话，子进程还活着，
    # 解释器可能一直不退出（命令行看起来就像卡住了）。
    await agent_core.shutdown()

    return 0 if summary["metrics"]["端到端通过率"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
