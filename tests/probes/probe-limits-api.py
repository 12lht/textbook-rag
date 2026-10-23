"""探针：限制的 HTTP 层——状态码、`Retry-After`、以及「额度没了就别跑模型」。

**这个探针不调模型**：额度判定在开流之前，所以「打满了」的请求会在
`begin_chat` 之前就 429 回去。要是哪天这个顺序被改反了，最直观的表现就是
这个探针开始变慢、开始花钱——那本身就是一个信号。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # 仓库根：从本文件位置反推，不写死盘符

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT))

import documents  # noqa: E402
import limits  # noqa: E402
import memories  # noqa: E402
import users  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-limits-api-"))
users.DATA_DIR = tmp
users.DB_PATH = tmp / "users.sqlite3"
memories.DATA_DIR = tmp
memories.DB_PATH = tmp / "memories.sqlite3"
memories.LEGACY_JSON_PATH = tmp / "memories.json"
documents.DATA_DIR = tmp
documents.DB_PATH = tmp / "documents.sqlite3"
documents.UPLOAD_DIR = tmp / "uploads"
documents.ASSET_DIR = tmp / "assets"
limits.DATA_DIR = tmp
limits.DB_PATH = tmp / "limits.sqlite3"
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
        print("=== 登录接口限流 ===")
        limits.reset_rate_limits()
        statuses = []
        for _ in range(limits.LOGIN_ATTEMPTS_PER_MINUTE):
            statuses.append(client.post("/api/auth/login",
                                        json={"account": "nobody", "password": "12345678"}).status_code)
        check("前 10 次是普通的 401（账号不存在）",
              set(statuses) == {401}, str(set(statuses)))

        r = client.post("/api/auth/login", json={"account": "nobody", "password": "12345678"})
        check("第 11 次是 429", r.status_code == 429, r.text[:100])
        check("带 Retry-After 头", "retry-after" in {k.lower() for k in r.headers},
              str(dict(r.headers)))
        check("Retry-After 是正整数",
              r.headers.get("retry-after", "").isdigit()
              and int(r.headers["retry-after"]) >= 1,
              r.headers.get("retry-after"))

        print("\n=== 换一个来源就不受影响 ===")
        r = client.post("/api/auth/login",
                        json={"account": "nobody", "password": "12345678"},
                        headers={"X-Forwarded-For": "9.9.9.9"})
        check("另一个 IP 还是 401 而不是 429", r.status_code == 401, r.text[:100])

        print("\n=== 先注册一个真账号（第一个，免邀请码）===")
        limits.reset_rate_limits()
        r = client.post("/api/auth/register", json={"account": "taoge", "password": "12345678"})
        check("注册成功", r.status_code in (200, 201), r.text[:120])
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
        uid = r.json()["user"]["user_id"]

        print("\n=== 注册也限流 ===")
        limits.reset_rate_limits()
        for _ in range(limits.LOGIN_ATTEMPTS_PER_MINUTE):
            client.post("/api/auth/register", json={"account": "spam", "password": "12345678"})
        r = client.post("/api/auth/register", json={"account": "spam2", "password": "12345678"})
        check("超了之后注册也是 429", r.status_code == 429, r.text[:100])

        print("\n=== 额度用完：429 且不跑模型 ===")
        real = limits.ASK_PER_DAY
        limits.ASK_PER_DAY = 0
        limits.reset_rate_limits()
        try:
            r = client.post("/api/chat/stream", json={"message": "你好"}, headers=headers)
            check("429", r.status_code == 429, r.text[:120])
            check("文案说「用完了」而不是「太快了」", "用完" in r.text, r.text[:120])
            check("不是流式响应（没有开始跑图）",
                  not r.headers.get("content-type", "").startswith("text/event-stream"),
                  r.headers.get("content-type"))
        finally:
            limits.ASK_PER_DAY = real

        print("\n=== 会话不存在时把额度退回去 ===")
        limits.reset_rate_limits()
        before = limits.used_today(uid, "ask")
        r404 = client.post("/api/chat/stream",
                           json={"message": "你好", "thread_id": "t-does-not-exist"},
                           headers=headers)
        check("404", r404.status_code == 404, r404.text[:100])
        check("额度退回去了（没白烧一次）",
              limits.used_today(uid, "ask") == before,
              f"{limits.used_today(uid, 'ask')} != {before}")

        print("\n=== 书数上限：400，且不去解析文件 ===")
        real_docs = limits.DOCS_PER_USER
        limits.DOCS_PER_USER = 0
        try:
            r = client.post(
                "/api/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 fake", "application/pdf")},
                headers=headers,
            )
            check("400", r.status_code == 400, r.text[:120])
            check("文案让他删书", "删" in r.text, r.text[:120])
        finally:
            limits.DOCS_PER_USER = real_docs

    print(f"\n合计 {ok} 项通过，{bad} 项失败")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
