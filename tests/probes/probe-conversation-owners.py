"""会话归属（conversations.py）的探针。

全部操作都打在**临时目录**里：先把模块里的三个路径换成 tmp，
之后任何调用都碰不到 `data/`。最后再验一次真实目录没被动过——
之前踩过「探针静默写生产库」的坑（见 MEMORY.md），所以这条检查留在这。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import conversations as C  # noqa: E402
import users  # noqa: E402

PASS = 0
FAIL = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"    ✓ {label}  {detail}")
    else:
        FAIL += 1
        print(f"    ✗ {label}  {detail}")


def header(text: str) -> None:
    print(f"\n== {text} ==")


# --------------------------------------------------------------------------
# 真实数据目录的快照：跑完要核对没变
# --------------------------------------------------------------------------
REAL_DIR = ROOT / "data"
REAL_FILES = ["conversation_summaries.json", "conversation_renames.json",
              "conversation_owners.json"]


def real_snapshot() -> dict:
    return {
        name: (REAL_DIR / name).stat().st_mtime_ns if (REAL_DIR / name).exists() else None
        for name in REAL_FILES
    }


before_real = real_snapshot()

TMP = Path(tempfile.mkdtemp(prefix="conv-owners-"))
C.SUMMARY_PATH = TMP / "conversation_summaries.json"
C.RENAME_PATH = TMP / "conversation_renames.json"
C.OWNER_PATH = TMP / "conversation_owners.json"

GUEST = users.GUEST_USER_ID


# --------------------------------------------------------------------------
header("1. 老会话没有归属记录 → 落到默认账号")
# --------------------------------------------------------------------------
# 手工造出「本次改造之前」的状态：只有标题，没有归属。
C.save_conversation_summaries({"old-1": "老会话一", "old-2": "老会话二"})
print("     归属文件存在吗:", C.OWNER_PATH.exists())

check(C.owner_of("old-1") == GUEST, "owner_of 兜底到默认账号", repr(C.owner_of("old-1")))
check(C.ids_of_user(GUEST) == {"old-1", "old-2"},
      "默认账号能看到全部老会话", sorted(C.ids_of_user(GUEST)))
check(C.ids_of_user("u-nobody") == set(),
      "别的账号看不到老会话", sorted(C.ids_of_user("u-nobody")))
check(not C.OWNER_PATH.exists(),
      "只是读，不会顺手把归属文件写出来（避免读操作带写副作用）")

# --------------------------------------------------------------------------
header("2. 新建会话记下归属")
# --------------------------------------------------------------------------
t_a = C.create_conversation("u-alice")
t_b = C.create_conversation("u-bob")
print("     alice:", t_a, " bob:", t_b)
print("     归属文件:", json.loads(C.OWNER_PATH.read_text("utf-8")))

check(C.owner_of(t_a) == "u-alice", "alice 的会话归属对")
check(C.owner_of(t_b) == "u-bob", "bob 的会话归属对")
check(t_a in C.ids_of_user("u-alice") and t_b not in C.ids_of_user("u-alice"),
      "alice 的列表只含自己的")
check(t_b in C.ids_of_user("u-bob") and t_a not in C.ids_of_user("u-bob"),
      "bob 的列表只含自己的")
check(C.ids_of_user(GUEST) == {"old-1", "old-2"},
      "新会话不会跑到默认账号名下", sorted(C.ids_of_user(GUEST)))
check(not (C.ids_of_user("u-alice") & C.ids_of_user("u-bob")),
      "两个账号的交集为空")

# 老会话 + 新会话同时存在，各自归各自
check(len(C.ids_of_user(GUEST)) + len(C.ids_of_user("u-alice")) + len(C.ids_of_user("u-bob"))
      == len(C.load_conversation_summaries()),
      "三个账号的会话加起来 = 总数（不重不漏）")

# --------------------------------------------------------------------------
header("3. belongs_to：存在性 + 归属一起判")
# --------------------------------------------------------------------------
check(C.belongs_to(t_a, "u-alice") is True, "自己的会话 → True")
check(C.belongs_to(t_a, "u-bob") is False, "别人的会话 → False")
check(C.belongs_to("t-not-exist", "u-alice") is False, "不存在的会话 → False")
check(C.belongs_to(t_a, GUEST) is False, "登录用户的会话，默认账号拿不到")
check(C.belongs_to("old-1", GUEST) is True, "老会话对默认账号可见")
check(C.belongs_to("t-not-exist", GUEST) is False,
      "不存在的会话即使是默认账号也 False（存在性先判）")

# --------------------------------------------------------------------------
header("4. 删除会话：三份一起清")
# --------------------------------------------------------------------------
C.rename_conversation(t_a, "改过名的会话")
check(C.is_renamed(t_a) is True, "重命名标记已写入")
check(C.drop_conversation(t_a) is True, "第一次删除返回 True")
check(t_a not in C.load_conversation_summaries(), "标题已删")
check(C.is_renamed(t_a) is False, "重命名标记已删")
check(t_a not in json.loads(C.OWNER_PATH.read_text("utf-8")), "归属记录已删")
check(C.drop_conversation(t_a) is False, "第二次删除返回 False（幂等）")
check(C.owner_of(t_a) == GUEST, "删掉之后 owner_of 兜底（不再指向 alice）")
check(C.belongs_to(t_a, "u-alice") is False, "删掉之后 alice 也拿不到了")

# --------------------------------------------------------------------------
header("5. 归属文件坏掉 / 值为空时不许崩")
# --------------------------------------------------------------------------
C.OWNER_PATH.write_text("{ 这不是 json", encoding="utf-8")
check(C.owner_of(t_b) == GUEST, "损坏的归属文件 → 当没有记录（不抛异常）")
check(C.ids_of_user(GUEST) == {"old-1", "old-2", t_b},
      "损坏时全部会话退到默认账号", sorted(C.ids_of_user(GUEST)))

C.save_conversation_owners({t_b: ""})
check(C.owner_of(t_b) == GUEST, "归属值为空字符串 → 兜底到默认账号")

C.save_conversation_owners({"whatever": "u-alice"})
check(C.ids_of_user("u-alice") == set(),
      "只存在于归属文件、会话表里没有的记录被滤掉", sorted(C.ids_of_user("u-alice")))

# --------------------------------------------------------------------------
header("6. 真实 data/ 目录没有被碰到")
# --------------------------------------------------------------------------
after_real = real_snapshot()
check(before_real == after_real, "三个会话元数据文件的 mtime 未变", str(after_real))
check(C.OWNER_PATH != REAL_DIR / "conversation_owners.json",
      "模块路径确实被换到临时目录了")

print(f"\n结果: {PASS}/{PASS + FAIL} 通过")
sys.exit(0 if FAIL == 0 else 1)
