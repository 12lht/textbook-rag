"""bench 主入口：跑场景、聚合、出报告。

用法（解释器固定用 agent 环境）：

    python tests/bench/bench.py --suite smoke
    python tests/bench/bench.py --suite full --repeat 3

产出两份文件（tests/bench/runs/<时间戳>.json / .md）并把 Markdown 摘要打到 stdout。

**会真实花 API 配额**：smoke ≈ 9 次模型请求，full ≈ 每个场景轮数 × repeat。
不碰生产会话列表（每个临时会话跑完立即删除），但会占用 DeepSeek 的前缀缓存——
跑完 bench 后的正常使用，命中率反而会短暂变好，这不是 bug。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 为了 import 同目录的 metrics/runner

import agent_core  # noqa: E402
import metrics  # noqa: E402
import runner as runner_mod  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent
RUNS_DIR = BENCH_DIR / "runs"

SUITES: dict[str, list[str]] = {
    "smoke": ["chat-1turn", "tool-time", "no-reasoning"],
    "full": [
        "chat-1turn",
        "chat-5turn",
        "tool-time",
        "tool-weather",
        "rag-textbook",
        "no-reasoning",
    ],
}


def _fmt_int(value: float | int) -> str:
    return f"{int(value):,}"


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}" if value >= 100 else f"{value:,.1f}"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(report: dict) -> str:
    """报告只保留决策需要的列：成本四项、时延两项、工具两项。其余进 JSON。"""
    lines: list[str] = []
    meta = report["meta"]
    lines.append(f"# 系统 core 指标基准（{meta['created_at']}）")
    lines.append("")
    lines.append(
        f"provider=`{meta['provider']}` model=`{meta['model']}` "
        f"suite=`{meta['suite']}` repeat={meta['repeat']} "
        f"耗时 {_fmt_ms(meta['wall_ms'])}ms"
    )
    lines.append("")

    lines.append("## 总表（cold：每场景第 1 次；warm：之后各次）")
    lines.append("")
    lines.append(
        "| 场景 | 档 | 轮 | input | 命中率 | output(思考) | tok/轮 "
        "| 模型调用/轮 | 工具/轮 | TTFT P50 | TTFT P95 | 总时延 P50 | 总时延 P95 "
        "| 工具ms | ms/out-tok | 成本当量/轮 |"
    )
    lines.append("|" + "---|" * 16)
    for item in report["scenarios"]:
        for label, key in (("cold", "cold"), ("warm", "warm")):
            row = metrics.summary_row(item["scenario"], item["cold_warm"][key])
            if row["turns"] == 0:
                continue
            lines.append(
                f"| {row['name']} | {label} | {row['turns']} "
                f"| {_fmt_int(row['input_tokens'])} | {_fmt_pct(row['cache_hit_rate'])} "
                f"| {_fmt_int(row['output_tokens'])} ({_fmt_int(row['reasoning_tokens'])}) "
                f"| {_fmt_int(row['tokens_per_turn'])} "
                f"| {row['model_calls_per_turn']:.1f} | {row['tool_calls_per_turn']:.1f} "
                f"| {_fmt_ms(row['ttft_p50_ms'])} | {_fmt_ms(row['ttft_p95_ms'])} "
                f"| {_fmt_ms(row['total_p50_ms'])} | {_fmt_ms(row['total_p95_ms'])} "
                f"| {_fmt_ms(row['tool_ms_mean'])} | {_fmt_ms(row['ms_per_output_token'])} "
                f"| {_fmt_int(row['cost_units_per_turn'])} |"
            )
    lines.append("")

    problems = [
        (item["scenario"], item["errors"], item["missing_tools"])
        for item in report["scenarios"]
        if item["errors"] or item["missing_tools"]
    ]
    if problems:
        lines.append("## 问题")
        lines.append("")
        for name, errors, missing in problems:
            if errors:
                lines.append(f"- **{name}** 出错 {len(errors)} 轮：{errors[0]}")
            if missing:
                lines.append(f"- **{name}** 预期工具没调到：{', '.join(missing)}")
        lines.append("")

    lines.append("## 口径备注")
    lines.append("")
    lines.append("- 命中率 = cache_read / input，来自 provider 返回的 usage，不是本地估算。")
    lines.append("- output 含思维链 token（括号里单列），思考 token 也计费但用户不可见。")
    lines.append("- TTFT = 发出请求到第一个可见字；工具 ms 是 tool_call→tool_result 的时间差。")
    lines.append(
        "- 成本当量 = 命中×1 + 未命中×4 + 输出×16（DeepSeek 定价比例，见 metrics.COST_WEIGHTS），"
        "表里是按轮均值，cold/warm 轮数不同不能直接比总量。"
    )
    lines.append("- 标题模型的 token 不在统计内（独立调用、不上 checkpoint），其时间含在 TTFT 里。")
    return "\n".join(lines) + "\n"


async def main_async(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=sorted(SUITES), default="smoke")
    parser.add_argument("--scenarios", help="逗号分隔的场景 id，覆盖 --suite")
    parser.add_argument("--repeat", type=int, default=2, help="每场景重复次数（≥2 才有 warm 档）")
    parser.add_argument("--out", default=None, help="输出目录，默认 tests/bench/runs/")
    args = parser.parse_args(argv)

    ids = (
        [s.strip() for s in args.scenarios.split(",") if s.strip()]
        if args.scenarios
        else SUITES[args.suite]
    )
    scenarios = {
        s.id: s for s in runner_mod.load_scenarios(BENCH_DIR / "scenarios.jsonl")
    }
    missing = [i for i in ids if i not in scenarios]
    if missing:
        print(f"场景不存在：{missing}", file=sys.stderr)
        return 2

    wall_started = time.perf_counter()

    # MCP 预热单独计时：这是一次性的进程冷启动成本，不该摊进任何一个场景的 TTFT。
    warm_start = time.perf_counter()
    await agent_core.load_tools()
    warmup_ms = (time.perf_counter() - warm_start) * 1000
    print(f"MCP 预热 {_fmt_ms(warmup_ms)}ms（不计入场景）", flush=True)

    items = []
    for scenario_id in ids:
        scenario = scenarios[scenario_id]
        print(f"→ {scenario_id}（{scenario.title}）…", flush=True)
        result = await runner_mod.run_scenario(scenario, repeat=args.repeat)
        items.append(result)
        overall = result["overall"]
        print(
            f"  input={overall['input_tokens']} 命中率={overall['cache_hit_rate'] * 100:.0f}% "
            f"TTFT P50={overall['ttft_p50_ms']}ms 总 P50={overall['total_p50_ms']}ms",
            flush=True,
        )

    wall_ms = (time.perf_counter() - wall_started) * 1000
    provider = __import__("os").getenv("MODEL_PROVIDER", "deepseek")
    report = {
        "meta": {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "suite": args.suite if not args.scenarios else "custom",
            "repeat": args.repeat,
            "provider": provider,
            "model": getattr(agent_core.model, "model_name", "?"),
            "warmup_ms": round(warmup_ms, 1),
            "wall_ms": round(wall_ms, 1),
        },
        "scenarios": items,
    }

    out_dir = Path(args.out) if args.out else RUNS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    markdown = render_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"已写入 {json_path} 和 {md_path}")
    await agent_core.close_mcp()
    return 0


def main() -> int:
    return asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
