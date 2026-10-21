# -*- coding: utf-8 -*-
"""探针：记忆面板的 HTTP 层——注册 → 列表 → 改 → 删 → 游客 401。

用临时库跑整个应用（不改 `data/`），所以能连鉴权和 404/409 一起验。
对应 PROJECT-PLAN 的 E6。

**不是存储层的复读**：这里要验的是三层拼起来之后的形状——
鉴权挡没挡住、状态码分没分清、以及最要紧的那条
「改完之后拿**旧列表里的 id** 还能删掉」（编号和内容解耦）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:/agent")
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402
import memories  # noqa: E402
import users  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-memory-api-"))
# 每个模块**各自的库文件**。共用一个 `db.sqlite3` 也能跑（各查各的表），
# 但那是靠「表名恰好不一样」侥幸成立的——哪天某个模块改了初始化判据
# （比如开始数全库的表），就会莫名互相干扰。
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
        print("=== 游客不能看记忆 ===")
        r = client.get("/api/memories")
        check("GET 返回 401", r.status_code == 401, r.text[:80])
        r = client.delete("/api/memories/x")
        check("DELETE 返回 401", r.status_code == 401, r.text[:80])

        print("\n=== 注册 → 拿到令牌 ===")
        r = client.post("/api/auth/register", json={"account": "t1@example.com", "password": "12345678"})
        check("注册 201/200", r.status_code in (200, 201), r.text[:120])
        token = r.json()["token"]
        uid = r.json()["user"]["user_id"]
        headers = {"Authorization": f"Bearer {token}"}

        print("\n=== 空列表 ===")
        r = client.get("/api/memories", headers=headers)
        check("返回 200 且为空", r.status_code == 200 and r.json() == [], r.text[:120])

        print("\n=== 写三条后能看到 ===")
        memories.remember(
            uid,
            [
                {"text": "先给结论再展开", "kind": "偏好"},
                {"text": "叫我涛哥", "kind": "身份"},
                {"text": "在复习第3章", "kind": "计划"},
            ],
            source_thread="t-1",
        )
        r = client.get("/api/memories", headers=headers)
        items = r.json()
        check("三条都在", len(items) == 3, str(items)[:120])
        check("每条都带 id", all(item.get("id") for item in items))
        check("不对外暴露 fingerprint", all("fingerprint" not in i for i in items))
        mine = next(item for item in items if item["kind"] == "计划")
        old_id = mine["id"]

        print("\n=== 改一条 ===")
        r = client.patch(
            f"/api/memories/{old_id}",
            json={"text": "在复习移动通信第3章"},
            headers=headers,
        )
        check("PATCH 200", r.status_code == 200, r.text[:120])
        items = client.get("/api/memories", headers=headers).json()
        check("新措辞出现", any(i["text"] == "在复习移动通信第3章" for i in items))
        check("条数仍为 3", len(items) == 3)
        target = next(i for i in items if i["text"] == "在复习移动通信第3章")
        check("改完 id 没变（前端不用重新记编号）", target["id"] == old_id, target["id"])

        print("\n=== 改成重复 ===")
        r = client.patch(
            f"/api/memories/{target['id']}",
            json={"text": "先给结论再展开"},
            headers=headers,
        )
        check("409 clash", r.status_code == 409, r.text[:100])

        print("\n=== 用**改之前**拿到的 id 删（回归）===")
        # 前端典型动作序列：拉列表 → 改措辞 → 删。中间不会重新拉列表，
        # 所以它手里只有改之前的 id。要是那个 id 会随内容变，这一步就是 404。
        r = client.delete(f"/api/memories/{old_id}", headers=headers)
        check("DELETE 200", r.status_code == 200, r.text[:100])
        items = client.get("/api/memories", headers=headers).json()
        check("剩两条", len(items) == 2, str(len(items)))
        check("被删的读不到了", all(i["text"] != "在复习移动通信第3章" for i in items))

        print("\n=== 再删同一条 ===")
        r = client.delete(f"/api/memories/{old_id}", headers=headers)
        check("404（不存在）", r.status_code == 404, r.text[:100])

        print("\n=== 换一个账号看不到别人的 ===")
        # 站点已有用户之后注册必须持邀请码（F1），所以让 t1（主人）先发一个。
        # 顺带把「主人能发码」这条也验了。
        r = client.post("/api/invites", json={"note": "探针用"}, headers=headers)
        check("主人能发邀请码", r.status_code == 200, r.text[:100])
        invite = r.json()["code"]
        r2 = client.post(
            "/api/auth/register",
            json={"account": "t2@example.com", "password": "12345678", "invite": invite},
        )
        check("第二个人持码注册成功", r2.status_code in (200, 201), r2.text[:120])
        h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
        items2 = client.get("/api/memories", headers=h2).json()
        check("另一个账号是空的", items2 == [], str(items2)[:100])
        r = client.delete(f"/api/memories/{items[0]['id']}", headers=h2)
        check("删别人的返回 404", r.status_code == 404, r.text[:100])

        print("\n=== 原账号数据没被影响 ===")
        check("仍为 2 条", len(client.get("/api/memories", headers=headers).json()) == 2)

    print(f"\n合计 {ok} 项通过，{bad} 项失败")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
