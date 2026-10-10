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
    工具选择完全匹配率  实际调用的工具集合 == 期望集合
    工具召回率          仅在有期望工具的样本上算，|交集| / |期望|
    工具精确率          仅在实际调用了工具的样本上算，|交集| / |实际|
    负例抑制率          期望集合为空时，确实一个工具都没调的占比（专治「凡事都调工具」）
    工具调用成功率      tool_result 事件里 status == success 的占比（status 是 langchain 标准字段）
    端到端通过率        选择正确 + expect_contains 满足 + 有回答文本 + 没抛异常
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402  （必须在 sys.path 调整之后导入）

EVAL_DIR = ROOT / "eval"
DEFAULT_CASES = EVAL_DIR / "cases.jsonl"
DEFAULT_BASELINE_DIR = EVAL_DIR / "baselines"

# 只统计这些工具名，把流式分片可能产生的半截名字挡在指标之外。
KNOWN_TOOLS = {tool.name for tool in agent_core.tools}
# 这些 key 名里只出现名字、不出现值，用来确认评测环境装没装齐。
KEY_NAMES = ["DEEPSEEK_API_KEY", "AMAP_API_KEY", "TAVILY_API_KEY", "GLM_API_KEY"]


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
        contains = case.get("expect_contains")
        if contains is not None and (
            not isinstance(contains, list) or not all(isinstance(x, str) for x in contains)
        ):
            problems.append(f"{case_id}: expect_contains 必须是字符串列表")
    return problems


# ---------------------------------------------------------------- 单例跑一次

def _read_usage(thread_id: str) -> dict:
    """从 checkpoint 里读真实 token 用量。

    model_node 走的是 .invoke()（不是 .stream()），所以落库的 AIMessage 带着
    usage_metadata，读一次状态就能拿到确切 token 数——不需要额外发请求，
    也不用给 agent_core 加回调。必须在删 thread 之前调用。
    """
    empty = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_calls": 0}
    try:
        messages = agent_core.graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        ).values.get("messages", [])
    except Exception:  # noqa: BLE001 — 读不到用量不该让用例算失败
        return empty

    prompt = completion = calls = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            continue
        calls += 1
        prompt += usage.get("input_tokens", 0) or 0
        completion += usage.get("output_tokens", 0) or 0
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
        "model_calls": calls,
    }


def run_case(case: dict, repeat_index: int, run_slug: str) -> dict:
    """在一个全新会话里跑一条用例，返回该样本的记录。

    每条用例都用独立的 thread_id，跑完立刻删掉 checkpoint——
    评测不能污染 data/agent_checkpoints.sqlite，也不能让上一条用例的历史
    影响下一条（否则「多工具」用例会因为前文而白拿分）。
    """
    thread_id = f"eval-{run_slug}-{case['id']}-{repeat_index}-{uuid.uuid4().hex[:6]}"
    started = time.perf_counter()
    ttft_ms: float | None = None
    tool_call_events: list[str] = []
    tool_results: list[dict] = []
    answer_parts: list[str] = []
    error: str | None = None
    usage: dict = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_calls": 0}

    try:
        for event in agent_core.stream_agent("eval-user", thread_id, case["query"]):
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
        usage = _read_usage(thread_id)
    except Exception as exc:  # noqa: BLE001 — 单条用例崩溃不该中断整轮评测
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            agent_core.delete_checkpoint_thread(thread_id)
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
    expected_set, called_set = set(expected), set(called)
    intersection = expected_set & called_set

    exact_match = expected_set == called_set
    recall = len(intersection) / len(expected_set) if expected_set else None
    precision = len(intersection) / len(called_set) if called_set else None

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
        "expected_tools": expected,
        "called_tools": called,
        "unrecognized_tool_names": unrecognized,
        "exact_match": exact_match,
        "recall": _round(recall),
        "precision": _round(precision),
        "contains_ok": contains_ok,
        "e2e_ok": bool(
            exact_match
            and contains_ok is not False
            and answer.strip()
            and error is None
        ),
        "tool_call_events": len(tool_call_events),
        "tool_result_events": len(tool_results),
        "tool_results": tool_results,
        "tool_success": success_statuses,
        "usage": usage,
        "ttft_ms": _round(ttft_ms, 1),
        "total_ms": _round(total_ms, 1),
        "answer_chars": len(answer),
        "answer": answer[:600],
        "error": error,
    }


# ---------------------------------------------------------------- 聚合

