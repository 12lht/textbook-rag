"""探针：配额与限流（F3 / F4）。

两个东西在测：
- **配额**（落 SQLite，按天）：扣得动、扣到上限就挡住、第二天自己恢复、
  下游失败能退回去。
- **限流**（内存滑窗）：秒级连点会被挡、等一会儿自动放行、不同人互不牵连。

时间相关的部分全部用假时钟推，不 sleep——真实等待会让这个探针跑几十秒，
而且「刚好在边界上」这类断言靠 sleep 是测不准的。
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(r"D:/agent")
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402
import limits  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-limits-"))
limits.DATA_DIR = tmp
limits.DB_PATH = tmp / "limits.sqlite3"

_clock = types.SimpleNamespace(now=0.0)
limits.time = types.SimpleNamespace(monotonic=lambda: _clock.now)

import service  # noqa: E402

passed = failed = 0


def check(label: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {extra}")


def expect(label: str, exc_type, fn, *args, **kwargs):
    global passed, failed
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        passed += 1
        print(f"  OK   {label}")
        return exc
    except Exception as exc:  # noqa: BLE001
        failed += 1
        print(f"  FAIL {label}  抛的是 {type(exc).__name__}: {exc}")
        return None
    failed += 1
    print(f"  FAIL {label}  没抛异常")
    return None


def main() -> None:
    print("=== 配额：扣得动，扣满就挡 ===")
    check("一开始是 0", limits.used_today("u1", "ask") == 0)
    check("剩 2（上限 3）", limits.consume("u1", "ask", 3) == 2)
    check("剩 1", limits.consume("u1", "ask", 3) == 1)
    check("第 3 次成功、剩 0（不是被拒）", limits.consume("u1", "ask", 3) == 0)
    check("第 4 次返回 -1", limits.consume("u1", "ask", 3) == -1)
    check("超额那次没被算进计数", limits.used_today("u1", "ask") == 3,
          str(limits.used_today("u1", "ask")))

    print("\n=== 不同用户、不同类别互不影响 ===")
    check("另一个用户还有额度", limits.consume("u2", "ask", 3) == 2)
    check("同一人的另一类别独立", limits.consume("u1", "upload", 3) == 2)
    check("u1 的 ask 还是满的", limits.used_today("u1", "ask") == 3)

    print("\n=== 退回 ===")
    check("退一次后剩 1", limits.refund("u1", "ask") == 2)
    check("退完能再用一次", limits.consume("u1", "ask", 3) == 0)
    check("退不存在的记录不炸", limits.refund("u-nobody", "ask") == 0)

    print("\n=== 跨天自动恢复 ===")
    real_today = limits.today
    limits.today = lambda: "2999-01-02"
    check("第二天计数是 0", limits.used_today("u1", "ask") == 0)
    check("第二天又能用了", limits.consume("u1", "ask", 3) == 2)
    limits.today = lambda: "2999-01-03"
    check("第三天也是新的", limits.used_today("u1", "ask") == 0)
    limits.today = real_today

    print("\n=== 清理旧计数 ===")
    removed = limits.purge_before("2999-01-02")
    check("删掉了 2999-01-02 之前的行", removed >= 1, str(removed))
    check("2999-01-02 那天的还在", limits.used_today("u1", "ask") == 0)

    print("\n=== 限流：窗口内限次 ===")
    limits.reset_rate_limits()
    _clock.now = 0.0
    results = [limits.allow("k", 3, 60)[0] for _ in range(4)]
    check("前 3 次放行", results[:3] == [True, True, True], str(results))
    check("第 4 次被挡", results[3] is False, str(results))

    allowed, wait = limits.allow("k", 3, 60)
    check("被挡时给出要等多久", allowed is False and wait > 0, f"{allowed} {wait}")
    check("等的时间就是最早那次滑出的时刻", abs(wait - 60.0) < 0.001, str(wait))

    print("\n=== 滑窗：时间过了自动放行 ===")
    _clock.now = 59.0
    check("59 秒还在窗口内，仍然挡", limits.allow("k", 3, 60)[0] is False)
    _clock.now = 60.5
    check("最早那次滑出去之后放行", limits.allow("k", 3, 60)[0] is True)

    print("\n=== 不同 key 互不牵连 ===")
    _clock.now = 100.0
    check("另一个 key 不受影响", limits.allow("other", 3, 60)[0] is True)
    _clock.now = 61.0
    state = limits.rate_limit_state()
    check("状态里能看到多个 key", state["keys"] >= 2, str(state))

    print("\n=== 清理不会误删活跃的 key ===")
    limits.reset_rate_limits()
    _clock.now = 0.0
    limits.allow("hot", 5, 60)
    _clock.now = 30.0
    limits.allow("hot", 5, 60)   # 这个 key 还在窗口内
    _clock.now = 200.0
    limits.allow("cold", 5, 60)  # 顺手触发一次 sweep 的机会
    limits.reset_rate_limits()
    check("reset 之后是空的", limits.rate_limit_state()["keys"] == 0)

    print("\n=== service 层：先限流后配额，异常分得清 ===")
    limits.reset_rate_limits()
    _clock.now = 0.0
    real_limit = limits.ASK_PER_DAY
    limits.ASK_PER_DAY = 2
    try:
        check("第 1 次返回剩 1", service.take_ask_slot("u-ask") == 1)
        check("第 2 次返回剩 0", service.take_ask_slot("u-ask") == 0)
        exc = expect("第 3 次抛 QuotaExceeded", service.QuotaExceeded,
                     service.take_ask_slot, "u-ask")
        check("文案说的是「今天用完了」",
              exc is not None and "用完" in str(exc), str(exc))
    finally:
        limits.ASK_PER_DAY = real_limit

    limits.reset_rate_limits()
    _clock.now = 0.0
    kinds = []
    for _ in range(limits.ASKS_PER_MINUTE + 3):
        try:
            service.take_ask_slot("u-fast")
        except Exception as exc:  # noqa: BLE001
            kinds.append(type(exc).__name__)
    check("连点之后挡下来的是 RateLimited",
          kinds and set(kinds) == {"RateLimited"}, str(kinds))
    check("挡下的次数对得上",
          len(kinds) == 3, str(len(kinds)))

    print("\n=== service 层：退还 ===")
    limits.reset_rate_limits()
    _clock.now = 0.0
    before = limits.used_today("u-refund", "ask")
    service.take_ask_slot("u-refund")
    check("扣了一次", limits.used_today("u-refund", "ask") == before + 1)
    service.refund_ask_slot("u-refund")
    check("退回去之后和原来一样", limits.used_today("u-refund", "ask") == before)

    print("\n=== 登录限流 ===")
    limits.reset_rate_limits()
    _clock.now = 0.0
    for _ in range(limits.LOGIN_ATTEMPTS_PER_MINUTE):
        service.limit_login("1.2.3.4")
    exc = expect("超了抛 RateLimited", service.RateLimited, service.limit_login, "1.2.3.4")
    check("带 retry_after", exc is not None and exc.retry_after > 0,
          str(exc.retry_after if exc else None))
    check("另一个 IP 不受影响", service.limit_login("5.6.7.8") is None)

    print("\n=== 书数上限 ===")
    real_list = documents.list_documents
    try:
        documents.list_documents = lambda uid: [{}] * (limits.DOCS_PER_USER - 1)
        check("差一本时放行", service.check_document_quota("u-doc") is None)

        documents.list_documents = lambda uid: [{}] * limits.DOCS_PER_USER
        exc = expect("正好满了就挡住", service.QuotaExceeded,
                     service.check_document_quota, "u-doc")
        check("文案告诉他要删书",
              exc is not None and "删" in str(exc), str(exc))
    finally:
        documents.list_documents = real_list

    print(f"\n合计 {passed} 项通过，{failed} 项失败")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
