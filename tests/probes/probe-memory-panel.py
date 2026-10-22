"""探针：界面上那块「AI 记住了什么」——列表 / 删除 / 修改。

对应 PROJECT-PLAN 的 E6（记得的都能看见，可删、可改）。测的是存储层那几个
函数，不碰模型、不碰生产库（临时目录）。

关键验收（E6 原文）：「删掉某条后，它真的不再提」——所以除了三个函数本身，
最后还要验一次「删完之后 recall 读出来的东西里没有它」。

**`id` 必须和内容无关**：改措辞后编号不能变，否则前端「改完马上删」会 404。
这条单独有一节测（`=== 改完编号不变 ===`），是那个 bug 的回归。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:/agent")
sys.path.insert(0, str(ROOT))
import memories  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-memory-panel-"))
memories.DATA_DIR = tmp
memories.DB_PATH = tmp / "memories.sqlite3"
memories.LEGACY_JSON_PATH = tmp / "memories.json"

USER = "u-panel-test"

passed = failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
    print(f"  {'OK  ' if condition else 'FAIL'} {label}")


def main() -> None:
    print("=== 写入三条 ===")
    memories.remember(
        USER,
        [
            {"text": "先给结论再展开", "kind": "偏好"},
            {"text": "叫我涛哥", "kind": "身份"},
            {"text": "在复习移动通信第3章", "kind": "计划"},
        ],
        source_thread="t-1",
    )
    rows = memories.memories_of(USER)
    print(f"  条数 {len(rows)}，字段 {sorted(rows[0].keys())}")
    check("memories_of 带出 id", "id" in rows[0])
    check("不对外暴露 fingerprint", "fingerprint" not in rows[0])
    check("三条都在", len(rows) == 3)

    print("\n=== 改一条（改措辞，kind 不变）===")
    target = next(r for r in rows if r["text"] == "叫我涛哥")
    outcome = memories.update_text(USER, target["id"], "称呼我为涛哥")
    check("返回 ok", outcome == "ok")
    after = memories.memories_of(USER)
    texts = [r["text"] for r in after]
    check("新措辞在", "称呼我为涛哥" in texts)
    check("旧措辞没了", "叫我涛哥" not in texts)
    check("条数仍为 3（没有变成新增）", len(after) == 3)
    kept = next(r for r in after if r["text"] == "称呼我为涛哥")
    check("kind 沿用原来的身份", kept["kind"] == "身份")
    check("created_at 没被改成现在", kept["created_at"] == target["created_at"])

    print("\n=== 改完编号不变（回归）===")
    check("改完 id 和改前一模一样", kept["id"] == target["id"])
    check(
        "拿旧 id 还能删得掉",
        memories.forget_by_id(USER, target["id"]),
    )
    memories.remember(USER, [{"text": "称呼我为涛哥", "kind": "身份"}])
    after = memories.memories_of(USER)
    check("补回来之后仍是 3 条", len(after) == 3)
    kept = next(r for r in after if r["text"] == "称呼我为涛哥")

    print("\n=== 改成和已有条目重复 ===")
    outcome = memories.update_text(USER, kept["id"], "先给结论再展开")
    check("返回 clash 而不是静默合并", outcome == "clash")
    check("条数没变", len(memories.memories_of(USER)) == 3)

    print("\n=== 删一条 ===")
    plan = next(r for r in after if r["kind"] == "计划")
    check("forget_by_id 返回 True", memories.forget_by_id(USER, plan["id"]))
    remaining = memories.memories_of(USER)
    check("条数 3 -> 2", len(remaining) == 2)
    check("被删的那条读不出来了", plan["text"] not in [r["text"] for r in remaining])

    print("\n=== 删不存在的 ===")
    check("返回 False（路由据此报 404）", not memories.forget_by_id(USER, "根本不存在的条目"))
    check("改不存在的返回 missing", memories.update_text(USER, "不存在", "随便") == "missing")

    print("\n=== E6 验收：删掉之后真的不再提 ===")
    recalled = [r["text"] for r in memories.memories_of(USER)]
    joined = " / ".join(recalled)
    check(f"recall 读到的内容里没有被删的那条（{joined}）", plan["text"] not in joined)

    print("\n=== 别人的记忆碰不到 ===")
    check(
        "换个 user_id 删不掉",
        not memories.forget_by_id("u-someone-else", remaining[0]["id"]),
    )
    check("原用户那条还在", len(memories.memories_of(USER)) == 2)

    print(f"\n合计 {passed} 项通过，{failed} 项失败")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
