"""展示层消息存储：前端历史回放用的对话记录，与 checkpoint 明确分工。

checkpoint（SQLite，由 LangGraph 的 checkpointer 管）是 **agent 运行状态**：
`truncate_node` 会按 token 把旧轮次物理删除，回退也会删。这份存储是**展示层的
事实**：每轮聊完把完整的一轮原样记下来，前端回放读它——被裁掉的早期消息在
UI 上依然完整，摘要在系统提示里另有副本（那是为了模型，不是为了人）。

**写入时机只有一处**：`service.chat_events` 收到 done 之后（此时 checkpoint 已
落盘），从 `agent_core.get_history` 取「最后一条用户消息及其后的助手侧条目」，
盖上轮号存进来——消息结构的翻译逻辑只有 get_history 一份，这里绝不复写。
流中途挂掉的轮次由 service 决定要不要补记用户消息，本模块不管。

条目带 `turn`（1-based 轮号，口径同 `should_generate_title`）而不靠位置对齐：
回退按轮号删（`turn >= from_turn` 全清），这样即使某个轮次缺席，后续轮次的
删除范围也不会错位。

存的是**未装饰**条目（附件是裸 id、引用里是 as_xx 相对名）：装饰（签名 URL）
带时效，每次读取现做，存死链接过几天全 403。

读端约定：这里**没有记录**的会话（历史存量），service 层退回 checkpoint——
新会话零迁移，旧会话保持原行为。
"""
from __future__ import annotations

import json
from typing import Any

from conversations import DATA_DIR

CHAT_LOG_PATH = DATA_DIR / "chat_log.json"


def _load() -> dict[str, list[dict[str, Any]]]:
    if not CHAT_LOG_PATH.exists():
        return {}
    try:
        data = json.loads(CHAT_LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, list[dict[str, Any]]]) -> None:
    CHAT_LOG_PATH.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def entries(thread_id: str) -> list[dict[str, Any]] | None:
    """这个会话的展示条目（已剥掉轮号包装）。

    返回 `None` 表示这里没有记录（历史存量会话），调用方应退回 checkpoint；
    返回 `[]` 表示有记录但为空（例如回退清空过），**以这里为准**。
    """
    stored = _load().get(str(thread_id))
    if stored is None:
        return None
    return [dict(item["entry"]) for item in stored]


def append_turn(
    thread_id: str,
    turn_number: int,
    user_entry: dict[str, Any],
    assistant_entries: list[dict[str, Any]],
) -> None:
    """记一轮：用户消息 + 助手侧条目，全部盖上同一个轮号。

    重复调用会写出重复轮号（例如流挂掉后补记、下一轮又正常记了一次），这是
    有意的：回退按 `turn >= from_turn` 删，重复轮号会一起清掉，不会错位。
    """
    data = _load()
    log = data.setdefault(str(thread_id), [])
    log.append({"turn": int(turn_number), "entry": dict(user_entry)})
    for entry in assistant_entries:
        log.append({"turn": int(turn_number), "entry": dict(entry)})
    _save(data)


def rewind(thread_id: str, from_turn: int) -> None:
    """删掉第 `from_turn` 轮（1-based）及其后的全部展示条目。

    口径对齐 `agent_core.rewind_thread`：那边 `turn` 是 0-based 的用户消息
    序号，调用方传 `turn + 1` 过来。清空后把 key 摘掉，让读端退回
    checkpoint（此时 checkpoint 里也只剩 SystemMessage，两者一致）。
    """
    data = _load()
    log = data.get(str(thread_id))
    if log is None:
        return
    kept = [item for item in log if item["turn"] < from_turn]
    if kept:
        data[str(thread_id)] = kept
        _save(data)
    else:
        data.pop(str(thread_id), None)
        _save(data)


def drop(thread_ids: list[str]) -> None:
    """会话被删除（单个删或访客清理）时，把展示条目一并清掉。"""
    wanted = {str(thread_id) for thread_id in thread_ids}
    if not wanted:
        return
    data = _load()
    hit = [thread_id for thread_id in wanted if thread_id in data]
    if not hit:
        return
    for thread_id in hit:
        del data[thread_id]
    _save(data)
