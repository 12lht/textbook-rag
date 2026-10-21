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

# 邀请码用的字符集：去掉了 I / L / O / 0 / 1。
#
# 这不是洁癖：邀请码要靠人**手工转发**（微信里发一句、口述给同学），
# 那几个字符在不同字体下会看错，一次输错就白跑。31 个字符取 8 位
# ≈ 8.5×10¹¹ 种，对「发给几个同学」的规模绰绰有余。
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_SEGMENTS = 2       # AW-XXXX-XXXX
INVITE_SEGMENT_LEN = 4

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
-- 邀请码。**code 存明文**，和密码、令牌都不一样，理由见 `create_invite`。
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    note       TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    used_by    TEXT,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_invites_used ON invites(used_by);
"""


class AccountTaken(ValueError):
    """账号已被注册。用异常而不是返回值，是为了让 service 层能一眼看出
    「这是用户输入问题」（→ HTTP 400）而不是系统故障（→ 500）。"""


class InvalidCredentials(ValueError):
    """账号或密码不对。**故意不区分这两者**，否则攻击者可以拿它当
    「这个账号存不存在」的探测器。"""


class InviteRequired(ValueError):
    """这个站已经有人了，注册必须带邀请码。

    和 `InvalidInvite` 分开，是因为对用户来说这是两件事：一个是「你漏填了」，
    另一个是「你填的那个不对」。都回一句「邀请码无效」会让人以为是码错了，
    反复换码试。
    """


class InvalidInvite(ValueError):
    """邀请码不存在，或者已经被别人用掉了。**这两者也不区分**：
    区分了就等于提供了一个「这个码存在吗」的探测器。"""


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
    """两张表都在才算就绪。

    **加了新表就必须把它加进来**：`_bootstrap` 只在「不就绪」时才跑，
    只判 `users` 的话，一个已有的老库永远进不了建表分支，新表建不出来
    （然后每个请求都报 `no such table: invites`）。这和 `memories.py`
    里「加了 `id` 列就要一起改初始化条件」是同一类坑。
    """
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('users', 'invites')"
        )
    }
    return names == {"users", "invites"}


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


def count_users() -> int:
    with _db() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def owner_id() -> str | None:
    """最早注册的那个人，站点的「主人」。没有用户时返回 None。

    **只有这个人能发邀请码。** 不这么做的话邀请制形同虚设：任何一个被邀请
    进来的人都能再拉无限个进来。也不用另设管理员账号或环境变量——
    「第一个注册的人」天然只有一个，不需要额外配置，也不会配错。
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT user_id FROM users ORDER BY created_at ASC, rowid ASC LIMIT 1"
        ).fetchone()
    return row["user_id"] if row else None


# --------------------------------------------------------------------------
# 对外接口：邀请码
# --------------------------------------------------------------------------

def _new_invite_code() -> str:
    part = lambda: "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_SEGMENT_LEN))
    return "AW-" + "-".join(part() for _ in range(INVITE_SEGMENTS))


def normalize_invite(code: object) -> str:
    """统一成大写并去掉空白。用户可能粘贴成 `aw-abcd-efgh` 或带空格。"""
    return "".join(str(code or "").split()).upper()


def create_invite(created_by: str, note: object = None) -> dict:
    """发一个新邀请码，返回 `{"code", "note", "created_at"}`。

    **`code` 存明文**，这和密码（只存哈希）、令牌（只存摘要）的做法相反。
    差别在用途：密码和令牌是「验证你知道某个秘密」，验证完就该忘掉原文；
    邀请码是**要被转发出去的东西**——管理员需要回头查「我发出去哪几个、
    哪个还没用」，存哈希就再也看不到了。而且它一次性、用完即废，
    泄露窗口只到被用掉那一刻，风险比长期凭证小得多。

    重码概率极低（8 位 × 31 字符），撞上了就重试几次——比直接报错友好。
    """
    cleaned_note = " ".join(str(note or "").split())[:80] or None
    now = _stamp(_now())
    for _ in range(8):
        code = _new_invite_code()
        try:
            with _db() as conn:
                conn.execute(
                    "INSERT INTO invites (code, note, created_by, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (code, cleaned_note, str(created_by), now),
                )
            return {"code": code, "note": cleaned_note, "created_at": now}
        except sqlite3.IntegrityError:
            continue  # 撞了已有的码，换一个
    raise RuntimeError("生成邀请码连续撞码，这不该发生")


