"""探针：邀请制注册（F1）——第一人免码、之后必须持码、码一次性、失败不烧码。

跑在临时库上，不碰生产账号。用 `service` 而不是直接调 `users`，
因为「谁能发码」这条规则就写在 service 里——绕过它就测不到。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT))

import users  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-invite-"))
users.DATA_DIR = tmp
users.DB_PATH = tmp / "users.sqlite3"

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
    """期望抛某个异常，返回异常本身（好读文案）。"""
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
    print("=== 空库：第一个用户不用邀请码 ===")
    check("一开始 needs_invite=False", service.registration_policy()["needs_invite"] is False)
    check("一开始没有主人", users.owner_id() is None)
    first = service.register_user("taoge", "12345678")
    check("没填邀请码也注册成功", "token" in first)
    owner = first["user"]["user_id"]
    check("他成了主人", users.owner_id() == owner)
    check("现在 needs_invite=True", service.registration_policy()["needs_invite"] is True)

    print("\n=== 第二个人不填码 → 被拦 ===")
    exc = expect("缺码报 InviteRequired", users.InviteRequired, service.register_user, "lisi", "12345678")
    check("文案说的是「需要邀请码」而不是「码不对」",
          exc is not None and "需要邀请码" in str(exc), str(exc))
    check("确实没建出用户", users.count_users() == 1, str(users.count_users()))

    print("\n=== 乱填码 → 被拦 ===")
    exc = expect("错码报 InvalidInvite", users.InvalidInvite,
                 service.register_user, "lisi", "12345678", "AW-XXXX-YYYY")

    print("\n=== 主人发码 ===")
    inv = service.create_invite(owner, "给室友小李")
    code = inv["code"]
    print(f"  发出来的码：{code}")
    check("码是 AW-XXXX-XXXX 形状",
          len(code) == 12 and code.startswith("AW-") and code[7] == "-", code)
    check("码里没有容易看错的字符", not (set(code[3:]) & set("ILO01")), code)
    check("备注存下来了", inv["note"] == "给室友小李", str(inv.get("note")))

    print("\n=== 别人不能发码 ===")
    expect("未注册的人不能发", service.NotAllowed, service.create_invite, "u-someone", None)
    expect("非主人不能看邀请码列表", service.NotAllowed, service.invites_of, "u-someone")

    print("\n=== 拿码注册，成功 ===")
    second = service.register_user("lisi", "12345678", code)
    check("注册成功并拿到令牌", "token" in second)
    check("主人没变（还是最早那个）", users.owner_id() == owner)
    second_id = second["user"]["user_id"]

    print("\n=== 同一个码再用 → 被拦 ===")
    expect("已用过的码报 InvalidInvite", users.InvalidInvite,
           service.register_user, "wangwu", "12345678", code)
    check("用户数还是 2", users.count_users() == 2, str(users.count_users()))

    print("\n=== 大小写 / 空格容错 ===")
    code2 = service.create_invite(owner)["code"]
    messy = " " + code2.lower()[:5] + " " + code2.lower()[5:] + "  "
    third = service.register_user("wangwu", "12345678", messy)
    check(f"「{messy.strip()}」也能用", "token" in third)

    print("\n=== 账号重名时不能白烧一个码 ===")
    code3 = service.create_invite(owner, "给老张")["code"]
    expect("重名报 AccountTaken", users.AccountTaken,
           service.register_user, "lisi", "12345678", code3)
    leftover = [i for i in service.invites_of(owner) if i["code"] == code3]
    check("那个码还在列表里", len(leftover) == 1)
    check("而且没被标成已用", leftover and leftover[0]["used"] is False, str(leftover))
    retry = service.register_user("laozhang", "12345678", code3)
    check("换个账号用它，仍然能用", "token" in retry)

    print("\n=== 邀请码列表 ===")
    all_invites = service.invites_of(owner)
    check("列表里有 3 个码", len(all_invites) == 3, str(len(all_invites)))
    used = [i for i in all_invites if i["used"]]
    unused = [i for i in all_invites if not i["used"]]
    check("3 个都已被用掉", len(used) == 3 and not unused, f"used={len(used)} unused={len(unused)}")
    check("已用的带 used_at", all(i["used_at"] for i in used))
    check("列表不泄露被谁用了", all("used_by" not in i for i in all_invites), str(all_invites[0]))

    print("\n=== 删除邀请码 ===")
    fresh = users.create_invite(owner, "待删除")["code"]
    spare = users.create_invite(owner, "留着别动")["code"]
    check("新建了两个码", len(users.list_invites()) == 5, str(len(users.list_invites())))

    check("删除成功（传小写也认）", users.delete_invite(fresh.lower(), owner) is True)
    check("列表里没了", all(i["code"] != fresh for i in users.list_invites()))
    check("删第二次返回 False", users.delete_invite(fresh, owner) is False)

    expect("用删掉的码注册 → 被拦", users.InvalidInvite,
           service.register_user, "sunqi", "12345678", fresh)
    check("被拦下之后它也没复活", all(i["code"] != fresh for i in users.list_invites()))
    check("那次失败没建出用户", users.count_users() == 4, str(users.count_users()))

    check("别的码还在", any(i["code"] == spare for i in users.list_invites()))
    check("别的码仍然能用", "token" in service.register_user("zhaoliu", "12345678", spare))

    used_code = all_invites[0]["code"]
    check("已用的码能删", users.delete_invite(used_code, owner) is True)
    check("删已用的码不影响那个账号", bool(service.login("lisi", "12345678")["token"]))

    check("不是他发的（用别人的 id 删）→ False",
          users.delete_invite(spare, second_id) is False)
    check("删不存在的码 → False", users.delete_invite("AW-NONE-NONE", owner) is False)
    check("空码 → False", users.delete_invite("", owner) is False)
    check("空码没把整表清空", len(users.list_invites()) >= 1, str(len(users.list_invites())))

    print("\n=== 主角色的边界 ===")
    check("主人 is_owner=True", service.is_owner(owner) is True)
    check("被邀请的人 is_owner=False", service.is_owner(second_id) is False)
    check("空 user_id 不是主人", service.is_owner("") is False)
    expect("被邀请的人发码 → 403", service.NotAllowed, service.create_invite, second_id, None)

    print(f"\n合计 {passed} 项通过，{failed} 项失败")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
