"""账号与登录：注册、校验密码、签发和撤销登录令牌。

和 `conversations.py` / `memories.py` 一样是**纯应用层**：只读写
`data/users.sqlite3`，不 import 任何 agent 相关的东西。

## 两张表

```sql
users    (user_id, account, account_key, password_hash, created_at)
sessions (token_hash, user_id, created_at, expires_at)
```

- `account` 是用户填的原文（保留大小写，界面显示用）；
  `account_key` 是**归一化后**的值（去首尾空白 + 转小写），唯一约束加在它上面。
  分开存是因为「显示」和「判重」要的东西不一样——用户叫 `TaoGe` 就该显示 `TaoGe`，
  但注册 `taoge` 必须被挡住。
- `user_id` 是**与账号无关的不透明 id**（`u-xxxxxxxxxx`）。不拿账号当主键，
  因为账号以后可能允许改，而"这个人的历史数据"不该跟着变。

## 密码怎么存

用标准库的 `hashlib.pbkdf2_hmac`（OpenSSL 实现，纯 C，够快），存成**带版本前缀**的
字符串：

```text
pbkdf2_sha256$600000$<salt_b64>$<hash_b64>
```

三个理由：

1. **不存明文、不做可逆加密**。加密意味着有密钥，而密钥就在同一个仓库里，
   等于没加密。密码只做单向哈希。
2. **每个用户一个随机 salt**。不然两个人密码相同就是同一个哈希值，
   一眼能看出来，而且彩虹表直接通用。
3. **迭代次数写进字符串**。以后要调高（机器变快、或嫌慢），老记录仍然能按它自己
   记的次数验证通过，不需要强制所有人改密码。`.` 校验时**从存储值里读参数**，
   不用当前的全局常量——这是版本前缀的全部意义。

`salt` 和 `hash` 用 base64 存，避免二进制塞进 TEXT 出乱码。

## 令牌为什么也要哈希后存

登录成功后签发的令牌是 `secrets.token_urlsafe(32)`（约 256 位随机）。
**库里只存它的 sha256 摘要**，原文只在那一次响应里返回给客户端。

理由和密码一样但**不需要 salt 和迭代**：令牌本身就是高熵随机值，
猜不出来，也就不怕彩虹表——只需要防止「库被看到就等于拿到一批可用令牌」。

## 每次校验身份**不写库**

`resolve_token` 是纯读。想在这里顺手刷新 `last_seen` / 滑动延长过期时间的话，
每个 API 请求都会多一次写事务——而实测一次提交在这个环境要 ~30ms
（成本全在回滚日志建删文件，见 `memories.py` 的实测表）。所以：

- 过期用**绝对时间**（登录时定死 30 天），不做滑动续期；
- `last_seen` 只在登录那一刻记一次。

要滑动续期的话，得改成「距上次刷新超过 N 分钟才写」，别每请求都写。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "users.sqlite3"

# 账号：字母（含中文）、数字、下划线、点、@、加号、减号。
# 这样一个字段就能同时接住用户名 / 手机号 / 邮箱，不用让用户先选「用哪种登录」。
_ACCOUNT_RE = re.compile(r"^[\w.@+\-]+$")
ACCOUNT_MIN = 3
ACCOUNT_MAX = 32

# 密码：只限长度。不强制「必须含大小写和符号」——那种规则逼出来的是
# `Password1!` 这种更差的密码，长度才是真正有用的约束。
PASSWORD_MIN = 8
PASSWORD_MAX = 128

# 令牌有效期。绝对时间，不做滑动续期（理由见模块开头）。
SESSION_DAYS = 30

# PBKDF2 迭代次数。OWASP 对 PBKDF2-HMAC-SHA256 的建议值是 600000。
PBKDF2_ITERATIONS = 600_000
_HASH_ALGORITHM = "pbkdf2_sha256"

# 未登录用户共用的**默认账号**。游客不发令牌，服务端就把他们当成这个 user_id。
#
# 两个后果，都写在这里免得以后忘：
# 1. **游客之间不隔离**——他们共用会话列表。这是加登录之前就有的行为
#    （那会儿所有请求都硬编码 `user_id = "1"`），不是本次疏忽。要隔离只能
#    给游客发匿名令牌，那是另一件事。
# 2. **这个账号必须关掉长期记忆**（在 `service.py` 里）。否则几个游客
#    会互相读到对方记下的东西——那就不只是「看到会话」，而是「看到别人的偏好」。
GUEST_USER_ID = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    account       TEXT NOT NULL,
    account_key   TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
-- 退出所有设备时要按 user_id 批量删
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class AccountTaken(ValueError):
    """账号已被注册。用异常而不是返回值，是为了让 service 层能一眼看出
    「这是用户输入问题」（→ HTTP 400）而不是系统故障（→ 500）。"""


class InvalidCredentials(ValueError):
    """账号或密码不对。**故意不区分这两者**，否则攻击者可以拿它当
    「这个账号存不存在」的探测器。"""


# **初始化的**锁，和数据无关。这段和 `memories.py` 里的是同一套，理由也一样：
# `sqlite3.connect()` 遇到不存在的路径会顺手建文件，而这个「建」不是原子的——
# 几个线程同时来创建同一个文件，有的会拿到只读句柄，报
# `attempt to write a readonly database`（看着像权限问题，其实不是）。
# 实测（`memories.py` 里记着）：冷启动 8 线程并发 → 落盘 5~7/8；
# 先把文件建出来再并发 → 8/8。所以把「冷启动的连接」串起来就行。
#
# 注意本文件里还有一份**同样的**逻辑。两份要一起改——真要彻底避免，
# 该把它抽成一个共用的 SQLite 小工具模块，那是独立的一件事。
_init_lock = threading.Lock()


# --------------------------------------------------------------------------
# 连接（与 memories.py 同一套，见上面的注释）
# --------------------------------------------------------------------------

def _schema_ready(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        is not None
    )


def _bootstrap(conn: sqlite3.Connection) -> None:
    """建表。调用方必须已经持有 `_init_lock`。幂等，重复跑只是白干。"""
    conn.executescript(_SCHEMA)
    conn.commit()


def _connect() -> sqlite3.Connection:
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
    if not _schema_ready(conn):
        with _init_lock:
            if not _schema_ready(conn):
                _bootstrap(conn)
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """开连接 → 干活 → 提交 → 关连接。

    别写成 `with sqlite3.connect(...) as conn`——那个 `with` 只管事务不管关闭。
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
# 清洗与校验（业务规则只写在这一处）
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now().astimezone()


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def normalize_account(account: object) -> str:
    """归一化成判重用的键：去首尾空白 + 转小写。

    只做这两件事，是因为它们**不会改变用户的意图**：`TaoGe ` 和 `taoge` 显然是同一个人。
    不做「去掉中间的点」「去掉 +86」这类聪明处理——那是猜，猜错了会把两个不同的人
    合并成一个。
    """
    return str(account or "").strip().lower()


