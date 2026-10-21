# -*- coding: utf-8 -*-
"""探针：知识库管理页的 HTTP 层——分类增删改 + 文档归类。

存储层的行为（重名靠唯一索引、删分类不删书）已经由 `probe-categories.py` 覆盖，
这里只验路由层**额外**要保证的东西：

1. 状态码分得开。重名 409 / 名字不合法 400 / 不存在 404，三个含义不同，
   混成一个「操作失败」用户就没法判断是该换名字还是该刷新页面。
2. 400 的 `detail` 是**能读的中文句子**，不是 FastAPI 的 422 校验数组。
   （`CategoryRequest` 只挡超大请求，`min_length` 那种写法会把空名字变成 422。）
3. 越权一律 404 而不是 403——分类 id 是随机的，403 等于告诉对方「这个 id 存在」。
4. 删分类之后，类下的书**回到未分类**，而且真的还能读到。

文档记录直接调 `documents.create_upload` 造，**不走 POST /api/documents**：
上传和解析已经由 `probe-documents.py` 验过，在这里再解析一遍 PDF 只会让
这个探针慢几十秒，还引入检索预热的干扰。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:/agent")
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402
import users  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-categories-api-"))
# 每个模块各自的库文件，理由见 probe-memory-api.py：共用一个库是靠
# 「表名恰好不一样」侥幸成立的。
users.DATA_DIR = tmp
users.DB_PATH = tmp / "users.sqlite3"
documents.DATA_DIR = tmp
documents.DB_PATH = tmp / "documents.sqlite3"
documents.UPLOAD_DIR = tmp / "uploads"
documents.ASSET_DIR = tmp / "assets"
for sub in ("uploads", "assets"):
    (tmp / sub).mkdir(parents=True, exist_ok=True)

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


def register(client, account: str, invite: str | None = None) -> dict:
    payload = {"account": account, "password": "12345678"}
    if invite:
        payload["invite"] = invite
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def fake_doc(user_id: str, name: str) -> str:
    """造一条文档记录（不解析）。内容不同 → content_hash 不同 → 不会去重。"""
    record = documents.create_upload(user_id, name, b"%PDF-1.4\n%probe " + name.encode())
    return str(record["doc_id"])


def main() -> None:
    with TestClient(api.app) as client:
        print("=== 游客一律 401 ===")
        check("GET /api/categories → 401", client.get("/api/categories").status_code == 401)
        check("POST /api/categories → 401",
              client.post("/api/categories", json={"name": "x"}).status_code == 401)
        check("PATCH /api/categories/x → 401",
              client.patch("/api/categories/c-1", json={"name": "x"}).status_code == 401)
        check("DELETE /api/categories/x → 401",
              client.delete("/api/categories/c-1").status_code == 401)
        check("PATCH /api/documents/x → 401",
              client.patch("/api/documents/d-1", json={"category_id": None}).status_code == 401)

        print("\n=== 两个用户 ===")
        alice = register(client, "alice")
        # 第二个人要走邀请码（第一个注册的才是主人）。
        code = client.post("/api/invites", json={}, headers=alice).json()["code"]
        bob = register(client, "bob", code)
        alice_id = client.get("/api/auth/me", headers=alice).json()["user_id"]
        bob_id = client.get("/api/auth/me", headers=bob).json()["user_id"]

        print("\n=== 新建 ===")
        r = client.post("/api/categories", json={"name": "  移动通信  "}, headers=alice)
        check("201", r.status_code == 201, r.text[:120])
        body = r.json()
        check("名字折叠了空白", body.get("name") == "移动通信", str(body))
        check("带 category_id", bool(body.get("category_id")), str(body))
        check("初始 doc_count = 0", body.get("doc_count") == 0, str(body))
        cat1 = body["category_id"]

        r = client.post("/api/categories", json={"name": "信号与系统"}, headers=alice)
        cat2 = r.json()["category_id"]
        check("建第二个也成功", r.status_code == 201, r.text[:120])

        print("\n=== 重名 / 坏名字 ===")
        r = client.post("/api/categories", json={"name": "移动通信"}, headers=alice)
        check("重名 → 409", r.status_code == 409, r.text[:120])
        check("409 的 detail 是中文句子", isinstance(r.json().get("detail"), str),
              str(r.json())[:120])

        r = client.post("/api/categories", json={"name": "   "}, headers=alice)
        # 这条最关键：写成 Field(min_length=1) 会变成 422 + 校验数组
        check("空名字 → 400（不是 422）", r.status_code == 400, r.text[:120])
        check("空名字的 detail 可读", r.json().get("detail") == "分类名不能为空", str(r.json())[:120])

        r = client.post("/api/categories", json={"name": "长" * 41}, headers=alice)
        check("41 个字 → 400", r.status_code == 400, r.text[:120])

        print("\n=== 列表与计数 ===")
        r = client.get("/api/categories", headers=alice)
        items = r.json()
        check("两个分类", len(items) == 2, str(items)[:160])
        check("按创建先后排", [it["name"] for it in items] == ["移动通信", "信号与系统"],
              str([it["name"] for it in items]))

        print("\n=== 归类 ===")
        d1 = fake_doc(alice_id, "a1.pdf")
        d2 = fake_doc(alice_id, "a2.pdf")
        r = client.patch(f"/api/documents/{d1}", json={"category_id": cat1}, headers=alice)
        check("归类 200", r.status_code == 200, r.text[:120])
        check("返回新的 category_id", r.json().get("category_id") == cat1, str(r.json())[:120])
        client.patch(f"/api/documents/{d2}", json={"category_id": cat1}, headers=alice)

        counts = {it["category_id"]: it["doc_count"] for it in
                  client.get("/api/categories", headers=alice).json()}
        check("doc_count 变成 2", counts.get(cat1) == 2, str(counts))

        docs = {d["doc_id"]: d for d in client.get("/api/documents", headers=alice).json()}
        check("列表里带 category_id", docs.get(d1, {}).get("category_id") == cat1,
              str(docs.get(d1))[:160])

        r = client.patch(f"/api/documents/{d1}", json={"category_id": None}, headers=alice)
        check("挪回未分类 200", r.status_code == 200, r.text[:120])
        docs = {d["doc_id"]: d for d in client.get("/api/documents", headers=alice).json()}
        check("category_id 变回 None", docs.get(d1, {}).get("category_id") is None,
              str(docs.get(d1))[:160])

        r = client.patch("/api/documents/d-nope", json={"category_id": cat1}, headers=alice)
        check("给不存在的书归类 → 404", r.status_code == 404, r.text[:120])

        print("\n=== 越权一律 404 ===")
        bob_doc = fake_doc(bob_id, "b1.pdf")
        r = client.patch(f"/api/documents/{bob_doc}", json={"category_id": cat1}, headers=alice)
        check("给别人的书归类 → 404", r.status_code == 404, r.text[:120])
        detail_doc = r.json().get("detail")
        # 这里必须用**自己的书 + 别人的分类**，才走得到「分类不存在」那条分支。
        # 拿别人的书配别人的分类，第一道检查（书是不是我的）就返回了，
        # 测的其实还是同一件事。
        r = client.patch(f"/api/documents/{bob_doc}", json={"category_id": cat1}, headers=bob)
        check("用别人的分类 id 归类 → 404", r.status_code == 404, r.text[:120])
        detail_cat = r.json().get("detail")
        check("两条 404 的 detail 不同（书 / 分类）", detail_doc != detail_cat,
              f"{detail_doc!r} vs {detail_cat!r}")
        check("看不到别人的分类",
              client.get("/api/categories", headers=bob).json() == [])

        print("\n=== 改名 ===")
        r = client.patch(f"/api/categories/{cat2}", json={"name": "信号与系统（2024 秋）"},
                         headers=alice)
        check("改名 200", r.status_code == 200, r.text[:120])
        names = [it["name"] for it in client.get("/api/categories", headers=alice).json()]
        check("新名字生效", "信号与系统（2024 秋）" in names, str(names))

        r = client.patch(f"/api/categories/{cat2}", json={"name": "移动通信"}, headers=alice)
        check("改成已有的名字 → 409", r.status_code == 409, r.text[:120])
        r = client.patch(f"/api/categories/{cat2}", json={"name": "   "}, headers=alice)
        check("改成空名字 → 400", r.status_code == 400, r.text[:120])
        r = client.patch("/api/categories/c-nope", json={"name": "随便"}, headers=alice)
        check("改不存在的分类 → 404", r.status_code == 404, r.text[:120])
        r = client.patch(f"/api/categories/{cat2}", json={"name": "别人改不动"}, headers=bob)
        check("改别人的分类 → 404", r.status_code == 404, r.text[:120])

        print("\n=== 删分类：书退回未分类，不跟着删 ===")
        r = client.delete(f"/api/categories/{cat1}", headers=alice)
        check("200", r.status_code == 200, r.text[:120])
        check("响应里写明书未被删", r.json().get("documents") == "unfiled", str(r.json())[:120])

        docs = {d["doc_id"]: d for d in client.get("/api/documents", headers=alice).json()}
        check("两本书都还在", d1 in docs and d2 in docs, str(list(docs))[:160])
        check("两本书都回到未分类",
              docs[d1]["category_id"] is None and docs[d2]["category_id"] is None,
              f"{docs[d1]['category_id']} / {docs[d2]['category_id']}")
        check("分类真的没了",
              [it["category_id"] for it in client.get("/api/categories", headers=alice).json()]
              == [cat2])

        r = client.delete(f"/api/categories/{cat1}", headers=alice)
        check("删第二次 → 404", r.status_code == 404, r.text[:120])
        r = client.delete(f"/api/categories/{cat2}", headers=bob)
        check("删别人的分类 → 404", r.status_code == 404, r.text[:120])
        check("别人的分类还在",
              len(client.get("/api/categories", headers=alice).json()) == 1)

    print(f"\n通过 {ok}，失败 {bad}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
