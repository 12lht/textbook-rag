"""访客隔离回归（2026-10-22）。不调用任何 LLM。


改之前所有未登录访客共用 `GUEST_USER_ID = "1"`，后果是**任何陌生人打开网址
都能读到此前所有访客的会话**。现在每个浏览器领一个签名 cookie 当独立身份。

这个测试守四件事，任何一条坏了都意味着隔离被打破或访客体验断掉：

1. **彼此看不见**：A 建的会话，B 列表里没有、直接访问 404；
2. **cookie 必须验签**：手填一个 `g-xxxx` 不能被采纳；
3. **身份要连贯**：同一个客户端刷新后还是同一个 id（否则「刚聊的会话不见了」）；
4. **配额绕不过去**：清 cookie 换新身份时，全局访客池仍然封顶。

另外验证清理：过期访客会话会被删，**登录用户的会话一条都不动**。


存储路径在 import 时就指到临时目录（`conversations` 的四个 JSON + `limits` 的
SQLite），所以这个脚本可以反复跑，不会污染 `data/`。

`TestClient` **不进入 `with`**：那样不会触发 lifespan，也就不会跑 startup
（里面有个 purge，会去碰生产的 checkpoint 库）。清理逻辑在这里手动调。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import conversations

TMP = Path(tempfile.mkdtemp(prefix="guest-isolation-"))
for _name, _file in [
    ("SUMMARY_PATH", "summaries.json"),
    ("RENAME_PATH", "renames.json"),
    ("OWNER_PATH", "owners.json"),
    ("TOUCH_PATH", "touched.json"),
]:
    setattr(conversations, _name, TMP / _file)

import limits  # noqa: E402

limits.DB_PATH = TMP / "limits.sqlite3"

import api  # noqa: E402
import service  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

results: list[bool] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def main() -> None:
    a = TestClient(api.app)
    b = TestClient(api.app)

    print("\n=== 身份签发 ===")
    resp_a = a.get("/api/auth/me")
    ra = resp_a.json()
    rb = b.get("/api/auth/me").json()
    check("两个访客 id 不同", ra["user_id"] != rb["user_id"], f"{ra['user_id']} vs {rb['user_id']}")
    check("都是访客", ra["guest"] and rb["guest"])
    check("id 带 g- 前缀", ra["user_id"].startswith("g-"))
    check("首次响应下发 Set-Cookie", "aw_guest" in resp_a.headers.get("set-cookie", ""))

    again = a.get("/api/auth/me").json()
    check("刷新后身份不变", again["user_id"] == ra["user_id"], again["user_id"])

    print("\n=== 伪造 cookie ===")
    forged = TestClient(api.app)
    forged.cookies.set("aw_guest", "g-0000000000000000." + "0" * 24)
    rf = forged.get("/api/auth/me").json()
    check("伪造 id 不被采纳", rf["user_id"] != "g-0000000000000000", f"got={rf['user_id']}")
    tampered = TestClient(api.app)
    good = ra["user_id"]
    tampered.cookies.set("aw_guest", f"{good}.{'a' * 24}")
    rt = tampered.get("/api/auth/me").json()
    check("签名不对不被采纳", rt["user_id"] != good, f"got={rt['user_id']}")

    print("\n=== 会话隔离 ===")
    tid = a.post("/api/conversations").json()["thread_id"]
    a_list = [x["thread_id"] for x in a.get("/api/conversations").json()]
    b_list = [x["thread_id"] for x in b.get("/api/conversations").json()]
    check("A 列表里有自己的会话", tid in a_list)
    check("B 列表里没有 A 的会话", tid not in b_list)
    check("B 读 A 的历史 → 404", b.get(f"/api/conversations/{tid}/messages").status_code == 404)
    check("B 改 A 的标题 → 404", b.patch(f"/api/conversations/{tid}", json={"title": "x"}).status_code == 404)
    check("B 删 A 的会话 → 404", b.delete(f"/api/conversations/{tid}").status_code == 404)

    print("\n=== 访客池（防清 cookie 绕配额）===")
    saved_pool = limits.GUEST_ASK_PER_DAY
    limits.GUEST_ASK_PER_DAY = 2
    try:
        g1, g2, g3 = (service.new_guest_id() for _ in range(3))
        service.take_ask_slot(g1, "10.0.0.1")
        service.take_ask_slot(g2, "10.0.0.2")
        blocked = False
        try:
            service.take_ask_slot(g3, "10.0.0.3")
        except service.QuotaExceeded:
            blocked = True
        check("池满后新身份也被挡", blocked)
        check("被池挡下时个人额度已退回", limits.used_today(g3, "ask") == 0,
              f"used={limits.used_today(g3, 'ask')}")
    finally:
        limits.GUEST_ASK_PER_DAY = saved_pool

    print("\n=== 过期清理 ===")
    guest_old = service.new_guest_id()
    guest_new = service.new_guest_id()
    old_thread = conversations.create_conversation(guest_old)
    new_thread = conversations.create_conversation(guest_new)
    keeper = conversations.create_conversation(service.CLI_IDENTITY.user_id)

    touched = conversations.load_touched()
    touched[old_thread] = "2020-01-01T00:00:00+00:00"
    touched[keeper] = "2020-01-01T00:00:00+00:00"  # 老，但不是访客
    conversations.save_touched(touched)

    import agent_core

    asked: list[str] = []

    async def fake_delete(thread_id: str) -> None:
        asked.append(thread_id)

    original_delete = agent_core.delete_checkpoint_thread
    agent_core.delete_checkpoint_thread = fake_delete
    try:
        removed = asyncio.run(service.purge_stale_guests())
    finally:
        agent_core.delete_checkpoint_thread = original_delete

    summaries = conversations.load_conversation_summaries()
    check("超期访客会话被清掉", old_thread not in summaries, f"removed={removed}")
    check("新访客会话保留", new_thread in summaries)
    check("非访客的老会话不动", keeper in summaries)
    check("清掉的那个也请了 checkpoint 删除", old_thread in asked, f"asked={asked}")
    check("保留的没有被要求删 checkpoint", new_thread not in asked and keeper not in asked)

    print()
    total, passed = len(results), sum(results)
    print(f"合计 {passed}/{total} 通过")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