def validate_account(account: object) -> str:
    """校验并返回清洗后的账号（保留大小写）。不合规抛 ValueError。"""
    cleaned = str(account or "").strip()
    if not cleaned:
        raise ValueError("账号不能为空")
    if len(cleaned) > ACCOUNT_MAX:
        raise ValueError(f"账号不能超过 {ACCOUNT_MAX} 个字符")
    if len(cleaned) < ACCOUNT_MIN:
        raise ValueError(f"账号至少 {ACCOUNT_MIN} 个字符")
    if not _ACCOUNT_RE.match(cleaned):
        raise ValueError("账号只能包含字母、数字、下划线、点、@、加号和减号")
    return cleaned


def validate_password(password: object) -> str:
    """校验并返回原样的密码。不合规抛 ValueError。

    **不做任何清洗**：密码里的空格、首尾空白都是密码的一部分，
    `strip()` 一下就会让「用户以为自己设了什么」和「实际存了什么」对不上。
    """
    raw = str(password or "")
    if len(raw) < PASSWORD_MIN:
        raise ValueError(f"密码至少 {PASSWORD_MIN} 位")
    if len(raw) > PASSWORD_MAX:
        raise ValueError(f"密码不能超过 {PASSWORD_MAX} 位")
    return raw


# --------------------------------------------------------------------------
# 密码哈希
# --------------------------------------------------------------------------

