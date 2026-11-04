"""展示层消息存储：前端历史回放用的对话记录，与 checkpoint 明确分工。

checkpoint（SQLite，由 LangGraph 的 checkpointer 管）是 **agent 运行状态**：
`truncate_node` 会按 token 把旧轮次物理删除，回退也会删。这份存储是**展示层的
事实**：每轮聊完把完整的一轮原样记下来，前端回放读它——被裁掉的早期消息在
UI 上依然完整，摘要在系统提示里另有副本（那是为了模型，不是为了人）。

**为什么是 SQLite 而不是 JSON**：第一版用了和 conversations 一致的整读整写
JSON，复盘发现两笔真实的债——每记一轮全量重写整个文件、读端全量 parse。
迁到 SQLite 后：读端按 `(thread_id, turn)` 索引取单会话，写入是逐行 INSERT，
原子性由事务保证（多 worker 部署也成立），和 `data/` 下其余存储形态统一。
对外只暴露四个函数，引擎是可替换的实现细节——本次迁移调用方一行没改。

**写入时机只有一处**：`service.chat_events` 收到 done 之后（此时 checkpoint 已
落盘），从 `agent_core.get_history` 取「最后一条用户消息及其后的助手侧条目」，
盖上轮号存进来——消息结构的翻译逻辑只有 get_history 一份，这里绝不复写。
流中途挂掉的轮次由 service 决定要不要补记用户消息，本模块不管。

条目带 `turn`（1-based 轮号，口径同 `should_generate_title`）而不靠位置对齐：
回退按轮号删（`turn >= from_turn` 全清），这样即使某个轮次缺席，后续轮次的
删除范围也不会错位。**重复轮号是有意的**（流挂掉补记后又正常记了一次），
所以表上只有普通索引、没有唯一约束，读端按 `(turn, rowid)` 排序保持追加序。

存的是**未装饰**条目（附件是裸 id、引用里是 as_xx 相对名）：装饰（签名 URL）
带时效，每次读取现做，存死链接过几天全 403。

读端约定：这里**没有记录**的会话（历史存量），service 层退回 checkpoint——
新会话零迁移，旧会话保持原行为。库文件损坏时按「可再生的缓存」处理：
丢弃重建，绝不把请求带崩。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from conversations import DATA_DIR

CHAT_LOG_PATH = DATA_DIR / "chat_log.sqlite3"

_conn: sqlite3.Connection | None = None

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS turns ("
    " thread_id TEXT NOT NULL,"
    " turn INTEGER NOT NULL,"
    " entry TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_turns_thread ON turns (thread_id, turn)",
)


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    conn = sqlite3.connect(CHAT_LOG_PATH)
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.commit()
    except sqlite3.DatabaseError:
        conn.close()  # 坏库的句柄必须先关，Windows 上不关 unlink 会失败
        raise
    _conn = conn
    return conn


def _recreate() -> sqlite3.Connection:
    """库文件损坏时删掉重建。展示层是可再生的，宁可丢数据不能带崩请求。"""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:  # noqa: BLE001 — 连不上就当它坏了
            pass
        _conn = None
    CHAT_LOG_PATH.unlink(missing_ok=True)
    return _connect()


def _reset_for_tests() -> None:
    """测试夹具用：换存储路径前必须先断开旧连接，否则 patch 不生效。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def entries(thread_id: str) -> list[dict[str, Any]] | None:
    """这个会话的展示条目（已剥掉轮号包装，按追加序）。

    返回 `None` 表示这里没有记录（历史存量会话或已回退清空），调用方应退回
    checkpoint；`rowid` 保序，同轮内用户消息在前、助手条目在后。
    """
    try:
        rows = _connect().execute(
            "SELECT entry FROM turns WHERE thread_id = ? ORDER BY turn, rowid",
            (str(thread_id),),
        ).fetchall()
    except sqlite3.DatabaseError:
        _recreate()
        return None
    if not rows:
        return None
    return [dict(json.loads(row[0])) for row in rows]


def _mutate(sql: str, seq: list[tuple[Any, ...]]) -> None:
    """写路径统一入口：库损坏时丢弃重建、重试一次（展示层可再生，宁丢不崩）。"""
    try:
        conn = _connect()
    except sqlite3.DatabaseError:
        conn = _recreate()
    try:
        with conn:
            conn.executemany(sql, seq)
    except sqlite3.DatabaseError:
        conn = _recreate()
        with conn:
            conn.executemany(sql, seq)


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
    rows = [
        (str(thread_id), int(turn_number), json.dumps(entry, ensure_ascii=False))
        for entry in (user_entry, *assistant_entries)
    ]
    _mutate(
        "INSERT INTO turns (thread_id, turn, entry) VALUES (?, ?, ?)", rows
    )


def rewind(thread_id: str, from_turn: int) -> None:
    """删掉第 `from_turn` 轮（1-based）及其后的全部展示条目。

    口径对齐 `agent_core.rewind_thread`：那边 `turn` 是 0-based 的用户消息
    序号，调用方传 `turn + 1` 过来。删空后这里自然无记录，读端退回
    checkpoint（此时 checkpoint 里也只剩 SystemMessage，两者一致）。
    """
    _mutate(
        "DELETE FROM turns WHERE thread_id = ? AND turn >= ?",
        [(str(thread_id), int(from_turn))],
    )


def drop(thread_ids: list[str]) -> None:
    """会话被删除（单个删或访客清理）时，把展示条目一并清掉。"""
    if not thread_ids:
        return
    _mutate(
        "DELETE FROM turns WHERE thread_id = ?",
        [(str(thread_id),) for thread_id in thread_ids],
    )
