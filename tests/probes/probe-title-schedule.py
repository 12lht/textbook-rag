"""探针：起标题的节奏（第几轮重起一次）。

纯计算，不调模型、不读 checkpoint——所以跑得极快，改一次就能跑一次。
它守的是一句业务规则：「第 1 轮起一次，之后每 10 轮重起一次，第 50 轮为止」。

这句话以前散在条件表达式里（`turn_number <= AUTO_TITLE_TURNS`，而且那个常量
的含义还是「每轮都起」），现在收进了 `service.should_generate_title`，
这里就把它的边界一条条钉住。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("no_proxy", "*")

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

sys.path.insert(0, str(REPO_ROOT))

import service  # noqa: E402

EXPECTED_TRIGGERS = [1, 11, 21, 31, 41]
failures = 0


def check(condition: bool, label: str, extra: str = "") -> None:
    global failures
    if condition:
        print(f"  [ok]   {label}")
    else:
        print(f"  [FAIL] {label}{(' —— ' + extra) if extra else ''}")
        failures += 1


DATA_DIR = REPO_ROOT / "data"
def snapshot() -> dict[str, int]:
    return {p.name: p.stat().st_mtime_ns for p in DATA_DIR.glob("*") if p.is_file()}


before = snapshot()

print("触发点")
triggers = [t for t in range(1, 1001) if service.should_generate_title(t)]
check(
    triggers == EXPECTED_TRIGGERS,
    "1000 轮里的触发点正好是 1/11/21/31/41",
    f"实际 {triggers}",
)
check(len(triggers) == 5, "一个会话最多重起 5 次标题", f"{len(triggers)} 次")

print("\n逐轮核对 1..60")
wrong = [
    turn
    for turn in range(1, 61)
    if service.should_generate_title(turn) != (turn in EXPECTED_TRIGGERS)
]
check(not wrong, "1..60 轮逐轮与预期一致", f"不一致的轮次 {wrong}")

print("\n边界")
check(service.should_generate_title(1) is True, "第 1 轮要起（第一轮先起一次）")
check(service.should_generate_title(2) is False, "第 2 轮不起")
check(service.should_generate_title(10) is False, "第 10 轮不起")
check(service.should_generate_title(11) is True, "第 11 轮重起")
check(service.should_generate_title(41) is True, "第 41 轮重起（最后一次）")
check(service.should_generate_title(42) is False, "第 42 轮不起")
check(service.should_generate_title(50) is False, "第 50 轮不起")
check(service.should_generate_title(51) is False, "第 51 轮起不再改标题")
check(service.should_generate_title(500) is False, "第 500 轮不起")
check(service.should_generate_title(0) is False, "轮号 0 不起（轮号从 1 开始）")
check(service.should_generate_title(-3) is False, "负数轮号不起")

print("\n常量一致性")
check(service.TITLE_INTERVAL == 10, "间隔是 10 轮", f"{service.TITLE_INTERVAL}")
check(service.TITLE_LAST_TURN == 50, "上限是第 50 轮", f"{service.TITLE_LAST_TURN}")
check(
    not hasattr(service, "AUTO_TITLE_TURNS"),
    "旧的 AUTO_TITLE_TURNS 已经删掉（它是「前 10 轮每轮都起」，语义不同，留着会被误用）",
)

after = snapshot()
check(before == after, "没有往 data/ 里写任何文件", f"{set(after) ^ set(before)}")

print(f"\n失败项：{failures}")
raise SystemExit(1 if failures else 0)
