"""探针：账号与登录存储层（`users.py`）——零模型调用。

这一层管的是「谁能进」和「密码怎么存」，出错的方式比记忆层严重得多
（记忆记错了只是记错，身份判错了是别人进了别人的数据），所以单独盯。

覆盖：注册与判重、清洗与校验、密码哈希（不存明文 / 每人不同 salt / 版本前缀）、
身份验证（三条失败路径时间要拉平）、令牌（哈希后存 / 撤销 / 过期 / 批量撤销）、
并发注册同一账号、以及 PBKDF2 到底要多久（决定迭代次数选得对不对）。

只写 .workbuddy/tmp/ 下的临时库，不碰 data/users.sqlite3。
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import users

HERE = Path(__file__).with_name
RUN = uuid.uuid4().hex[:8]
_created: list[Path] = []
RESULTS: list[bool] = []


def fresh_db() -> None:
    """换一个全新的库路径（不删旧文件：沙箱对 unlink 有拦截，换路径更干净）。"""
    path = HERE(f"probe-users-{RUN}-{len(_created)}.sqlite3")
    _created.append(path)
    users.DB_PATH = path


def check(label: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append(bool(condition))
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return bool(condition)


def add_user(owner_id: str, account: str, password: str) -> dict:
    """在有主人的站上再注册一个人：走真实的「发码 → 持码注册」流程。

    F1 之后 `users.register` 的第二个用户起就必须要码，探针里凡是「再建一个人」
    的地方都得走这一步，不能再直接 `users.register(...)`。
    """
    code = users.create_invite(owner_id)["code"]
    return users.register(account, password, code)


def test_register() -> None:
    print("== 1. 注册、判重、校验 ==")
    fresh_db()

    user = users.register("TaoGe", "correct-horse")
    print(f"   register -> {user}")
    check("拿到 user_id 且形如 u-xxxx", user["user_id"].startswith("u-"))
    check("账号原文保留大小写", user["account"] == "TaoGe", user["account"])
    check("能按 user_id 查回来", users.get_user(user["user_id"]) is not None)

    for bad, why in [
        ("taoge", "大小写不同也算重名"),
        (" TAOGE ", "首尾空白和大小写都归一化"),
    ]:
        # 站里已经有人了，注册必须持码（F1）。每次发一个新的——失败那次会把码
        # 一起回滚，所以不能复用同一个。
        code = users.create_invite(user["user_id"])["code"]
        try:
            users.register(bad, "correct-horse", code)
            check(f"重名被挡住（{why}）", False, "居然注册成功了")
        except users.AccountTaken:
            check(f"重名被挡住（{why}）", True)

    for bad_call, why in [
        (("ab", "correct-horse"), "账号太短"),
        (("a" * 33, "correct-horse"), "账号太长"),
        (("tao ge", "correct-horse"), "账号含空格"),
        (("taoge", "1234567"), "密码太短"),
        (("taoge", "x" * 129), "密码太长"),
        (("", "correct-horse"), "账号为空"),
    ]:
        args, _ = bad_call, why
        try:
            users.register(*args)
            check(f"{why}被挡住", False, "居然注册成功了")
        except ValueError:
            check(f"{why}被挡住", True)
    print()


def test_password_storage() -> None:
    print("== 2. 密码怎么存 ==")
    fresh_db()
    secret = "my-Secret-Password-42"
    user = users.register("alice", secret)

    with users._db() as conn:
        stored = conn.execute(
            "SELECT password_hash FROM users WHERE user_id = ?", (user["user_id"],)
        ).fetchone()["password_hash"]

    algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
    print(f"   存的样子：{algorithm}${iterations}$<salt>${'<hash>'}")
    check("库里不含明文密码", secret not in stored)
    check("是版本化格式（4 段）", stored.count("$") == 3)
    check("算法名对得上", algorithm == "pbkdf2_sha256")
    check("迭代次数按配置写进去", int(iterations) == users.PBKDF2_ITERATIONS,
          f"{iterations} 次")

    other = add_user(user["user_id"], "bob", secret)
    with users._db() as conn:
        other_stored = conn.execute(
            "SELECT password_hash FROM users WHERE user_id = ?", (other["user_id"],)
        ).fetchone()["password_hash"]
    check("同密码不同用户 salt 不同", other_stored != stored)
    check("hash 部分也不同", other_stored.split("$")[3] != digest_b64)

    # 版本前缀的意义：老记录按它自己记的迭代次数验证
    weak = users._hash_password(secret, iterations=1000)
    check("低迭代次数的老记录仍能验证（不强制改密码）", users._verify_password(secret, weak))
    check("存储值被改坏时返回 False 而不是抛异常", users._verify_password(secret, "junk") is False)
    print()


def test_authenticate() -> None:
    print("== 3. 验证身份 ==")
    fresh_db()
    user = users.register("carol", "correct-horse")
    ok = users.authenticate("carol", "correct-horse")
    check("正确密码通过", ok["user_id"] == user["user_id"])
    check("账号大小写不敏感", users.authenticate("CAROL", "correct-horse")["user_id"] == user["user_id"])
    check("首尾空白也容忍", users.authenticate("  carol  ", "correct-horse")["user_id"] == user["user_id"])

    for account, password, why in [
        ("carol", "wrong-password", "密码错"),
        ("nobody", "correct-horse", "账号不存在"),
    ]:
        try:
            users.authenticate(account, password)
            check(f"{why}时抛 InvalidCredentials", False, "居然通过了")
        except users.InvalidCredentials as exc:
            check(f"{why}时抛 InvalidCredentials", True, str(exc))

    # 三条路的时间要接近，否则「耗时」本身就成了「账号存不存在」的探测器
    def time_of(account: str, password: str) -> float:
        start = time.perf_counter()
        try:
            users.authenticate(account, password)
        except users.InvalidCredentials:
            pass
        return (time.perf_counter() - start) * 1000

    exists = time_of("carol", "wrong-password")
    missing = time_of("nobody", "wrong-password")
    print(f"   密码错（账号存在）{exists:.0f}ms   账号不存在 {missing:.0f}ms")
    check(
        "两条失败路径耗时同量级（差距 < 3 倍）",
        max(exists, missing) / max(min(exists, missing), 0.001) < 3,
        f"{exists:.0f}ms vs {missing:.0f}ms",
    )
    print()


def test_sessions() -> None:
    print("== 4. 登录令牌 ==")
    fresh_db()
    user = users.register("dave", "correct-horse")
    session = users.create_session(user["user_id"])
    token = session["token"]
    print(f"   令牌长度 {len(token)} 字符，形如 {token[:12]}…")

    check("能解析回同一个用户", users.resolve_token(token)["user_id"] == user["user_id"])
    check("瞎编的令牌解析不出来", users.resolve_token("garbage") is None)
    check("空令牌解析不出来", users.resolve_token("") is None)
    check("None 也不炸", users.resolve_token(None) is None)

    with users._db() as conn:
        rows = conn.execute("SELECT token_hash FROM sessions").fetchall()
    check("库里不含令牌原文", all(token not in r["token_hash"] for r in rows))
    check("存的是 64 位十六进制摘要", len(rows[0]["token_hash"]) == 64)

    revoked = users.create_session(user["user_id"])
    check("撤销成功返回 True", users.revoke_token(revoked["token"]) is True)
    check("撤销后立刻失效", users.resolve_token(revoked["token"]) is None)
    check("撤销不存在的令牌返回 False", users.revoke_token("nope") is False)
    check("撤销一个不影响另一个", users.resolve_token(token) is not None)

    # 过期：把 expires_at 改到过去
    with users._db() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            ((datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds"),
             users._token_fingerprint(token)),
        )
    check("过期令牌解析不出来", users.resolve_token(token) is None)

    fresh = users.create_session(user["user_id"])
    users.create_session(user["user_id"])
    removed = users.revoke_all_sessions(user["user_id"])
    print(f"   退出所有设备：删掉 {removed} 条")
    check("批量撤销清空该用户全部令牌", users.resolve_token(fresh["token"]) is None)
    print()


def test_concurrent_register() -> None:
    print("== 5. 并发注册同一个账号（唯一约束兜住） ==")
    fresh_db()
    # 站里先站一个人当主人，之后注册都要码。**每个线程发自己那一个**——
    # 同一个码只能被一个人用，共用的话这场并发测的就变成「抢码」了。
    owner = users.register("owner", "correct-horse")
    count = 8
    codes = [users.create_invite(owner["user_id"], f"worker-{i}")["code"] for i in range(count)]
    barrier = threading.Barrier(count)
    ok, taken, other = [], [], []

    def worker(index: int) -> None:
        barrier.wait()
        try:
            users.register("same-account", "correct-horse", codes[index])
            ok.append(index)
        except users.AccountTaken:
            taken.append(index)
        except BaseException as exc:  # noqa: BLE001
            other.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(f"   成功 {len(ok)} / 判重 {len(taken)} / 其它异常 {len(other)}  {other[:1]}")
    check("只成功一次", len(ok) == 1, f"{len(ok)} 次")
    check("其余都被判重挡住（不是崩掉）", len(taken) == count - 1 and not other)

    with users._db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    check("库里只有两行（主人 + 注册成功的那一个）", total == 2, f"{total} 行")

    # 那 7 次失败的注册各自抢到过一个码，事务回滚之后必须全部退回可用状态。
    # 漏了这一步的话，一次手滑（账号打错）就白烧一个邀请码。
    unused = [i for i in users.list_invites() if not i["used"]]
    check("失败的注册没烧掉邀请码", len(unused) == count - 1, f"{len(unused)} 个还可用")
    print()


def test_timing() -> None:
    print("== 6. PBKDF2 代价（决定迭代次数选得对不对） ==")
    fresh_db()
    rounds = 5

    start = time.perf_counter()
    for _ in range(rounds):
        users._hash_password("correct-horse")
    hash_ms = (time.perf_counter() - start) / rounds * 1000

    user = users.register("timing", "correct-horse")
    session = users.create_session(user["user_id"])

    start = time.perf_counter()
    for _ in range(20):
        users.resolve_token(session["token"])
    resolve_ms = (time.perf_counter() - start) / 20 * 1000

    print(f"   算一次密码哈希 {hash_ms:.0f}ms（迭代 {users.PBKDF2_ITERATIONS} 次）")
    print(f"   校验一次令牌 {resolve_ms:.1f}ms（纯读，不写库）")
    check("登录延迟在可接受范围（< 1 秒）", hash_ms < 1000, f"{hash_ms:.0f}ms")
    check("每次请求的令牌校验足够便宜（< 50ms）", resolve_ms < 50, f"{resolve_ms:.1f}ms")
    print()


def cleanup() -> None:
    for path in _created:
        for suffix in ("", "-wal", "-shm", "-journal"):
            side = Path(str(path) + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass


def main() -> int:
    try:
        test_register()
        test_password_storage()
        test_authenticate()
        test_sessions()
        test_concurrent_register()
        test_timing()
    finally:
        cleanup()

    passed = sum(RESULTS)
    print("结果:", f"{passed}/{len(RESULTS)} 通过" if all(RESULTS) else f"有失败（{passed}/{len(RESULTS)}）")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
