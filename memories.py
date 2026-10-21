"""跨会话的长期记忆存储：按 `user_id` 存「下次换个会话也该记得」的事实。

和 `conversations.py` 一样，这一层是纯应用层：只读写 `data/memories.sqlite3`，
**不 import 任何 agent 相关的东西**。分工是：

- 什么时候记、记什么 —— **模型自己判断**（它手里有 `save_memory` / `recall_memory`）
- 把这两个工具接到这一层 —— `service.py` 通过 `agent_core.set_memory_backend()` 注入
- 存成什么、怎么去重 —— 就在这里

## 为什么是 SQLite，而不是一个 JSON 文件

原来是一个 `memories.json`，写入是「把整份文件读进来 → 改一个用户的数组 →
把整份写回去」。于是成本跟着**总用户数**走，而不是跟着「这个用户自己有多少条」走：

```text
总用户数     文件      写一条事实
100        0.6MB      39ms
1000       5.9MB     290ms
5000      29.7MB       1.3s
20000      119MB       5.2s
```

问题不在磁盘空间，在**读改写的粒度**：任何一个人记一句话，代价由所有人的人数决定。
换成 SQLite 之后，写只碰这一个用户的行，成本与别人无关。

**顺带删掉了一把锁。** 原来 `memories.py` 里有一把 `threading.Lock`，因为
「读整份文件 → 改 → 整份写回」不是原子过程，两个线程会互相覆盖。现在去重和
累加由一条 SQL 在一个事务里完成，**原子性由数据库保证**，锁就没有存在的理由了。
但**初始化还是需要一把锁**（`_init_lock`），原因见 `_connect` 的注释——
这是换存储过程中唯一踩到的真坑。

写一条事实的成本从「跟着总用户数走」变成了**常数**（`probe-memory-scale.py`）：

```text
总用户数        100      1000      5000     20000
写一条耗时   30.4ms    29.5ms    29.8ms    29.5ms     ← 200 倍规模，纹丝不动
（换之前）     39ms      290ms      1.3s      5.2s
```

这 ~30ms 和我们的代码无关（裸 sqlite3 插一行也是 28.8ms），它是 SQLite
**回滚日志模式每次提交都要新建 + 删除一个 `-journal` 文件**的成本。
对「隔几轮才写一次」完全够用，所以**没有去调日志模式**；
真要调的话 `probe-sqlite-journal-modes.py` 里有实测对比。

## 表结构（一条事实一行）

```sql
CREATE TABLE memories (
    user_id     TEXT NOT NULL,
    id          TEXT NOT NULL,   -- 给界面用的稳定编号，和内容无关
    text        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,   -- 归一化后的文本，去重的依据
    kind        TEXT NOT NULL,
    source      TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, fingerprint)
);
```

**去重按 `fingerprint`**（归一化后的文本），不是语义：同一条事实反复记只会累加
`hits`、刷新 `last_seen`，不会多出一行。归一化只做最小处理（去空白、去标点、
转小写），够挡住「不吃香菜。」和「不吃香菜」这种差异。

语义级的合并（「不吃香菜」和「受不了香菜」算不算一条）是以后上向量库时的事，
现在**不要**假装做到了。

## 为什么多出一个 `id` 列（踩过的坑）

一开始为了省一列，直接把 `fingerprint` 当界面上那一行的编号用。看起来没问题：
它能唯一定位一行，还是主键的一半。**但它是从原文算出来的**——
用户点「改一下措辞」，原文变了，`fingerprint` 就跟着变，那一行在数据库里等于
**换了个编号**。前端手里还攥着旧编号，接着点「删除」就删不到了：

```text
列表拿到 id=称呼我为涛哥
用户改成「叫我涛哥」        → 内部变成 id=叫我涛哥
前端还拿「称呼我为涛哥」删  → 404 这条记忆不存在   ← 就是这里
```

这类 bug 特别难看出来：改是成功的、列表刷新后也是对的，**只有「改完马上删」
这一个动作序列会失败**。所以编号必须和内容解耦：`id` 是随机值，写入时生成，
之后**再怎么改措辞都不变**（`update_text` 删旧插新时把旧 `id` 带过去）。

老库（没有 `id` 列）在第一次连接时自动补，见 `_ensure_id_column`。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "memories.sqlite3"

# 老版本用的 JSON 文件。如果还在，第一次连库时会一次性导进来（见 `_import_legacy_json`）。
LEGACY_JSON_PATH = DATA_DIR / "memories.json"

# 单条事实的长度上限。模型偶尔会吐一整段话进来，截断比存下来更好。
MAX_TEXT_LENGTH = 120
# 每个用户最多存多少条。超了先丢「最久没再出现」的那些。
MAX_PER_USER = 200
# kind 只认这几个，其余一律归到「其他」。模型不守规矩时不能让脏值落盘。
# **要和 `agent_core.MemoryKind` 逐字一致**（那边是给模型的枚举，不能 import 本模块）。
KINDS = ("偏好", "身份", "计划", "事实")
DEFAULT_KIND = "其他"

_STRIP_CHARS = set(" \t\r\n，。！？、；：,.!?;:\"'“”‘’（）()【】[]《》<>-—_~`*#")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    user_id     TEXT NOT NULL,
    id          TEXT NOT NULL,
    text        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    kind        TEXT NOT NULL,
    source      TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, fingerprint)
);
-- 读一个用户的记忆按写入先后，写出去的顺序才稳定
CREATE INDEX IF NOT EXISTS idx_memories_order ON memories(user_id);
-- 裁掉最旧的那些时要按 last_seen 找
CREATE INDEX IF NOT EXISTS idx_memories_seen ON memories(user_id, last_seen);
-- 注意：`id` 的唯一索引**不能**写在这里。老库的表已经存在，
-- `CREATE TABLE IF NOT EXISTS` 不会给它加列，这条索引会直接报 no such column。
-- 建到 `_ensure_id_column` 里，一定在 ALTER 之后。
"""