def _hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([
        _HASH_ALGORITHM,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def _verify_password(password: str, stored: str) -> bool:
    """按**存储值里记的参数**验证，不用当前的全局常量（版本前缀的意义所在）。"""
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != _HASH_ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        iterations = int(iterations)
    except (ValueError, TypeError):
        # 存储值被改坏了。当成「验证失败」，不要抛——否则一个坏行能让登录接口 500。
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    # 常量时间比较：普通的 `==` 会在第一个不同的字节处提前返回，
    # 理论上可以被用来一个字节一个字节地试探摘要。
    return hmac.compare_digest(actual, expected)


# 给「账号不存在」时用的陪跑哈希，见 `authenticate`。
_DUMMY_HASH = _hash_password("not-a-real-password")


# --------------------------------------------------------------------------
# 对外接口：账号
# --------------------------------------------------------------------------

def _row_to_user(row: sqlite3.Row) -> dict:
    return {
        "user_id": row["user_id"],
        "account": row["account"],
        "created_at": row["created_at"],
    }


def new_user_id() -> str:
    return f"u-{uuid.uuid4().hex[:10]}"


def get_user(user_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT user_id, account, created_at FROM users WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    return _row_to_user(row) if row else None


def register(account: object, password: object) -> dict:
    """注册，返回新用户（含 `user_id`）。账号重名抛 `AccountTaken`。

    **靠唯一约束判重，不靠「先查再插」**：先查再插中间有一个窗口，
    两个并发注册会同时通过检查。`INSERT` 撞约束才知道，是原子的。
    """
    cleaned = validate_account(account)
    secret = validate_password(password)
    user_id = new_user_id()
    try:
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, account, account_key, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    cleaned,
                    normalize_account(cleaned),
                    _hash_password(secret),
                    _stamp(_now()),
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise AccountTaken("这个账号已经被注册了") from exc
    return {"user_id": user_id, "account": cleaned, "created_at": _stamp(_now())}


def authenticate(account: object, password: object) -> dict:
    """验证账号密码，成功返回用户。失败一律抛 `InvalidCredentials`。

    **账号不存在时也要跑一次哈希**：否则「不存在的账号」会立刻返回，
    「存在的账号但密码错」要花 0.2 秒算 PBKDF2——这个时间差就是一个
    「该账号已注册」的探测器。拿 `_DUMMY_HASH` 陪跑，把两条路的时间拉平。
    """
    key = normalize_account(account)
    raw = str(password or "")
    with _db() as conn:
        row = conn.execute(
            """
            SELECT user_id, account, created_at, password_hash
            FROM users WHERE account_key = ?
            """,
            (key,),
        ).fetchone()

    if row is None:
        _verify_password(raw, _DUMMY_HASH)
        raise InvalidCredentials("账号或密码不对")
    if not _verify_password(raw, row["password_hash"]):
        raise InvalidCredentials("账号或密码不对")
    return _row_to_user(row)


# --------------------------------------------------------------------------
# 对外接口：登录令牌
# --------------------------------------------------------------------------

def _token_fingerprint(token: str) -> str:
    """令牌在库里的表示。不是为了让令牌「更难猜」（它本来就猜不到），
    只是为了「库被人看到」不等于「拿到一批能用的令牌」。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: str) -> dict:
    """签发一个登录令牌，返回 `{"token", "expires_at"}`。**原文只在这一刻存在。**"""
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(days=SESSION_DAYS)
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (_token_fingerprint(token), str(user_id), _stamp(now), _stamp(expires)),
        )
    return {"token": token, "expires_at": _stamp(expires)}


def resolve_token(token: object) -> dict | None:
    """令牌 → 用户。无效、已撤销、已过期都返回 `None`。**纯读，不写库。**

    过期判断在 SQL 里做（`expires_at > ?`），不在 Python 里比——
    时间戳是我们自己用 `isoformat()` 写的，同格式字符串可以直接比大小，
    而放在 SQL 里能顺便用上主键索引。
    """
    raw = str(token or "").strip()
    if not raw:
        return None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT u.user_id, u.account, u.created_at
            FROM sessions s JOIN users u ON u.user_id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (_token_fingerprint(raw), _stamp(_now())),
        ).fetchone()
    return _row_to_user(row) if row else None


def revoke_token(token: object) -> bool:
    """撤销一个令牌（登出）。撤掉了返回 True。"""
    raw = str(token or "").strip()
    if not raw:
        return False
    with _db() as conn:
        removed = conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (_token_fingerprint(raw),)
        ).rowcount
    return bool(removed)


def revoke_all_sessions(user_id: str) -> int:
    """撤销这个用户的全部登录（改密码、或「退出所有设备」时用）。"""
    with _db() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE user_id = ?", (str(user_id),)
        ).rowcount


def purge_expired_sessions() -> int:
    """清掉已过期的令牌行。过期令牌本来就用不了，这只是防止表无限长大。

    没有定时任务，所以由调用方在顺手的地方触发（比如启动时）。
    """
    with _db() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (_stamp(_now()),)
        ).rowcount
