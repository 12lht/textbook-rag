"""会话归属（`conversations.py`）：老会话兜底、归属记录、删除、坏文件容错。

全部操作都打在临时目录里：三个路径换成 tmp，任何调用都碰不到 `data/`。
最后再验一次真实目录没被动过——之前踩过「探针静默写生产库」的坑。

原 `tests/probes/probe-conversation-owners.py`，2026-11-03 迁到 pytest。
原脚本靠顺序传递状态（第 4 段删掉的会话影响第 5 段），这里每个用例自己建前置状态。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import conversations as C
import users

GUEST = users.GUEST_USER_ID
REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DIR = REPO_ROOT / "data"
REAL_FILES = [
    "conversation_summaries.json",
    "conversation_renames.json",
    "conversation_owners.json",
]


def _real_snapshot() -> dict:
    return {
        name: (REAL_DIR / name).stat().st_mtime_ns if (REAL_DIR / name).exists() else None
        for name in REAL_FILES
    }


@pytest.fixture
def conv(tmp_path, monkeypatch):
    """把三个会话元数据文件指到 tmp，并造出「两条老会话 + 两个账号各一条新会话」。"""
    monkeypatch.setattr(C, "SUMMARY_PATH", tmp_path / "conversation_summaries.json")
    monkeypatch.setattr(C, "RENAME_PATH", tmp_path / "conversation_renames.json")
    monkeypatch.setattr(C, "OWNER_PATH", tmp_path / "conversation_owners.json")

    C.save_conversation_summaries({"old-1": "老会话一", "old-2": "老会话二"})
    return SimpleNamespace(
        a=C.create_conversation("u-alice"),
        b=C.create_conversation("u-bob"),
        tmp=tmp_path,
    )


def test_legacy_conversations_fall_back_to_guest(tmp_path, monkeypatch):
    """没有归属记录的老会话归默认账号，而且**只读不写**归属文件。"""
    monkeypatch.setattr(C, "SUMMARY_PATH", tmp_path / "conversation_summaries.json")
    monkeypatch.setattr(C, "RENAME_PATH", tmp_path / "conversation_renames.json")
    monkeypatch.setattr(C, "OWNER_PATH", tmp_path / "conversation_owners.json")
    C.save_conversation_summaries({"old-1": "老会话一", "old-2": "老会话二"})

    assert C.owner_of("old-1") == GUEST
    assert C.ids_of_user(GUEST) == {"old-1", "old-2"}
    assert C.ids_of_user("u-nobody") == set(), "别的账号不该看到老会话"
    assert not C.OWNER_PATH.exists(), "只是读，却把归属文件写出来了"


def test_new_conversation_records_owner(conv):
    assert C.owner_of(conv.a) == "u-alice"
    assert C.owner_of(conv.b) == "u-bob"
    assert conv.a in C.ids_of_user("u-alice") and conv.b not in C.ids_of_user("u-alice")
    assert conv.b in C.ids_of_user("u-bob") and conv.a not in C.ids_of_user("u-bob")
    assert C.ids_of_user(GUEST) == {"old-1", "old-2"}, "新会话跑到默认账号名下了"
    assert not (C.ids_of_user("u-alice") & C.ids_of_user("u-bob")), "两个账号的会话有交集"


def test_every_conversation_has_exactly_one_owner(conv):
    """三个账号的会话加起来 = 总数（不重不漏）。"""
    total = (
        len(C.ids_of_user(GUEST))
        + len(C.ids_of_user("u-alice"))
        + len(C.ids_of_user("u-bob"))
    )
    assert total == len(C.load_conversation_summaries())


def test_belongs_to_checks_existence_and_owner(conv):
    assert C.belongs_to(conv.a, "u-alice") is True
    assert C.belongs_to(conv.a, "u-bob") is False
    assert C.belongs_to("t-not-exist", "u-alice") is False
    assert C.belongs_to(conv.a, GUEST) is False, "登录用户的会话，默认账号拿不到"
    assert C.belongs_to("old-1", GUEST) is True, "老会话对默认账号可见"
    assert C.belongs_to("t-not-exist", GUEST) is False, "存在性要先判"


def test_drop_conversation_removes_all_three(conv):
    """标题、重命名标记、归属记录三份一起清，且重复删除幂等。"""
    C.rename_conversation(conv.a, "改过名的会话")
    assert C.is_renamed(conv.a) is True

    assert C.drop_conversation(conv.a) is True
    assert conv.a not in C.load_conversation_summaries(), "标题没删"
    assert C.is_renamed(conv.a) is False, "重命名标记没删"
    assert conv.a not in json.loads(C.OWNER_PATH.read_text(encoding="utf-8")), "归属记录没删"

    assert C.drop_conversation(conv.a) is False, "第二次删除不幂等"
    assert C.owner_of(conv.a) == GUEST, "删掉之后没兜底回默认账号"
    assert C.belongs_to(conv.a, "u-alice") is False, "删掉之后原主人还能拿到"


def test_corrupt_owner_file_degrades_to_guest(conv):
    """归属文件坏掉 / 值为空时不许崩，也不许把会话判给错误的人。"""
    C.OWNER_PATH.write_text("{ 这不是 json", encoding="utf-8")
    assert C.owner_of(conv.b) == GUEST, "损坏的归属文件应该当成没有记录"
    assert C.ids_of_user(GUEST) == {"old-1", "old-2", conv.a, conv.b}
    assert C.ids_of_user("u-alice") == set(), "损坏后还认为 alice 有会话"

    C.save_conversation_owners({conv.b: ""})
    assert C.owner_of(conv.b) == GUEST, "归属值为空字符串也要兜底"

    C.save_conversation_owners({"whatever": "u-alice"})
    assert C.ids_of_user("u-alice") == set(), "只在归属文件、会话表里没有的记录要被滤掉"


def test_real_data_dir_is_untouched(conv):
    """路径确实换到了临时目录，真实 data/ 一个字节都不该动。"""
    before = _real_snapshot()
    C.create_conversation("u-alice")
    C.drop_conversation(conv.a)
    after = _real_snapshot()

    assert C.OWNER_PATH != REAL_DIR / "conversation_owners.json", "模块路径没被换掉"
    assert before == after, f"真实会话元数据被改了：{after}"
