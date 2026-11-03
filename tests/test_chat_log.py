"""chat_log（展示层消息存储）的回归测试。

重点盯三件事：追加不改旧条目、回退按轮号切不按位置切、清空后 key 摘掉让
读端退回 checkpoint。存储路径必须重定向到临时目录——漏了就会静默写进
`data/chat_log.json` 真库（2026-11-02 在 documents 上踩过同一个坑）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import chat_log


@pytest.fixture
def tmp_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把展示层存储指到临时目录。"""
    path = tmp_path / "chat_log.json"
    monkeypatch.setattr(chat_log, "CHAT_LOG_PATH", path)
    return path


def _turn(thread_id: str, turn: int, text: str) -> None:
    chat_log.append_turn(
        thread_id,
        turn,
        {"role": "user", "content": text},
        [{"role": "assistant", "content": f"re:{text}"}],
    )


def test_append_and_read_roundtrip(tmp_log: Path) -> None:
    _turn("t-1", 1, "你好")
    _turn("t-1", 2, "再见")

    entries = chat_log.entries("t-1")
    assert entries is not None
    assert [e["content"] for e in entries] == ["你好", "re:你好", "再见", "re:再见"]
    assert entries[0]["role"] == "user" and entries[1]["role"] == "assistant"

    # 落盘是真 JSON 文件，条目里不该漏进轮号包装（那是存储形态，不是展示形态）
    raw = json.loads(tmp_log.read_text(encoding="utf-8"))
    assert "turn" in raw["t-1"][0] and "turn" not in raw["t-1"][0]["entry"]


def test_missing_thread_returns_none_falls_back_to_checkpoint(tmp_log: Path) -> None:
    """没有记录的会话返回 None（读端据此退回 checkpoint），不是空列表。"""
    assert chat_log.entries("t-none") is None


def test_rewind_cuts_by_turn_number_not_position(tmp_log: Path) -> None:
    """轮 2 挂掉没记上、轮 3 正常记——按轮号删才不会错位。"""
    _turn("t-1", 1, "第一轮")
    # 轮 2 缺席（模拟流挂掉且没走补记）
    _turn("t-1", 3, "第三轮")

    chat_log.rewind("t-1", from_turn=2)

    entries = chat_log.entries("t-1")
    assert entries is not None
    # 只剩轮 1：轮 3 虽然在位置 1，也必须被删掉
    assert [e["content"] for e in entries] == ["第一轮", "re:第一轮"]


def test_rewind_keeps_earlier_turns(tmp_log: Path) -> None:
    _turn("t-1", 1, "第一轮")
    _turn("t-1", 2, "第二轮")
    _turn("t-1", 3, "第三轮")

    chat_log.rewind("t-1", from_turn=3)

    entries = chat_log.entries("t-1")
    assert entries is not None
    assert [e["content"] for e in entries if e["role"] == "user"] == ["第一轮", "第二轮"]


def test_rewind_to_empty_drops_key(tmp_log: Path) -> None:
    """清空后 key 摘掉：读端退回 checkpoint（此时那边也只剩 SystemMessage）。"""
    _turn("t-1", 1, "唯一一轮")
    chat_log.rewind("t-1", from_turn=1)

    assert chat_log.entries("t-1") is None


def test_rewind_missing_thread_is_noop(tmp_log: Path) -> None:
    chat_log.rewind("t-none", from_turn=1)
    assert chat_log.entries("t-none") is None


def test_drop_removes_only_wanted(tmp_log: Path) -> None:
    _turn("t-1", 1, "留下")
    _turn("t-2", 1, "删掉")

    chat_log.drop(["t-2", "t-none"])

    assert chat_log.entries("t-1") is not None
    assert chat_log.entries("t-2") is None


def test_corrupt_file_reads_as_empty(tmp_log: Path) -> None:
    """存储文件坏了就当没记录，让读端退回 checkpoint，而不是把请求带崩。"""
    tmp_log.write_text("{not json", encoding="utf-8")
    assert chat_log.entries("t-1") is None
    # 坏文件也能被下一次写入覆盖回来
    _turn("t-1", 1, "重建")
    assert chat_log.entries("t-1") is not None
