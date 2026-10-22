"""跨会话的长期记忆存储：按 `user_id` 存「下次换个会话也该记得」的事实。

和 `conversations.py` 一样，这一层是纯应用层：只读写 `data/memories.sqlite3`，
**不 import 任何 agent 相关的东西**。分工是：

- 什么时候记、记什么 —— **模型自己判断**（它手里有 `save_memory` / `recall_memory`）
- 把这两个工具接到这一层 —— `service.py` 通过 `agent_core.set_memory_backend()` 注入
- 存成什么、怎么去重 —— 就在这里


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

LEGACY_JSON_PATH = DATA_DIR / "memories.json"

MAX_TEXT_LENGTH = 120
MAX_PER_USER = 200
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

_init_lock = threading.Lock()



def _schema_ready(conn: sqlite3.Connection) -> bool:
    """表建好了没有。直接问数据库，不在内存里记一个「建过了」的布尔值——记布尔的写法踩过两次：
    探针删掉库文件之后进程还以为表在（报 `no such table`），换库路径也一样。问一次
    `sqlite_master` 只要几微秒。
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        is not None
    )


def _needs_init(conn: sqlite3.Connection) -> bool:
    """要不要跑一次初始化：表不在、缺 `id` 列、或者老 JSON 还没搬走。

    `_connect` 里把这个条件**内联展开**写了（那里要判两次），改的时候两处一起改。留在这里是给
    探针一个「我问的和生产代码问的是同一件事」的入口。
    """
    return (
        LEGACY_JSON_PATH.exists()
        or not _schema_ready(conn)
        or not _has_id_column(conn)
    )


def _new_id() -> str:
    """给一行生成稳定编号，和内容无关，所以改措辞不会换号。

    12 位十六进制 = 48 bit，按 `MAX_PER_USER` 200 条算碰撞概率约 4×10⁻¹²；真撞了也只是那行插
    不进去（`INSERT OR IGNORE`），索引是 `(user_id, id)`，跨用户重号无所谓。
    """
    return uuid.uuid4().hex[:12]


def _has_id_column(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "id" for row in conn.execute("PRAGMA table_info(memories)"))


def _ensure_id_column(conn: sqlite3.Connection) -> None:
    """老库补 `id` 列并回填。**调用方必须已经持有 `_init_lock`。**

    幂等：列在就不再 ALTER，索引是 `IF NOT EXISTS`，回填只挑 id 为空的行。
    """
    if not _has_id_column(conn):
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

    幂等：`CREATE TABLE IF NOT EXISTS` 和 `INSERT OR IGNORE` 重复跑都没有副作用。
    """
    conn.executescript(_SCHEMA)
    _ensure_id_column(conn)
    migrated = _import_legacy_json(conn)
    conn.commit()
    if migrated:
        _archive_legacy_json()


def _connect() -> sqlite3.Connection:
    """开一条新连接，需要时把库初始化好。

    **每次都用新连接、用完就关**：这一层的调用发生在工作线程里（工具是同步函数，ToolNode 把
    它们丢进线程池），而 sqlite3 连接默认不许跨线程复用。开一条连接只要几十微秒。

    并发写由 SQLite 自己串行化（`timeout=5.0` 是等锁上限）。**唯一要自己管的是库文件还不存在
    的时候**：`connect()` 遇到不存在的路径会顺手建文件，而这个「建」不是原子的——8 个线程并发
    只有 5~7 个成功，报 `attempt to write a readonly database` 或 `PermissionError`（都跟权限
    无关，根因是多个连接同时创建同一个文件；异常在第一条写语句 `CREATE TABLE` 那行才抛）。

    做法：判文件在不在，不在就进锁、再判一次、建好再出来。热路径完全不碰锁。
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
    if (
        LEGACY_JSON_PATH.exists()
        or not _schema_ready(conn)
        or not _has_id_column(conn)
    ):
        with _init_lock:
            if (
                LEGACY_JSON_PATH.exists()
                or not _schema_ready(conn)
                or not _has_id_column(conn)
            ):
                _bootstrap(conn)
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """一个「开连接 → 干活 → 提交 → 关连接」的上下文。

    别写成 `with sqlite3.connect(...) as conn`：那个 `with` **只管事务不管关闭**，出了块连接还开着。
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



def memories_of(user_id: str) -> list[dict]:
    """读某个用户已有的长期记忆，按写入先后排列，没有就返回空列表。

    `id` 是界面那一行的编号、**和内容无关**（用户点删除或改措辞时传回来的就是它）。
    `fingerprint` 刻意**不对外**：它是去重用的内部字段，一旦暴露就会被顺手当编号用。
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

    `facts` 每项可以是字符串或 `{"text": ..., "kind": ...}`——调用方把模型输出原样丢进来即可，
    形状不对的项跳过，不让脏数据落盘。解析的容错只写在这一处。
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
    """按 `id` 删一条，删掉了返回 True。界面上的「删除这条」走这里。

    和 `forget(text)` 的区别：那边拿原文归一化后匹配（适合模型手里只有原文），这边直接命中稳定
    编号、**不做任何归一化**——`id` 是随机值，归一化只会把两个不同的编号弄成同一个。
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
    """改一条记忆的原文（`kind` 不给就沿用原来的），按稳定编号 `id` 定位。

    返回 `"ok"` / `"missing"` / `"clash"`（改完和已有条目重了）——不用 bool，调用方要区分 404 和 409。

    「读旧 → 删旧 → 插新」是因为 `fingerprint` 是主键，原文一变它就跟着变，原地 UPDATE 主键在
    SQLite 里等于先删后插。`id`、`created_at`、`hits` 都保留原值：改措辞不该让它变成一条新记忆，
    `id` 换了前端手里的编号就失效。
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
    """超出每用户上限时，丢掉「最久没再出现」的那些，返回剩余条数。

    `rowid` 做次级排序键：同一次调用里写进去的几条 `last_seen` 完全相同，没有它「丢哪条」就不确定。
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
    """把老版本的 `memories.json` 搬进来，搬了东西返回 True。

    **不做改名**（那是 `_archive_legacy_json` 的事，且必须在提交成功之后）。插入用
    `INSERT OR IGNORE`，重复跑（比如改名失败、下次启动再来一遍）不会插出重复行。
    时间戳和 `hits` 按原样保留，不做二次归一化。
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

    用 `os.replace` 而不是 `Path.rename`：Windows 上 rename 的目标已存在会直接抛
    `FileExistsError`，而「上次迁移成功、`.migrated` 还在」是正常情况。覆盖旧的没有信息损失。
    """
    target = LEGACY_JSON_PATH.with_name(LEGACY_JSON_PATH.name + ".migrated")
    try:
        os.replace(LEGACY_JSON_PATH, target)
    except OSError:
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
