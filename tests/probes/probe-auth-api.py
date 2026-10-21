"""认证与越权隔离的 HTTP 层探针。

用 TestClient 打真实路由，但把 `agent_core` 那 5 个会碰 checkpoint / 模型的函数
换成桩：目的只是验「HTTP → service → 存储」这条链路的**状态码和归属判断**，
不需要真的跑图（跑图要起 MCP 子进程 + 真模型调用，又慢又花钱，还会写真实 checkpoint）。

存储全部重定向到临时目录，收尾再核对真实 data/ 没被动过。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"    ✓ {label}  {detail}")
    else:
        FAIL += 1
        print(f"    ✗ {label}  {detail}")


def header(text: str) -> None:
    print(f"\n== {text} ==")


REAL_DIR = ROOT / "data"
REAL_FILES = [
    "conversation_summaries.json",
    "conversation_renames.json",
    "conversation_owners.json",
    "users.sqlite3",
]


def real_snapshot() -> dict:
    return {
        name: (REAL_DIR / name).stat().st_mtime_ns if (REAL_DIR / name).exists() else None
        for name in REAL_FILES
    }


before_real = real_snapshot()

import conversations  # noqa: E402
import documents  # noqa: E402
import limits  # noqa: E402
import memories  # noqa: E402
import users  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="auth-api-"))
users.DB_PATH = TMP / "users.sqlite3"
conversations.SUMMARY_PATH = TMP / "conversation_summaries.json"
conversations.RENAME_PATH = TMP / "conversation_renames.json"
conversations.OWNER_PATH = TMP / "conversation_owners.json"
# 配额和待办列表：不重定向的话这个探针会去消费**生产**的提问次数
# （`limits.sqlite3` 在 data/ 下），跑两遍就把额度打没了。
memories.DATA_DIR = TMP
memories.DB_PATH = TMP / "memories.sqlite3"
memories.LEGACY_JSON_PATH = TMP / "memories.json"
documents.DATA_DIR = TMP
documents.DB_PATH = TMP / "documents.sqlite3"
documents.UPLOAD_DIR = TMP / "uploads"
documents.ASSET_DIR = TMP / "assets"
limits.DATA_DIR = TMP
limits.DB_PATH = TMP / "limits.sqlite3"
for _sub in ("uploads", "assets"):
    (TMP / _sub).mkdir(exist_ok=True)

import agent_core  # noqa: E402

# ---- 桩掉 agent_core 的「跑图 / 读状态」部分 ----------------------------------
stream_calls: list[dict] = []


async def fake_stream_agent(
    user_id,
    thread_id,
    user_input,
    location=None,
    *,
    memory_enabled=True,
    reasoning=True,
    web_search=True,
):
    # 签名必须和 `agent_core.stream_agent` **完全对齐**：`service` 是按关键字
    # 传 `reasoning` / `web_search` 的，桩少写一个参数就会在开流之后才抛
    # `unexpected keyword argument`，表现成「200 但流里只有一个 error 事件」。
    stream_calls.append(
        {
            "user_id": user_id,
            "thread_id": thread_id,
            "memory_enabled": memory_enabled,
            "reasoning": reasoning,
            "web_search": web_search,
        }
    )
    yield {"type": "assistant_delta", "content": "（桩回复）"}


async def fake_user_messages(thread_id):
    return []


async def fake_generate_title(context):
    return None  # 交给 service 回退成用户原话


async def fake_get_history(thread_id):
    return []


deleted_threads: list[str] = []


async def fake_delete_checkpoint_thread(thread_id):
    deleted_threads.append(thread_id)


agent_core.stream_agent = fake_stream_agent
agent_core.user_messages = fake_user_messages
agent_core.generate_title = fake_generate_title
agent_core.get_history = fake_get_history
agent_core.delete_checkpoint_thread = fake_delete_checkpoint_thread

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
import service  # noqa: E402

client = TestClient(api.app)

ALICE = {"account": "taoge@example.com", "password": "correct-horse-1"}
BOB = {"account": "bob", "password": "battery-staple-2"}


def auth(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


print("临时目录:", TMP)

with client:
    # ----------------------------------------------------------------
    header("1. 游客：没有令牌也能列会话，且拿到的是默认账号视角")
    # ----------------------------------------------------------------
    legacy = conversations.create_conversation(users.GUEST_USER_ID)
    r = client.get("/api/conversations")
    check(r.status_code == 200, "无令牌列会话 → 200", str(r.status_code))
    ids = [c["thread_id"] for c in r.json()]
    check(legacy in ids, "能看到没有归属记录的老会话", legacy)

    r = client.get("/api/auth/me")
    me = r.json()
    check(r.status_code == 200 and me["guest"] is True, "me → 游客", str(me))
    check(me["memory_enabled"] is False,
          "游客身份 memory_enabled=False（几个陌生人共用一个账号，不能互看记忆）")

    # ----------------------------------------------------------------
    header("2. 注册")
    # ----------------------------------------------------------------
    r = client.post("/api/auth/register", json=ALICE)
    check(r.status_code == 200, "注册成功 → 200", str(r.status_code))
    alice = r.json()
    alice_token = alice["token"]
    alice_id = alice["user"]["user_id"]
    check(alice["user"]["account"] == ALICE["account"], "账号原文保留大小写", alice_id)
    check(bool(alice.get("expires_at")), "返回了过期时间", alice.get("expires_at", ""))
    check("password" not in str(alice).lower().replace("password_hash", ""),
          "响应里不含密码相关字段")

    # 站里已经有 alice 了，之后每次注册都要持码（F1）。
    # 主人发码走真实的 `/api/invites`，顺手把这条路径也验了。
    def new_invite() -> str:
        return client.post("/api/invites", json={}, headers=auth(alice_token)).json()["code"]

    r = client.post("/api/auth/register", json={**ALICE, "invite": new_invite()})
    check(r.status_code == 400, "重复注册 → 400", str(r.status_code))
    check("已经被注册" in r.json()["detail"], "重名文案来自领域层", r.json()["detail"])

    r = client.post("/api/auth/register", json={"account": "short", "password": "123"})
    check(r.status_code == 400, "密码太短 → 400", str(r.status_code))
    check("8" in r.json()["detail"], "文案说的是 8 位（领域层的规则）", r.json()["detail"])

    # 缺邀请码 → 400，且文案说的是「需要邀请码」而不是「码不对」
    r = client.post("/api/auth/register", json=BOB)
    check(r.status_code == 400, "没带邀请码 → 400", str(r.status_code))
    check("需要邀请码" in r.json()["detail"], "文案点明缺的是邀请码", r.json()["detail"])

    r = client.post("/api/auth/register", json={**BOB, "invite": new_invite()})
    bob_token = r.json()["token"]
    bob_id = r.json()["user"]["user_id"]

    # ----------------------------------------------------------------
    header("3. me：带令牌 / 乱令牌")
    # ----------------------------------------------------------------
    r = client.get("/api/auth/me", headers=auth(alice_token))
    me = r.json()
    check(me["account"] == ALICE["account"] and me["guest"] is False, "登录态 me", str(me))
    check(me["memory_enabled"] is True, "登录用户 memory_enabled=True")

    r = client.get("/api/auth/me", headers=auth("not-a-real-token"))
    check(r.status_code == 401, "乱令牌 → 401（**不降级成游客**）", str(r.status_code))

    r = client.get("/api/auth/me", headers={"Authorization": alice_token})
    check(r.status_code == 401, "缺 Bearer 前缀 → 401（不静默当游客）", str(r.status_code))

    # ----------------------------------------------------------------
    header("4. 登录")
    # ----------------------------------------------------------------
    r = client.post("/api/auth/login", json=ALICE)
    check(r.status_code == 200, "正确密码 → 200", str(r.status_code))
    check(r.json()["user"]["user_id"] == alice_id, "登录拿到的还是同一个人")

    r1 = client.post("/api/auth/login", json={"account": ALICE["account"], "password": "wrong-pass"})
    r2 = client.post("/api/auth/login", json={"account": "nobody-here", "password": "wrong-pass"})
    check(r1.status_code == 401 and r2.status_code == 401, "密码错 / 账号不存在 → 都是 401")
    check(r1.json()["detail"] == r2.json()["detail"],
          "两者文案完全相同（不给探测账号是否存在）", r1.json()["detail"])

    # ----------------------------------------------------------------
    header("5. 会话隔离：B 拿 A 的 thread_id")
    # ----------------------------------------------------------------
    a_conv = client.post("/api/conversations", headers=auth(alice_token)).json()["thread_id"]
    b_conv = client.post("/api/conversations", headers=auth(bob_token)).json()["thread_id"]

    a_list = [c["thread_id"] for c in client.get("/api/conversations", headers=auth(alice_token)).json()]
    b_list = [c["thread_id"] for c in client.get("/api/conversations", headers=auth(bob_token)).json()]
    check(a_list == [a_conv], "A 的列表只有自己的会话", str(a_list))
    check(b_list == [b_conv], "B 的列表只有自己的会话", str(b_list))
    check(legacy not in a_list, "登录用户看不到默认账号名下的老会话")

    r = client.get(f"/api/conversations/{a_conv}/messages", headers=auth(bob_token))
    check(r.status_code == 404, "B 读 A 的消息 → 404（不是 403）", str(r.status_code))
    r = client.get(f"/api/conversations/{a_conv}/messages", headers=auth(alice_token))
    check(r.status_code == 200, "A 自己读 → 200", str(r.status_code))

    r = client.patch(f"/api/conversations/{a_conv}", json={"title": "偷改"}, headers=auth(bob_token))
    check(r.status_code == 404, "B 改 A 的标题 → 404", str(r.status_code))
    check(conversations.load_conversation_summaries()[a_conv] == conversations.DEFAULT_SUMMARY,
          "标题没被改掉")

    r = client.post(f"/api/conversations/{a_conv}/rewind", json={"turn": 0}, headers=auth(bob_token))
    check(r.status_code == 404, "B 回退 A 的会话 → 404", str(r.status_code))

    r = client.delete(f"/api/conversations/{a_conv}", headers=auth(bob_token))
    check(r.status_code == 404, "B 删 A 的会话 → 404", str(r.status_code))
    check(conversations.conversation_exists(a_conv), "A 的会话还在（没被删掉）")
    check(deleted_threads == [], "checkpoint 一次都没被删", str(deleted_threads))

    r = client.delete(f"/api/conversations/{a_conv}", headers=auth(alice_token))
    check(r.status_code == 200, "A 自己删 → 200", str(r.status_code))
    check(deleted_threads == [a_conv], "确实去删了 checkpoint", str(deleted_threads))

    # ----------------------------------------------------------------
    header("6. 客户端不能再自报 user_id")
    # ----------------------------------------------------------------
    stream_calls.clear()
    r = client.post(
        "/api/chat/stream",
        json={"message": "读一下别人的会话", "thread_id": b_conv, "user_id": alice_id},
        headers=auth(alice_token),
    )
    check(r.status_code == 404, "A 冒充 user_id 去聊 B 的会话 → 404", str(r.status_code))
    check(stream_calls == [], "压根没开始跑图", str(stream_calls))

    r = client.post(
        "/api/chat/stream",
        json={"message": "你好", "thread_id": b_conv},
        headers=auth(bob_token),
    )
    check(r.status_code == 200, "B 聊自己的会话 → 200", str(r.status_code))
    check("assistant_delta" in r.text, "SSE 里有回答事件")
    check(stream_calls and stream_calls[-1]["user_id"] == bob_id,
          "跑图用的是令牌里的 user_id", str(stream_calls[-1] if stream_calls else None))
    check(stream_calls and stream_calls[-1]["memory_enabled"] is True,
          "登录用户这次请求开着记忆")

    # ----------------------------------------------------------------
    header("7. 游客聊天：memory_enabled 必须是 False")
    # ----------------------------------------------------------------
    stream_calls.clear()
    r = client.post("/api/chat/stream", json={"message": "你好"})
    check(r.status_code == 200, "游客不给令牌也能聊 → 200", str(r.status_code))
    check(stream_calls and stream_calls[-1]["memory_enabled"] is False,
          "游客这次请求关着记忆", str(stream_calls[-1] if stream_calls else None))
    check(stream_calls and stream_calls[-1]["user_id"] == users.GUEST_USER_ID,
          "游客挂在默认账号下", str(stream_calls[-1] if stream_calls else None))

    # 游客新建的会话归默认账号，登录用户看不到
    guest_conv = None
    for c in client.get("/api/conversations").json():
        if c["thread_id"] not in {legacy}:
            guest_conv = c["thread_id"]
    check(guest_conv is not None, "游客聊天自动建的会话出现在游客列表里", str(guest_conv))
    check(guest_conv not in [x["thread_id"] for x in
                             client.get("/api/conversations", headers=auth(alice_token)).json()],
          "登录用户看不到游客新会话")

    # ----------------------------------------------------------------
    header("8. 登出")
    # ----------------------------------------------------------------
    r = client.post("/api/auth/logout", headers=auth(bob_token))
    check(r.status_code == 200 and r.json()["revoked"] is True, "登出 → 200", str(r.json()))
    r = client.get("/api/auth/me", headers=auth(bob_token))
    check(r.status_code == 401, "登出后原令牌失效 → 401", str(r.status_code))
    r = client.post("/api/auth/logout", headers=auth(bob_token))
    check(r.status_code == 200 and r.json()["revoked"] is False,
          "重复登出不报错（幂等）", str(r.json()))

    # ----------------------------------------------------------------
    header("9. 真实 data/ 没被碰过")
    # ----------------------------------------------------------------
    check(before_real == real_snapshot(), "四个文件 mtime 未变", str(real_snapshot()))
    check(users.DB_PATH != REAL_DIR / "users.sqlite3", "users 库路径已换到临时目录")

print(f"\n结果: {PASS}/{PASS + FAIL} 通过")
sys.exit(0 if FAIL == 0 else 1)
