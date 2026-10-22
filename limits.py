"""各种「别把资源用爆」的限制：每日配额 + 接口限流。

两件事放一个文件，是因为它们回答的是同一个问题——**这个人现在还能不能再做一次**。
但机制完全不同，所以分成两节：

- **配额**（按天计数）：问了多少次、存了几本书。要跨重启保留，所以落 SQLite；
  按天自然失效（换一天就是新的一行），不需要任何定时清理。
- **限流**（按分钟滑窗）：几秒内连点了几次。**故意只放内存**——它的价值在于
  「现在挡住」，重启后从零开始完全可接受；落盘反而会带来「上次重启前的那几秒
  还占着额度」这种没道理的行为。


内存计数重启就归零，那「每日 200 次」等于没有——重启一次就白送一轮。
反过来，限流落盘是过度设计：它防的是「这一秒刷了 50 次」，而这个瞬间
根本熬不过一次重启。


按天切是用户的直觉（「今天用得有点多」），也省掉了滑动窗口的实现。
代价是凌晨 0 点会重置，有人可以 23:59 用满再在 0:01 用一波——
对「几个同学共用一个站」这个规模，这点漏洞不值得用更复杂的方案去堵。
"""
from __future__ import annotations


import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "limits.sqlite3"


ASK_PER_DAY = 200

DOCS_PER_USER = 20

GUEST_ASK_PER_DAY = 500

GUEST_THREAD_TTL_DAYS = 7
GUEST_THREAD_MAX = 300


LOGIN_ATTEMPTS_PER_MINUTE = 10

ASKS_PER_MINUTE = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    user_id TEXT NOT NULL,
    day     TEXT NOT NULL,      -- 本地时区的 YYYY-MM-DD
    kind    TEXT NOT NULL,      -- ask / upload / …
    count   INTEGER NOT NULL,
    PRIMARY KEY (user_id, day, kind)
);
-- 按天整体清理（可选的维护动作，不是必需）
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily(day);
"""

_init_lock = threading.Lock()


def _schema_ready(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily'"
        ).fetchone()
        is not None
    )


def _connect() -> sqlite3.Connection:
    """和平常一样「冷启动要串行」。理由见 `memories.py` 里那段实测。"""
    if not DB_PATH.exists():
        with _init_lock:
            if not DB_PATH.exists():
                conn = sqlite3.connect(str(DB_PATH))
                try:
                    conn.executescript(_SCHEMA)
                    conn.commit()
                finally:
                    conn.close()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if not _schema_ready(conn):
        with _init_lock:
            if not _schema_ready(conn):
                conn.executescript(_SCHEMA)
                conn.commit()
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()



def today() -> str:
    """本地时区的今天。**不用 UTC**：用户看的「今天」是他自己的今天。"""
    return date.today().isoformat()


def used_today(user_id: str, kind: str) -> int:
    """今天这个人在这一类上已经用掉多少。没有记录就是 0。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT count FROM daily WHERE user_id = ? AND day = ? AND kind = ?",
            (str(user_id), today(), str(kind)),
        ).fetchone()
    return int(row["count"]) if row else 0


def consume(user_id: str, kind: str, limit: int, amount: int = 1) -> int:
    """试着记一次用量。**成功返回剩余次数（可能是 0），超额返回 -1。**

    不用 0 表示超额：第 `limit` 次本身是合法的，它成功之后剩余正好是 0；两种都返回 0，调用方
    就分不出「最后一次」和「多出来的那一次」。

    一条 UPSERT 同时完成「判断 + 记录」：`DO UPDATE ... WHERE count + ? <= ?` 不成立时这一行
    既不插也不改、`RETURNING` 什么都拿不到——正好就是超额，没有「先查再改」的窗口，并发下也
    不会超发。调用方不该先 `used_today` 再 `consume`（中间可被插队），真正说了算的是这里的返回值。
    """
    if amount <= 0:
        return max(0, limit - used_today(user_id, kind))
    key, day = str(user_id), today()
    with _db() as conn:
        try:
            row = conn.execute(
                """
                INSERT INTO daily (user_id, day, kind, count)
                SELECT ?, ?, ?, ? WHERE ? <= ?
                ON CONFLICT(user_id, day, kind) DO UPDATE
                    SET count = count + ?
                    WHERE count + ? <= ?
                RETURNING count
                """,
                (
                    key, day, str(kind), amount,   # INSERT 的值
                    amount, limit,                  # INSERT 的门槛
                    amount, amount, limit,          # UPDATE 的门槛
                ),
            ).fetchone()
        except sqlite3.OperationalError:
            return limit
    if row is None:
        return -1
    remaining = limit - int(row["count"])
    return remaining if remaining >= 0 else -1


def refund(user_id: str, kind: str, amount: int = 1) -> int:
    """把记过的次数退回去，用在「下游失败、这次不该算」的时候。只该在 `consume` 成功之后调。

    不检查会不会退成负数：加一道 `MAX(count - ?, 0)` 会让「退多了」这种 bug 变得无声无息。
    """
    if amount <= 0:
        return 0
    with _db() as conn:
        row = conn.execute(
            """
            UPDATE daily SET count = count - ?
             WHERE user_id = ? AND day = ? AND kind = ?
            RETURNING count
            """,
            (amount, str(user_id), today(), str(kind)),
        ).fetchone()
    return int(row["count"]) if row else 0


def purge_before(day: str) -> int:
    """删掉 `day` 之前的所有计数行。没有定时任务，靠调用方在启动时顺手跑一次。"""
    with _db() as conn:
        return conn.execute("DELETE FROM daily WHERE day < ?", (str(day),)).rowcount



_windows: dict[str, deque] = {}
_windows_lock = threading.Lock()
_SWEEP_THRESHOLD = 4096


def _sweep(now: float, seconds: float) -> None:
    """清掉那些「整个队列都已过期」的 key。**调用方必须已持有 `_windows_lock`。**"""
    stale = [k for k, q in _windows.items() if not q or now - q[-1] >= seconds]
    for key in stale:
        _windows.pop(key, None)


def allow(key: str, limit: int, seconds: float) -> tuple[bool, float]:
    """这个 key 现在还能不能再来一次，返回 `(放行?, 要等几秒)`。

    放行时第二个值是 0，被挡时是「最早那次命中滑出窗口还要多久」，调用方直接拿去做
    `Retry-After`。用 `time.monotonic()` 而不是墙钟：NTP 校正会让墙钟倒退、滑窗就乱了。
    """
    if limit <= 0:
        return True, 0.0
    now = time.monotonic()
    with _windows_lock:
        queue = _windows.get(key)
        if queue is None:
            if len(_windows) >= _SWEEP_THRESHOLD:
                _sweep(now, seconds)
            queue = deque()
            _windows[key] = queue

        while queue and now - queue[0] >= seconds:
            queue.popleft()

        if len(queue) >= limit:
            return False, max(0.0, seconds - (now - queue[0]))

        queue.append(now)
        return True, 0.0


def reset_rate_limits() -> None:
    """清空所有限流状态。只给探针用——生产代码不该调它，
    那等于把「防刷」这层在运行时关掉。"""
    with _windows_lock:
        _windows.clear()


def rate_limit_state() -> dict[str, int]:
    """当前有多少个 key 在计数。给探针看泄漏用的。"""
    with _windows_lock:
        return {"keys": len(_windows)}
