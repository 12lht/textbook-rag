"""探针：邀请制注册的 HTTP 层（F1）。

存储层已经由 `probe-invite.py` 覆盖，这里只验路由层要额外保证的东西：
状态码分得清（400 / 401 / 403）、`is_owner` 有没有跟着 `/api/auth/me` 出去、
以及**未登录的人拿不到主人是谁**。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402
import memories  # noqa: E402
import users  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-invite-api-"))
users.DATA_DIR = tmp
users.DB_PATH = tmp / "users.sqlite3"
memories.DATA_DIR = tmp
memories.DB_PATH = tmp / "memories.sqlite3"
memories.LEGACY_JSON_PATH = tmp / "memories.json"
documents.DATA_DIR = tmp
documents.DB_PATH = tmp / "documents.sqlite3"
documents.UPLOAD_DIR = tmp / "uploads"
documents.ASSET_DIR = tmp / "assets"
tmp.mkdir(parents=True, exist_ok=True)
for sub in ("uploads", "assets"):
    (tmp / sub).mkdir(exist_ok=True)

import api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = 0
bad = 0


def check(label: str, condition: bool, extra: str = "") -> None:
    global ok, bad
    if condition:
        ok += 1
        print(f"  OK   {label}")
    else:
        bad += 1
        print(f"  FAIL {label}  {extra}")


def main() -> None:
    with TestClient(api.app) as client:
        print("=== 空库：告诉前端「不用码」===")
        r = client.get("/api/auth/registration-policy")
        check("200", r.status_code == 200, r.text[:80])
        check("needs_invite=False", r.json().get("needs_invite") is False, str(r.json()))
        check("不回「主人是谁」", "is_owner" not in r.json() and "owner" not in r.json(),
              str(r.json()))

        print("\n=== 游客不能碰邀请码 ===")
        check("未登录发码 → 401", client.post("/api/invites").status_code == 401)
        check("未登录看列表 → 401", client.get("/api/invites").status_code == 401)

        print("\n=== 第一个用户免码注册 ===")
        r = client.post("/api/auth/register", json={"account": "taoge", "password": "12345678"})
        check("201/200", r.status_code in (200, 201), r.text[:120])
        owner_headers = {"Authorization": f"Bearer {r.json()['token']}"}
        check("me 里 is_owner=True",
              client.get("/api/auth/me", headers=owner_headers).json().get("is_owner") is True)

        print("\n=== 库里有人的，策略翻转 ===")
        check("needs_invite=True",
              client.get("/api/auth/registration-policy").json().get("needs_invite") is True)

        print("\n=== 第二个人缺码 / 错码 ===")
        r = client.post("/api/auth/register", json={"account": "lisi", "password": "12345678"})
        check("缺码 → 400", r.status_code == 400, r.text[:100])
        check("文案点明「需要邀请码」", "需要邀请码" in r.text, r.text[:100])
        r = client.post("/api/auth/register",
                        json={"account": "lisi", "password": "12345678", "invite": "AW-AAAA-BBBB"})
        check("错码 → 400", r.status_code == 400, r.text[:100])
        check("错码文案不是「需要邀请码」", "需要邀请码" not in r.text, r.text[:100])

        print("\n=== 主人发码，第二个人持码注册 ===")
        r = client.post("/api/invites", json={"note": "给小李"}, headers=owner_headers)
        check("发码 200", r.status_code == 200, r.text[:100])
        code = r.json()["code"]
        r = client.post("/api/auth/register",
                        json={"account": "lisi", "password": "12345678", "invite": code})
        check("持码注册成功", r.status_code in (200, 201), r.text[:120])
        guest2_headers = {"Authorization": f"Bearer {r.json()['token']}"}
        check("他说自己不是主人",
              client.get("/api/auth/me", headers=guest2_headers).json().get("is_owner") is False)

        print("\n=== 已用过的码不能再用 ===")
        r = client.post("/api/auth/register",
                        json={"account": "wangwu", "password": "12345678", "invite": code})
        check("→ 400", r.status_code == 400, r.text[:100])

        print("\n=== 非主人发码 → 403 ===")
        r = client.post("/api/invites", json={}, headers=guest2_headers)
        check("403 而不是 404", r.status_code == 403, r.text[:100])
        r = client.get("/api/invites", headers=guest2_headers)
        check("看列表也是 403", r.status_code == 403, r.text[:100])

        print("\n=== 主人看自己的邀请码 ===")
        r = client.get("/api/invites", headers=owner_headers)
        check("200", r.status_code == 200, r.text[:100])
        items = r.json()
        check("有一个码", len(items) == 1, str(len(items)))
        check("它被标成已用", items[0]["used"] is True, str(items[0]))

        print("\n=== 删除邀请码 ===")
        r = client.delete("/api/invites/AW-NONE-NONE", headers=guest2_headers)
        check("非主人删 → 403", r.status_code == 403, r.text[:100])
        r = client.delete(f"/api/invites/{items[0]['code']}", headers=guest2_headers)
        check("非主人删已用的码也是 403", r.status_code == 403, r.text[:100])
        check("被拦下之后码还在",
              len(client.get("/api/invites", headers=owner_headers).json()) == 1)

        check("未登录删 → 401", client.delete("/api/invites/AW-AAAA-BBBB").status_code == 401)

        unused = client.post("/api/invites", json={"note": "删我"}, headers=owner_headers).json()["code"]
        r = client.delete(f"/api/invites/{unused}", headers=owner_headers)
        check("主人删 → 200", r.status_code == 200, r.text[:100])
        check("响应里带 code 和 deleted",
              r.json().get("code") == unused and r.json().get("status") == "deleted", str(r.json()))
        codes = [i["code"] for i in client.get("/api/invites", headers=owner_headers).json()]
        check("删完列表里没了", unused not in codes, str(codes))
        r = client.post("/api/auth/register",
                        json={"account": "wangwu", "password": "12345678", "invite": unused})
        check("用删掉的码注册 → 400", r.status_code == 400, r.text[:120])
        check("而且提示是「码不对」而不是「服务器错了」",
              "邀请码" in str(r.json().get("detail")), str(r.json())[:120])

        r = client.delete(f"/api/invites/{unused}", headers=owner_headers)
        check("删第二次 → 404", r.status_code == 404, r.text[:100])
        r = client.delete("/api/invites/AW-NONE-NONE", headers=owner_headers)
        check("删不存在的码 → 404", r.status_code == 404, r.text[:100])

        used_code = items[0]["code"]
        r = client.delete(f"/api/invites/{used_code}", headers=owner_headers)
        check("已用的码也能删 → 200", r.status_code == 200, r.text[:100])
        r = client.post("/api/auth/login", json={"account": "lisi", "password": "12345678"})
        check("那个账号照样能登录", r.status_code == 200 and "token" in r.json(), r.text[:120])
        check("列表彻底空了",
              client.get("/api/invites", headers=owner_headers).json() == [])

    print(f"\n合计 {ok} 项通过，{bad} 项失败")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
