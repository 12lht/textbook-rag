"""各种「别把资源用爆」的限制：每日配额 + 接口限流。

两件事放一个文件，是因为它们回答的是同一个问题——**这个人现在还能不能再做一次**。
但机制完全不同，所以分成两节：

- **配额**（按天计数）：问了多少次、存了几本书。要跨重启保留，所以落 SQLite；
  按天自然失效（换一天就是新的一行），不需要任何定时清理。
- **限流**（按分钟滑窗）：几秒内连点了几次。**故意只放内存**——它的价值在于
  「现在挡住」，重启后从零开始完全可接受；落盘反而会带来「上次重启前的那几秒
  还占着额度」这种没道理的行为。

## 为什么配额要落盘

内存计数重启就归零，那「每日 200 次」等于没有——重启一次就白送一轮。
反过来，限流落盘是过度设计：它防的是「这一秒刷了 50 次」，而这个瞬间
根本熬不过一次重启。

## 为什么用「天」而不是「24 小时」

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

# --------------------------------------------------------------------------
# 配额数值
# --------------------------------------------------------------------------

# 每人每天能问多少次。
#
# 200 这个数的依据：一次复习可能连着问几十轮，一天下来 200 次是「用得很凶」
# 但不会撞墙的量；而脚本刷起来几秒就是几百，所以它挡得住脚本、挡不住人。
ASK_PER_DAY = 200

# 每人最多存几本书。几个同学各传自己的教材，一学期 20 本够用。
# 真正占空间的是单本大小（`documents.MAX_FILE_BYTES`，250MB），
# 两者相乘就是一个人最多能占多少磁盘——20 × 250MB = 5GB，这是有意接受的量级。
DOCS_PER_USER = 20

# 全部访客加起来每天能问多少次（2026-10-22 加）。
#
# 为什么个人配额之外还要一个总池：访客身份按浏览器 cookie 分配，**清掉 cookie
# 就是新身份、配额归零**。只按人算的话，一个脚本循环「清 cookie 再提问」
# 就能无限刷，而每一次都真金白银地调模型。池子把「陌生人总共能烧多少」
# 封死在一个数上，清 cookie 绕不过去。
#
# 500 的依据：给同学用、他们本来就会注册，访客只是「打开试试」的余量。
# 池满时的文案顺势引导注册（见 `service.take_ask_slot`）。
GUEST_ASK_PER_DAY = 500

# 访客会话的保留期与总量上限（`service.purge_stale_guests` 用）。
#
# 访客的会话没人负责删：他不注册、就没有「我的会话」这个界面，过期数据
# 会一直堆在 checkpoint 库里。7 天足够「隔天回来接着问」，再久也没意义。
# 300 是兜底——万一有人成批制造访客身份，也不能靠时间慢慢清。
GUEST_THREAD_TTL_DAYS = 7
GUEST_THREAD_MAX = 300

# --------------------------------------------------------------------------
# 限流数值
# --------------------------------------------------------------------------

# 登录：同一来源 1 分钟内最多试几次。PBKDF2 一次约 110ms，
# 不设限的话一个循环就能把 CPU 占满，顺便还能暴力猜密码。
LOGIN_ATTEMPTS_PER_MINUTE = 10

# 提问：同一个人 1 分钟内最多发几次。正常打字远达不到，
# 但「按住回车」和「脚本连发」会。一条消息要跑模型、要花钱，必须挡。
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


# --------------------------------------------------------------------------
# 配额：按天计数
# --------------------------------------------------------------------------

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

    这里刻意**不用 0 表示超额**：第 `limit` 次本身是合法的，它成功之后剩余
    正好是 0。两种情况的返回值要是都是 0，调用方就分不出「最后一次」和
    「多出来的那一次」——那会把用户卡在额度还差一次的尴尬位置上。

    用一条 UPSERT 一次搞定「判断还能不能记 + 记录」：

    ```sql
    INSERT ... VALUES (?, ?, ?, ?)
    ON CONFLICT(...) DO UPDATE SET count = count + ? WHERE count + ? <= ?
    RETURNING count
    ```

    `DO UPDATE ... WHERE` 不成立时这一行既不插也不改，`RETURNING` 什么都拿不到
    ——正好就是「超额」。所以没有「先查再改」的中间窗口，并发下也不会超发。

    调用方不该「先 `used_today` 再 `consume`」：那两次调用之间就是可以被插队的空档，
    而且真正说了算的是这里的返回值。
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
            # 极老的 SQLite 不认 `RETURNING`。不值得为它写一套降级路径，
            # 但也不能让它把整个提问接口 500 掉——退回「不加限制」，
            # 宁可少一层保护，也不要正常用户用不了。
            return limit
    if row is None:
        return -1
    # **不能写 `max(0, ...)`**：`limit` 被调小之后（比如从 200 改成 100，
    # 而今天已经用了 150）算出来是负数，抹成 0 就变成「成功、剩 0 次」，
    # 于是下一次照样放行——额度形同虚设。负数就是超额。
    remaining = limit - int(row["count"])
    return remaining if remaining >= 0 else -1


def refund(user_id: str, kind: str, amount: int = 1) -> int:
    """把记过的次数退回去。用在下游失败、这次不该算的时候。

    不检查会不会退成负数：调用方只该在 `consume` 成功之后才退，
    而多加一道 `MAX(count - ?, 0)` 会让「退多了」这种 bug 变得无声无息。
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


# --------------------------------------------------------------------------
# 限流：内存滑窗
# --------------------------------------------------------------------------
#
# 每个 key 存一个「最近命中的时刻」队列。判定的就是队列里还有几个落在窗口内：
#
#     窗口 60 秒、上限 3 次
#     t=0   hit  → [0]           放行
#     t=10  hit  → [0,10]        放行
#     t=20  hit  → [0,10,20]     放行
#     t=30  hit  → 三个都还在窗口内 → 挡住，要等到 t=60
#
# 为什么记「时刻队列」而不是「计数器 + 上次清零时间」：那种写法在窗口边界
# 会出现「两倍突发」——59 秒时用满、60 秒清零后再用满，等于 2 秒内两次满额。

_windows: dict[str, deque] = {}
_windows_lock = threading.Lock()
# 攒到这么多 key 就顺手清一次全空的条目，防止长期运行时字典无限长大。
_SWEEP_THRESHOLD = 4096


def _sweep(now: float, seconds: float) -> None:
    """清掉那些「整个队列都已过期」的 key。**调用方必须已持有 `_windows_lock`。**"""
    stale = [k for k, q in _windows.items() if not q or now - q[-1] >= seconds]
    for key in stale:
        _windows.pop(key, None)


def allow(key: str, limit: int, seconds: float) -> tuple[bool, float]:
    """这个 key 现在还能不能再来一次。返回 `(放行?, 要等几秒)`。

    放行时第二个值是 0；被挡时是「最早那次命中滑出窗口还要多久」——
    直接给调用方拿去做 `Retry-After`，不用自己再算。

    `seconds` 用 `time.monotonic()` 计，不用墙钟：系统时间被校正时
    （NTP 跳一下）墙钟会突然倒退，滑窗就乱了，而单调时钟不会。
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
            # 队列满 = 窗口内的次数用完了。等最早那次滑出去就能再来。
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