def aggregate(records: list[dict], cases: list[dict], config: dict) -> dict:
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
        "工具选择完全匹配率": _round(_rate([r["exact_match"] for r in records])),
        "工具召回率": _round(_mean([r["recall"] for r in positive if r["recall"] is not None])),
        "工具精确率": _round(_mean([r["precision"] for r in with_tools if r["precision"] is not None])),
        "负例抑制率": _round(_rate([not r["called_tools"] for r in negative])),
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

    by_category: dict[str, dict] = {}
    # 累加先放在 _tools 里，最后才换算成「平均工具数」——避免同一个 key
    # 一会儿是 list、一会儿是数字，读的时候要回头看类型。
    for record in records:
        bucket = by_category.setdefault(
            record["category"], {"样本数": 0, "通过数": 0, "完全匹配数": 0, "_tools": []}
        )
        bucket["样本数"] += 1
        bucket["通过数"] += 1 if record["e2e_ok"] else 0
        bucket["完全匹配数"] += 1 if record["exact_match"] else 0
        bucket["_tools"].append(len(record["called_tools"]))
    for bucket in by_category.values():
        bucket["完全匹配率"] = _round(bucket["完全匹配数"] / bucket["样本数"])
        bucket["端到端通过率"] = _round(bucket["通过数"] / bucket["样本数"])
        bucket["平均工具数"] = _round(_mean(bucket.pop("_tools")))

    return {
        "metrics": metrics,
        "by_category": by_category,
        "config": config,
        "failed_case_ids": sorted({r["case_id"] for r in records if not r["e2e_ok"]}),
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
    print(f"  {'分类':<18} {'样本':>4} {'完全匹配率':>10} {'端到端通过率':>12} {'平均工具数':>10}")
    for category, bucket in summary["by_category"].items():
        print(
            f"  {category:<18} {bucket['样本数']:>4} "
            f"{_fmt(bucket['完全匹配率']):>10} {_fmt(bucket['端到端通过率']):>12} "
            f"{_fmt(bucket['平均工具数']):>10}"
        )

    failures = [r for r in records if not r["e2e_ok"]]
    _line(f"未通过的样本（{len(failures)}/{len(records)}）")
    if not failures:
        print("  无")
    for record in failures:
        detail = []
        if record["error"]:
            detail.append(f"异常={record['error']}")
        if record["expected_tools"] != record["called_tools"]:
            detail.append(f"期望={record['expected_tools']} 实际={record['called_tools']}")
        if record["contains_ok"] is False:
            detail.append("缺少期望关键词")
        if not record["answer"].strip():
            detail.append("回答为空")
        print(f"  [{record['case_id']}] {record['query']}")
        print(f"      {'；'.join(detail) or '未分类原因'}")

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

    old_categories = previous.get("by_category", {})
    if old_categories:
        _line("分类完全匹配率变化")
        for category, bucket in current["by_category"].items():
            old_bucket = old_categories.get(category)
            if not old_bucket:
                continue
            old_rate = old_bucket.get("完全匹配率")
            new_rate = bucket.get("完全匹配率")
            if old_rate is None or new_rate is None:
                continue
            delta = new_rate - old_rate
            marker = "+" if delta > 0 else ""
            print(f"  {category:<18} {_fmt(old_rate):>12} {_fmt(new_rate):>12} {marker + _fmt(delta):>12}")


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="跑金标集，产出 Agent 量化指标")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="金标集路径（jsonl）")
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR, help="基线输出目录")
    parser.add_argument("--tag", default=None, help="基线名，默认用时间戳")
    parser.add_argument("--repeat", type=int, default=1, help="每个用例重复次数（默认 1）")
    parser.add_argument("--concurrency", type=int, default=1, help="并发数；>1 时共用同一个 sqlite checkpointer，仅供加速用")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个用例（冒烟）")
    parser.add_argument("--compare", type=Path, default=None, help="与指定基线对比")
    parser.add_argument("--dry-run", action="store_true", help="只校验用例，不调模型")
    parser.add_argument("--verbose", action="store_true", help="打印逐条明细")
    parser.add_argument("--no-save", action="store_true", help="不写基线文件")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 终端不支持就算了，不影响评测
            pass

    if not args.cases.exists():
        raise SystemExit(f"找不到金标集：{args.cases}")
    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("金标集是空的。")

    problems = validate_cases(cases)
    if problems:
        _line(f"金标集有问题（{len(problems)} 条）")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    env = environment()
    _line("环境")
    print(f"  用例数          {len(cases)}")
    print(f"  模型            {env['provider']} / {env['model']} (temperature={env['temperature']})")
    if env["title_model"]:
        print(f"  标题模型        {env['title_model']}（评测不走 service.py，不会调用它）")
    print(f"  已配置的密钥    {[k for k, v in env['keys_present'].items() if v]}")
    missing = [k for k, v in env['keys_present'].items() if not v]
    if missing:
        print(f"  缺失的密钥      {missing}  → 相关用例会拿到「服务未配置」，指标会偏低")

    # 固定开销：每次模型调用都要重发的工具 schema。这是纯本地计算，不花钱。
    schemas = []
    for tool in agent_core.tools:
        schema = getattr(tool, "tool_call_schema", None)
        if schema is not None:
            schemas.append(schema.model_json_schema())
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
        return 0

    tag = args.tag or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_slug = uuid.uuid4().hex[:6]
    config = {
        "tag": tag,
        "repeat": args.repeat,
        "concurrency": args.concurrency,
        "cases_file": str(args.cases.relative_to(ROOT)) if args.cases.is_relative_to(ROOT) else str(args.cases),
    }

    tasks = [(case, index) for index in range(args.repeat) for case in cases]
    _line(f"开始评测：{len(tasks)} 个样本（{len(cases)} 用例 × {args.repeat} 次）")
    print("  跑真实模型，请稍候……")

    records: list[dict] = []
    wall_started = time.perf_counter()
    if args.concurrency > 1:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(run_case, case, index, run_slug) for case, index in tasks]
            for done, future in enumerate(futures, start=1):
                record = future.result()
                records.append(record)
                status = "OK " if record["e2e_ok"] else "FAIL"
                print(f"  [{done:>3}/{len(tasks)}] {status} {record['case_id']}")
    else:
        for done, (case, index) in enumerate(tasks, start=1):
            record = run_case(case, index, run_slug)
            records.append(record)
            status = "OK " if record["e2e_ok"] else "FAIL"
            print(f"  [{done:>3}/{len(tasks)}] {status} {record['case_id']}")

    wall_ms = (time.perf_counter() - wall_started) * 1000
    records.sort(key=lambda r: (r["case_id"], r["repeat"]))
    summary = aggregate(records, cases, config)
    summary.update({
        "tag": tag,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "environment": env,
        "wall_ms": _round(wall_ms, 1),
        "records": records,
    })

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

    return 0 if summary["metrics"]["端到端通过率"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
