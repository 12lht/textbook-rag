"""验证 tool_call 事件重复上报是否修好。

场景 A（对照组）：只有 1 个工具调用。修改前实测是 2 个 tool_call 事件，现在应为 1。
场景 B（最狠的情况）：一批里调两次「同名」工具，验证去重键能把它们区分开，
                     而不是把第二次误当成重复抹掉。
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core

SCENARIOS = [
    ("A 单工具", "现在几点了？"),
    ("B 同批同名双工具", "分别告诉我北京和上海现在的天气，方便我对比。"),
]


def drain(thread_id: str, query: str) -> list[dict]:
    events = list(agent_core.stream_agent("probe-user", thread_id, query))
    agent_core.delete_checkpoint_thread(thread_id)
    return events


all_ok = True
for label, query in SCENARIOS:
    thread_id = f"probe-tc-{label.split()[0]}"
    events = drain(thread_id, query)
    calls = [e["tool"] for e in events if e["type"] == "tool_call"]
    results = [e["tool"] for e in events if e["type"] == "tool_result"]

    print(f"\n=== {label} ===")
    print(f"查询：{query}")
    types = [e["type"] for e in events if e["type"] != "assistant_delta"]
    print(f"非 delta 事件序列：{types}")
    print(f"tool_call  事件 {len(calls)} 个：{calls}")
    print(f"tool_result 事件 {len(results)} 个：{results}")

    ok = len(calls) == len(results)
    print(f"调用数与执行数相等：{'是' if ok else '否  ← 仍有重复/漏报'}")
    all_ok = all_ok and ok

    if label.startswith("B"):
        # 场景 B 的关键：两次同名调用都得留下，不能被去重键吞掉
        same_name_twice = Counter(calls).most_common(1)[0][1] >= 2 if calls else False
        print(f"同名工具被报了 2 次（未被误去重）：{'是' if same_name_twice else '否  ← 位置兜底键失效'}")
        all_ok = all_ok and same_name_twice

print(f"\n全部通过：{all_ok}")