# **初始化的**锁，和数据无关。
#
# 别把它和「原来那把保护读改写的锁」搞混：那把已经删了，因为去重和累加由
# 一条事务里的 SQL 保证原子性。这一把保护的是**建表 / 迁移老数据**这几个动作。
#
# 所以正确的说法是：**数据不需要锁，初始化需要。** 只在第一次连某个库时用得上。
_init_lock = threading.Lock()


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------

def _schema_ready(conn: sqlite3.Connection) -> bool:
    """表建好了没有。

    直接问数据库，而不是在内存里记一个「建过了」的布尔值。记布尔的写法有两个坑，
    都踩过：探针删掉库文件之后进程还以为表在（报 `no such table`）；换了库路径
    也一样。问一次 `sqlite_master` 只要几微秒，换掉一整类「状态过期」的诡异问题。
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        is not None
    )


def _needs_init(conn: sqlite3.Connection) -> bool:
    """要不要跑一次初始化：表不在、缺 `id` 列、或者老 JSON 还没搬走。

    `_connect` 里是把这个条件**内联展开**写的（不再调用本函数），因为那里要判两次，
    展开后两处比较的是完全一样的表达式，改的时候记得一起改。留在这里是为了给探针
    一个「我问的和生产代码问的是同一件事」的入口。

    表不在时 `_has_id_column` 也是 False，但两个条件是「或」的关系，
    先短路到 `_schema_ready` 就够了，不用担心 PRAGMA 查空的表。
    """
    return (
        LEGACY_JSON_PATH.exists()
        or not _schema_ready(conn)
        or not _has_id_column(conn)
    )


def _new_id() -> str:
    """给一行生成稳定编号。和内容无关，所以改措辞不会换号。

    12 位十六进制 = 48 bit。单用户上限 `MAX_PER_USER` 才 200 条，
    按生日问题算碰撞概率约 200²/2^49 ≈ 4×10⁻¹²，可以不考虑。
    真要撞了也只是那条记忆插不进去（`INSERT OR IGNORE`），不会串到别人身上
    ——索引是 `(user_id, id)`，跨用户重号无所谓。
    """
    return uuid.uuid4().hex[:12]


def _has_id_column(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "id" for row in conn.execute("PRAGMA table_info(memories)"))


def _ensure_id_column(conn: sqlite3.Connection) -> None:
    """老库补 `id` 列并回填。**调用方必须已经持有 `_init_lock`。**

    幂等：列在就不再 ALTER，索引是 `IF NOT EXISTS`，回填只挑 id 为空的行。
    """
    if not _has_id_column(conn):
        # 不能直接 `ADD COLUMN id TEXT NOT NULL`：SQLite 要求无默认值的 NOT NULL
        # 列在已有数据的表上加不了。先加成可空列，回填完再靠代码保证——本层
        # 每条 INSERT 都会带 id，所以留空只可能是这一次回填之前的旧行。
        conn.execute("ALTER TABLE memories ADD COLUMN id TEXT")

    stale = conn.execute(
        "SELECT rowid FROM memories WHERE id IS NULL OR id = ''"
    ).fetchall()
    for row in stale:
        conn.execute("UPDATE memories SET id = ? WHERE rowid = ?", (_new_id(), row[0]))

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_id ON memories(user_id, id)"
    )


def _bootstrap(conn: sqlite3.Connection) -> None:
    """建表 + 补列 + 把老 JSON 搬进来。**调用方必须已经持有 `_init_lock`。**

    幂等：`CREATE TABLE IF NOT EXISTS` 和 `INSERT OR IGNORE` 重复跑都没有副作用，
    所以「不小心多跑一次」只是白干，不会坏数据。
    """
    conn.executescript(_SCHEMA)
    # 要排在导出老 JSON 之前：那个 INSERT 会写 id，列得先在。
    _ensure_id_column(conn)
    migrated = _import_legacy_json(conn)
    conn.commit()
    if migrated:
        # **搬完、提交成功之后**才把老文件改名。反过来做的话，
        # 万一事务回滚，改名已经生效 → 老数据永久丢了。
        _archive_legacy_json()


def _connect() -> sqlite3.Connection:
    """开一条新连接，需要时把库初始化好。

    **为什么每次都用新连接、用完就关**：这一层的调用发生在**工作线程**里
    （工具是同步函数，ToolNode 把它们丢进线程池），而 sqlite3 的连接默认不许
    跨线程复用。开一条连接只要几十微秒，相对于「省掉整份文件的读改写」不值一提，
    换来的是完全不用操心线程问题。

    **并发写怎么办**：由 SQLite 自己串行化（写事务互斥）。`connect` 默认的
    `timeout=5.0` 就是等锁的上限，等不到才会抛 `database is locked`。

    **但有一个例外，是这一层最容易踩的坑：库文件还不存在的时候。**
    `sqlite3.connect()` 遇到不存在的路径会顺手把文件建出来，**而这个「建」不是原子的**：

    ```text
    文件不存在，8 个线程各自 connect + 建表     落盘 5~7/8   异常在 CREATE TABLE 那行抛出
    先造出一个 0 字节文件，再并发                落盘 8/8     零异常
    表已建好（热）再并发                        落盘 8/8     零异常
    ```

    报错是两个都很误导人的样子：`attempt to write a readonly database`（像权限问题）、
    或 `PermissionError: [Errno 13]`。都跟权限无关，**根因是多个连接同时去创建同一个
    文件**；异常之所以在 `CREATE TABLE` 那行才抛，只是因为那是第一个需要写权限的语句。
    `CREATE TABLE` 本身并发是安全的——第二行里 8 个线程照样都在跑它，一个错没有。
    所以要串起来的只是「创建文件」这一步。

    做法：**先判文件在不在，不在就进锁、再判一次、建好再出来**（双重检查）。
    热路径（文件已在）完全不碰锁，每次连接只多一次 `exists()`。
    """
    if not DB_PATH.exists():
        with _init_lock:
            if not DB_PATH.exists():  # 等锁期间可能已经被别人建好了
                conn = sqlite3.connect(str(DB_PATH))
                try:
                    _bootstrap(conn)
                finally:
                    conn.close()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # 连到**已存在**的文件是并发安全的，所以这里可以放心在外面判。
    # 要跑初始化只有三种可能：老 JSON 还没搬走、表还没建、老表缺 `id` 列。
    if (
        LEGACY_JSON_PATH.exists()
        or not _schema_ready(conn)
        or not _has_id_column(conn)
    ):
        # 里面的动作都要串行做，理由见 `_init_lock`。判两次是为了别让每次连接
        # 都去抢锁——只有第一次（或老文件还没搬走时）才进得来。
        with _init_lock:
            if (
                LEGACY_JSON_PATH.exists()
                or not _schema_ready(conn)
                or not _has_id_column(conn)
            ):
                # **刻意不开 WAL**，用 SQLite 默认的回滚日志。这是个反直觉的决定，
                # 但实测过：本环境下 WAL + 多连接并发写会有一半左右失败——
                #
                #    WAL + 各线程独立连接        落盘 5/8  失败 3
                #    WAL + BEGIN IMMEDIATE      落盘 3/8  失败 5
                #    默认日志 + 各线程独立连接    落盘 8/8  失败 0
                #
                # 报错是 `attempt to write a readonly database`——看着像磁盘权限问题，
                # 其实是 WAL 需要把那对共享内存边车文件（`-wal` / `-shm`）映射到
                # 各个连接，这个环境不支持多连接共享它。
                #
                # WAL 的好处是「读不被写挡住」，可我们这里的事务都在毫秒级、
                # 没有长读，用不上。所以默认日志才是对的选择。
                _bootstrap(conn)
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """一个「开连接 → 干活 → 提交 → 关连接」的上下文。

    注意别写成 `with sqlite3.connect(...) as conn`：那个 `with` **只管事务不管关闭**，
    出了块连接还开着。两件事要一起做，就得自己包一层。
    """
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
# 清洗
# --------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """归一化：去空白、去标点、转小写。**只做机械处理，不做语义**。"""
    return "".join(ch for ch in str(text).strip().lower() if ch not in _STRIP_CHARS)


def _clean_kind(kind: object) -> str:
    return str(kind) if kind in KINDS else DEFAULT_KIND


def _clean_text(text: object) -> str:
    """折叠空白并截断。清洗后为空返回空串，由调用方跳过。"""
    return " ".join(str(text).split())[:MAX_TEXT_LENGTH]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def memories_of(user_id: str) -> list[dict]:
    """读某个用户已有的长期记忆，按写入先后排列。没有就返回空列表。

    `id` 是界面那一行的编号，**和内容无关**（理由见模块开头那个坑）。
    用户点「删除」「改一下措辞」时传回来的就是它。

    `fingerprint` 刻意**不对外**：它是去重用的内部字段，一旦暴露，调用方很容易
    顺手拿它当编号——那就正好把踩过的坑重新挖一遍。
    """
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT id, text, kind, source, created_at, last_seen, hits
            FROM memories WHERE user_id = ? ORDER BY rowid
            """,
            (str(user_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def remember(
    user_id: str,
    facts: list,
    source_thread: str | None = None,
) -> dict[str, int]:
    """把一批事实写进某个用户的长期记忆，返回 `{"added", "merged", "total"}`。

    `facts` 里每项可以是字符串，也可以是 `{"text": ..., "kind": ...}`——
    调用方直接把这层模型的输出原样丢进来即可，形状不对的项会被跳过，
    不让脏数据落盘。这样解析的容错只写在这一处。
    """
    key = str(user_id)
    now = _now()
    added = merged = 0

    with _db() as conn:
        for raw in facts:
            if isinstance(raw, str):
                text, kind = raw, DEFAULT_KIND
            elif isinstance(raw, dict):
                text, kind = raw.get("text", ""), raw.get("kind")
            else:
                continue

            text = _clean_text(text)
            if not text:
                continue
            fingerprint = _normalize(text)
            if not fingerprint:
                continue

            # 先试着插入。撞上 `(user_id, fingerprint)` 的唯一约束就说明这条记过了，
            # 于是改成累加。`INSERT OR IGNORE` 的 rowcount 正好能区分这两种情况，
            # 不用先查一次——先查反而多一趟，而且两次调用之间还有被插队的窗口。
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO memories
                    (user_id, id, text, fingerprint, kind, source,
                     created_at, last_seen, hits)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    key,
                    _new_id(),
                    text,
                    fingerprint,
                    _clean_kind(kind),
                    source_thread,
                    now,
                    now,
                ),
            ).rowcount
            if inserted:
                added += 1
                continue

            # 重复的那条：累加 hits、刷新 last_seen。
            # kind 只在这次明确给了合法值时才覆盖——`COALESCE(NULL, kind)` 会保留原值。
            conn.execute(
                """
                UPDATE memories
                   SET hits = hits + 1, last_seen = ?, kind = COALESCE(?, kind)
                 WHERE user_id = ? AND fingerprint = ?
                """,
                (now, str(kind) if kind in KINDS else None, key, fingerprint),
            )
            merged += 1

        total = _trim(conn, key)

    return {"added": added, "merged": merged, "total": total}