def list_invites(created_by: str | None = None) -> list[dict]:
    """已发出的邀请码，新的在前。给了 `created_by` 就只看这个人发的。"""
    sql = (
        "SELECT code, note, created_by, created_at, used_by, used_at"
        " FROM invites"
    )
    params: tuple = ()
    if created_by is not None:
        sql += " WHERE created_by = ?"
        params = (str(created_by),)
    sql += " ORDER BY created_at DESC, rowid DESC"
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "code": row["code"],
            "note": row["note"],
            "created_at": row["created_at"],
            "used": row["used_by"] is not None,
            "used_at": row["used_at"],
        }
        for row in rows
    ]


def delete_invite(code: str, created_by: str | None = None) -> bool:
    """删掉一个邀请码。返回它此前是否存在（且是不是他发的）。

    ## 删了就等于作废，不需要额外的「撤销」字段

    `_consume_invite` 是 `UPDATE ... WHERE code = ?`，删掉行之后这条 UPDATE
    的 `rowcount` 自然是 0，注册那边收到的就是 `InvalidInvite`。所以
    「删除」和「撤销」在行为上是同一件事，区别只在于**还查不查得到历史**——
    这里刻意选「查不到」：这张表的用途是「我发出去哪些码、还剩几个能用」，
    不是审计日志，留一堆已作废的行只会让列表越来越长。

    ## 已用掉的码也允许删，但调用方要提示清楚

    已用的码删掉之后，**「这个账号是通过哪个码进来的」就再也查不到了**
    ——`users` 表里没有这一列，`invites.used_by` 是唯一的线索。
    这是个真实的损失，所以界面上删已用的码要单独给一句确认文案。
    **但账号本身不受影响**：注册时 users 那行已经落库，邀请码只是当时的一道门。

    ## 为什么按 `created_by` 过滤

    只按 code 删的话，一个知道别人码的人（码本来就是要转发出去的）就能把
    别人的记录删掉。加上 `created_by` 之后，「不是你的」和「不存在」走同一条
    路径返回 False，外面照样报 404，不会变成「这个码是别人的」的探测器。
    """
    normalized = normalize_invite(code)
    if not normalized:
        return False
    sql = "DELETE FROM invites WHERE code = ?"
    params: tuple = (normalized,)
    if created_by is not None:
        sql += " AND created_by = ?"
        params = (normalized, str(created_by))
    with _db() as conn:
        return conn.execute(sql, params).rowcount > 0


def _consume_invite(conn: sqlite3.Connection, code: str, user_id: str, now: str) -> None:
    """把码标记成「被这个人用掉了」。没抢到就抛 `InvalidInvite`。

    **用一条 UPDATE 的 rowcount 判断，而不是「先查再用」**：先查再用中间有窗口，
    两个并发注册会拿着同一个码双双通过。`WHERE used_by IS NULL` 让这条 UPDATE
    自己变成「抢」，只有一个能抢到。

    调用方必须已经在事务里：注册失败（账号重名）时这行要跟着回滚，
    码才能退回可用状态。
    """
    claimed = conn.execute(
        "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
        (str(user_id), now, code),
    ).rowcount
    if not claimed:
        raise InvalidInvite("邀请码不对，或者已经被用过了")


def register(account: object, password: object, invite: object = None) -> dict:
    """注册，返回新用户（含 `user_id`）。账号重名抛 `AccountTaken`。

    **靠唯一约束判重，不靠「先查再插」**：先查再插中间有一个窗口，
    两个并发注册会同时通过检查。`INSERT` 撞约束才知道，是原子的。

    ## 邀请码

    **第一个用户不需要邀请码，之后都需要。** 这是「先有鸡还是先有蛋」的解法：
    要求所有人持码注册的话，谁去发第一个码？要么预置一个环境变量，要么留一个
    后门接口——两个都比「第一个进来的就是主人」更麻烦、更容易配错。

    冷启动那一刻如果有人并发注册两次，理论上两个都能免码。这是**刻意的取舍**：
    要堵住它得把整个站点的首次启动做成一个带锁的引导流程，而这一刻的真实场景
    是「他一个人第一次点注册」。风险远小于复杂度。
    """
    cleaned = validate_account(account)
    secret = validate_password(password)
    user_id = new_user_id()
    now = _stamp(_now())

    with _db() as conn:
        first_user = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if not first_user:
            code = normalize_invite(invite)
            if not code:
                raise InviteRequired("这个站需要邀请码才能注册")
            _consume_invite(conn, code, user_id, now)

        try:
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
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 注意：这行 raise 退出 `with` 会让整个事务回滚，
            # 上面抢到的邀请码跟着退回可用——不然这次失败会白烧一个码。
            raise AccountTaken("这个账号已经被注册了") from exc

    return {"user_id": user_id, "account": cleaned, "created_at": now}


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
