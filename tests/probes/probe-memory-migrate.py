"""探针：老记忆库（没有 `id` 列）升级上来会不会丢东西。

`id` 是后加的列，生产库里已经躺着一批老行。SQLite 的
`CREATE TABLE IF NOT EXISTS` **不会给已存在的表加列**，所以必须显式
`ALTER TABLE`，而这一步最容易把数据弄坏（漏值、覆盖、重复触发）。

这里手造一个旧结构的库来跑，验四件事：
1. 自动补上 `id`；
2. 每行 id 非空、用户内唯一；
3. 原有字段一个没变；
4. 幂等——再连一次 id 不变、行数不变。

另附一节：老 `memories.json` 迁移进新版库时，新列也得有值。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:/agent")
sys.path.insert(0, str(ROOT))
import memories  # noqa: E402

passed = failed = 0


def check(label: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}  {extra}")


OLD_SCHEMA = """
CREATE TABLE memories (
    user_id     TEXT NOT NULL,
    text        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    kind        TEXT NOT NULL,
    source      TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, fingerprint)
);
"""

OLD_ROWS = [
    ("u-old", "先给结论再展开", "先给结论再展开", "偏好", "t-9", "2026-09-01T10:00:00+08:00", "2026-09-20T08:00:00+08:00", 4),
    ("u-old", "叫我涛哥", "叫我涛哥", "身份", "t-9", "2026-09-02T10:00:00+08:00", "2026-09-21T08:00:00+08:00", 7),
    ("u-old", "在复习移动通信第3章", "在复习移动通信第3章", "计划", None, "2026-09-03T10:00:00+08:00", "2026-09-03T10:00:00+08:00", 1),
    ("u-other", "在做昇腾部署", "在做昇腾部署", "事实", "t-3", "2026-09-04T10:00:00+08:00", "2026-09-05T10:00:00+08:00", 2),
]


def point_at(db: Path, legacy: Path) -> None:
    memories.DATA_DIR = db.parent
    memories.DB_PATH = db
    memories.LEGACY_JSON_PATH = legacy


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="probe-memory-migrate-"))
    db = tmp / "memories.sqlite3"
    legacy = tmp / "memories.json"

    print("=== 造一个旧结构的库 ===")
    conn = sqlite3.connect(str(db))
    conn.executescript(OLD_SCHEMA)
    conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)", OLD_ROWS)
    conn.commit()
    conn.close()
    check("旧库没有 id 列", "id" not in [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(memories)")])

    point_at(db, legacy)

    print("\n=== 连一次，让它自己升级 ===")
    rows = memories.memories_of("u-old")
    check("三行都读出来了", len(rows) == 3, str(len(rows)))
    check("每行都有 id", all(r["id"] for r in rows), str([r.get("id") for r in rows]))
    check("id 互不相同", len({r["id"] for r in rows}) == 3, str([r["id"] for r in rows]))
    check("id 是 12 位十六进制", all(len(r["id"]) == 12 and all(c in "0123456789abcdef" for c in r["id"]) for r in rows))

    print("\n=== 原有字段一个没动 ===")
    by_text = {r["text"]: r for r in rows}
    for user_id, text, _fp, kind, source, created, seen, hits in OLD_ROWS:
        if user_id != "u-old":
            continue
        r = by_text.get(text)
        check(f"「{text}」还在", r is not None)
        if r is None:
            continue
        check(f"「{text}」kind 没变", r["kind"] == kind, f"{r['kind']} != {kind}")
        check(f"「{text}」created_at 没变", r["created_at"] == created, r["created_at"])
        check(f"「{text}」last_seen 没变", r["last_seen"] == seen, r["last_seen"])
        check(f"「{text}」hits 没变", r["hits"] == hits, f"{r['hits']} != {hits}")
        check(f"「{text}」source 没变", r.get("source") == source, str(r.get("source")))

    check("另一个用户的数据也读了", len(memories.memories_of("u-other")) == 1)

    print("\n=== 幂等：再连一次 ===")
    again = memories.memories_of("u-old")
    check("行数没变", len(again) == 3)
    check(
        "id 和上一次完全一样（没有重新生成）",
        [r["id"] for r in again] == [r["id"] for r in rows],
        f"{[r['id'] for r in again]} vs {[r['id'] for r in rows]}",
    )

    print("\n=== 升级完能正常增删改 ===")
    target = by_text["叫我涛哥"]
    check("按新 id 改得动", memories.update_text("u-old", target["id"], "称呼我为涛哥") == "ok")
    after = memories.memories_of("u-old")
    check("改完还是 3 条", len(after) == 3, str(len(after)))
    kept = next(r for r in after if r["text"] == "称呼我为涛哥")
    check("改完 id 不变", kept["id"] == target["id"], f"{kept['id']} != {target['id']}")
    check("hits 保留（老记忆没被当新条目）", kept["hits"] == 7, str(kept["hits"]))
    check("按新 id 删得掉", memories.forget_by_id("u-old", kept["id"]))
    check("删完剩 2 条", len(memories.memories_of("u-old")) == 2)

    print("\n=== 老 JSON 迁进来时也带 id ===")
    tmp2 = Path(tempfile.mkdtemp(prefix="probe-memory-migrate-json-"))
    db2, legacy2 = tmp2 / "memories.sqlite3", tmp2 / "memories.json"
    legacy2.write_text(
        json.dumps(
            {
                "u-json": [
                    {"text": "喜欢先看结论", "kind": "偏好", "created_at": "2026-08-01T09:00:00+08:00", "hits": 3},
                    {"text": "在准备保研", "kind": "计划"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    point_at(db2, legacy2)
    rows2 = memories.memories_of("u-json")
    check("两条都迁进来了", len(rows2) == 2, str(len(rows2)))
    check("迁进来的也有 id", all(r["id"] for r in rows2), str([r.get("id") for r in rows2]))
    check("id 互不相同", len({r["id"] for r in rows2}) == 2)
    check("老 JSON 被改名归档", not legacy2.exists() and (tmp2 / "memories.json.migrated").exists())
    check("hits 和历史时间保留", next(r for r in rows2 if r["text"] == "喜欢先看结论")["hits"] == 3)

    print(f"\n合计 {passed} 项通过，{failed} 项失败")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