def forget(user_id: str, text: str) -> bool:
    """按文本删掉一条记忆（同样走归一化匹配）。删掉了返回 True。"""
    fingerprint = _normalize(text)
    if not fingerprint:
        return False
    with _db() as conn:
        removed = conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND fingerprint = ?",
            (str(user_id), fingerprint),
        ).rowcount
    return bool(removed)


def forget_by_id(user_id: str, memory_id: str) -> bool:
    """按 `id` 删一条。界面上的「删除这条」走这里。删掉了返回 True。

    和 `forget(text)` 的区别：那边拿一段**原文**再归一化去匹配，适合模型手里
    只有原文的场景；这边直接命中稳定编号，**不做任何归一化**——`id` 是随机值，
    归一化它只会把不同的两个编号弄成同一个。

    名字从 `forget_by_key` 改过来，是因为对外只认 `id` 了（见模块开头的坑）。
    """
    key = str(memory_id).strip()
    if not key:
        return False
    with _db() as conn:
        removed = conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND id = ?",
            (str(user_id), key),
        ).rowcount
    return bool(removed)


def update_text(
    user_id: str,
    memory_id: str,
    text: str,
    kind: object = None,
) -> str:
    """改一条记忆的原文（`kind` 不给就沿用原来的）。按稳定编号 `id` 定位。

    返回 `"ok"` / `"missing"`（没这条）/ `"clash"`（改完和已有条目重了）。
    不用 bool：调用方要区分 404 和 409，混成一个 False 就分不出来了。

    为什么要「读旧 → 删旧 → 插新」：`fingerprint` 是主键，原文一变它就跟着变，
    原地 UPDATE 主键在 SQLite 里等于先删后插，不如写明白。`id`、`created_at`、
    `hits` 都**保留原值**——这条记忆是那时候留下来的、被提过几次，改个措辞
    不该让它变成一条新记忆。`id` 尤其不能换，换了前端手里那个编号就失效了。
    """
    key = str(memory_id).strip()
    cleaned = _clean_text(text)
    if not key or not cleaned:
        return "missing"
    new_fingerprint = _normalize(cleaned)
    if not new_fingerprint:
        return "missing"

    with _db() as conn:
        row = conn.execute(
            "SELECT id, fingerprint, kind, source, created_at, last_seen, hits"
            " FROM memories WHERE user_id = ? AND id = ?",
            (str(user_id), key),
        ).fetchone()
        if row is None:
            return "missing"
        old_fingerprint = row["fingerprint"]

        # 改出来的措辞可能和**另一条**已经重了（比如把「叫我涛哥」改成
        # 「先给结论再展开」，而那条本来就有）。这时不能静默合并——用户以为
        # 自己只是在改措辞，却把两条并成了一条，是不可逆的意外。
        if new_fingerprint != old_fingerprint:
            clash = conn.execute(
                "SELECT 1 FROM memories WHERE user_id = ? AND fingerprint = ?",
                (str(user_id), new_fingerprint),
            ).fetchone()
            if clash is not None:
                return "clash"

        conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND fingerprint = ?",
            (str(user_id), old_fingerprint),
        )
        conn.execute(
            "INSERT INTO memories (user_id, id, text, fingerprint, kind, source,"
            " created_at, last_seen, hits) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(user_id),
                row["id"],  # 沿用旧编号：对外它必须是不变的
                cleaned,
                new_fingerprint,
                _clean_kind(kind) if kind else row["kind"],
                row["source"],
                row["created_at"],
                row["last_seen"],
                row["hits"],
            ),
        )
    return "ok"


def clear(user_id: str) -> int:
    """清空某个用户的长期记忆，返回清掉了多少条。"""
    with _db() as conn:
        return conn.execute(
            "DELETE FROM memories WHERE user_id = ?", (str(user_id),)
        ).rowcount


def _trim(conn: sqlite3.Connection, user_id: str) -> int:
    """超出每用户上限时，丢掉「最久没再出现」的那些。返回剩余条数。

    `rowid` 做次级排序键：同一次调用里写进去的几条 `last_seen` 完全相同，
    没有它「丢哪条」就不确定了。
    """
    total = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    excess = total - MAX_PER_USER
    if excess > 0:
        conn.execute(
            """
            DELETE FROM memories WHERE rowid IN (
                SELECT rowid FROM memories WHERE user_id = ?
                ORDER BY last_seen ASC, rowid ASC LIMIT ?
            )
            """,
            (user_id, excess),
        )
        total -= excess
    return int(total)


def _import_legacy_json(conn: sqlite3.Connection) -> bool:
    """把老版本的 `memories.json` 搬进来。搬了东西返回 True。

    **不做改名**（那是 `_archive_legacy_json` 的事，且必须在提交成功之后）。
    这里只负责读 + 插。`_insert_legacy_entries` 用的是 `INSERT OR IGNORE`，
    所以重复跑（比如改名失败、下次启动再来一遍）不会插出重复行。

    时间戳和 `hits` 都按原样保留，不做二次归一化（那会丢掉历史）。
    """
    if not LEGACY_JSON_PATH.exists():
        return False
    try:
        data = json.loads(LEGACY_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    if not isinstance(data, dict):
        return False

    imported = False
    for user_id, entries in data.items():
        if isinstance(entries, list):
            _insert_legacy_entries(conn, str(user_id), entries)
            imported = True
    return imported


def _archive_legacy_json() -> None:
    """把搬完的老文件改名成 `.migrated`，让它以后不再触发迁移。

    用 `os.replace` 而不是 `Path.rename`：**Windows 上 `rename` 的目标文件已存在
    会直接抛**（`FileExistsError`），而上次跑过一次迁移、`.migrated` 还在，
    是很正常的情况。`os.replace` 两个平台都是「原子覆盖」，没有这个问题。

    覆盖掉旧的 `.migrated` 没有信息损失：那是**同一个已经被导入进库**的 JSON。
    """
    target = LEGACY_JSON_PATH.with_name(LEGACY_JSON_PATH.name + ".migrated")
    try:
        os.replace(LEGACY_JSON_PATH, target)
    except OSError:
        # 另一个进程/线程抢先搬走了。数据已经进库（INSERT OR IGNORE 保证不重复），
        # 这边什么都不用做。
        pass


def _insert_legacy_entries(
    conn: sqlite3.Connection, user_id: str, entries: list
) -> None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = _clean_text(entry.get("text", ""))
        fingerprint = _normalize(text)
        if not text or not fingerprint:
            continue
        now = _now()
        conn.execute(
            """
            INSERT OR IGNORE INTO memories
                (user_id, id, text, fingerprint, kind, source,
                 created_at, last_seen, hits)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                _new_id(),
                text,
                fingerprint,
                _clean_kind(entry.get("kind")),
                entry.get("source"),
                str(entry.get("created_at") or now),
                str(entry.get("last_seen") or now),
                int(entry.get("hits") or 1),
            ),
        )
