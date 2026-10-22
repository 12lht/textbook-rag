"""探针：长期记忆**存储层**（`memories.py`）的行为——零模型调用。

只测这一层：去重、清洗、上限、锁。因为它是唯一一份能保证「模型输出再脏也不落盘」
的地方，所以值得单独盯着。

另外两个探针分工（拆开是为了各自零成本或低成本）：
  - `probe-memory-tools.py`  工具装配：注册条件、schema、ToolNode 注入、并发写
  - `probe-memory-live.py`   真模型：会不会自己调 save_memory

历史说明：2026-10-10 之前这里还测「抽取层」（`agent_core.extract_memories`，
每轮用模型从对话里提炼事实）和「编排层」（`service._remember` 后台任务）。
那套已经删掉了——记忆改由模型通过 `save_memory` 工具自己触发，
所以对应的小节也一并移除，免得测一堆已经不存在的函数。
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import memories

HERE = Path(__file__).with_name
LEGACY = HERE("probe-legacy-memories.json")
RUN = uuid.uuid4().hex[:8]  # 每次运行换个随机串
_created: list[Path] = []
TMP = HERE("probe-memories-boot.sqlite3")  # 先占个位置，fresh_db() 会换掉
memories.DB_PATH = TMP
memories.LEGACY_JSON_PATH = LEGACY

RESULTS: list[bool] = []


def fresh_db() -> None:
    """**换一个全新的库路径**，而不是去删旧文件。

    两个原因：一是沙箱会把 `unlink` 改道到回收站，对 `.sqlite3` 实测会失败
    （`Some operations were aborted`）；二是换路径本来就比删文件干净——
    各节之间互不干扰，也不需要操心 WAL 那两个边车文件删没删干净。

    **名字里带随机串**，不能用一个每次运行都从 0 开始的计数器：上一轮遗留的库
    如果没删干净，这一轮就会接着用，而 `journal_mode` 是写进库文件头的持久设置——
    拿一个「上一轮设成 WAL」的库来做并发测试，测的就不是当前代码了（真踩过）。
    """
    global TMP
    TMP = HERE(f"probe-memories-{RUN}-{len(_created)}.sqlite3")
    _created.append(TMP)
    memories.DB_PATH = TMP


def check(label: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append(bool(condition))
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return bool(condition)


def test_storage() -> None:
    print("== 1. 去重、清洗、上限 ==")
    fresh_db()

    result = memories.remember(
        "u1",
        [
            {"text": "不吃香菜", "kind": "偏好"},
            "我是北邮大三学生",
            {"text": "  不吃香菜。  ", "kind": "偏好"},  # 归一化后与第一条相同
            {"text": "", "kind": "偏好"},  # 空文本要跳过
            {"nope": 1},  # 形状不对要跳过
            {"text": "明年三月结婚", "kind": "完全不认识的类别"},
        ],
        source_thread="t-1",
    )
    print(f"   remember -> {result}")
    check("新增 3 条、合并 1 条", result["added"] == 3 and result["merged"] == 1, str(result))

    entries = memories.memories_of("u1")
    texts = [item["text"] for item in entries]
    check("脏数据没落盘", len(entries) == 3, str(texts))
    check("未知 kind 归到「其他」", any(i["kind"] == "其他" for i in entries))

    dup = next(i for i in entries if "香菜" in i["text"])
    check("重复那条只累加 hits", dup["hits"] == 2, f"hits={dup['hits']}")
    check("首次出现就带 hits=1", all(i["hits"] >= 1 for i in entries))

    memories.remember("u1", [{"text": "不吃香菜"}])
    again = next(i for i in memories.memories_of("u1") if "香菜" in i["text"])
    check("第三次出现 hits=3", again["hits"] == 3, f"hits={again['hits']}")

    check("用户之间互不串味", memories.memories_of("u2") == [])

    check(
        "读出顺序 = 首次写入顺序（重复的那条没被挪到最后）",
        texts == ["不吃香菜", "我是北邮大三学生", "明年三月结婚"],
        str(texts),
    )

    check("forget 能按归一化文本删掉", memories.forget("u1", "不吃香菜。"))
    check("删完真的少了一条", len(memories.memories_of("u1")) == 2)
    check("forget 不存在的返回 False", memories.forget("u1", "从没说过的话") is False)

    print()
    print("   库里长这样：")
    with memories._db() as conn:
        rows = conn.execute(
            "SELECT user_id, text, kind, hits FROM memories ORDER BY rowid"
        ).fetchall()
    for row in rows:
        print("   " + json.dumps(dict(row), ensure_ascii=False))
    print()


def test_limits() -> None:
    print("== 2. 长度截断与条数上限 ==")
    long_text = "很长的内容" * 100
    memories.remember("u3", [{"text": long_text}])
    stored = memories.memories_of("u3")[0]["text"]
    check("单条被截到上限内", len(stored) <= memories.MAX_TEXT_LENGTH, f"{len(stored)} 字")

    memories.remember("u4", [{"text": f"第 {i} 条"} for i in range(memories.MAX_PER_USER + 20)])
    kept = memories.memories_of("u4")
    check("每个用户有条数上限", len(kept) == memories.MAX_PER_USER, f"{len(kept)} 条")

    check("clear 清空并返回条数", memories.clear("u4") == memories.MAX_PER_USER)
    check("清空后真的没了", memories.memories_of("u4") == [])
    print()


def test_concurrent() -> None:
    print("== 3. 并发写（模型一条消息发多个工具调用时会真并发） ==")
    def race(user: str, count: int, same: bool = False) -> tuple[int, int, list[str]]:
        """让 count 个线程尽量同时写，返回（落盘条数，抛异常的线程数，异常样本）。

        `same=True` 时所有线程写**同一条**事实，专门轰击唯一约束。
        """
        fresh_db()
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []
        guard = threading.Lock()

        def worker(index: int) -> None:
            barrier.wait()
            try:
                text = "同一条事实" if same else f"事实 {index}"
                memories.remember(user, [{"text": text}])
            except BaseException as exc:  # noqa: BLE001 — 探针要的是统计，不是崩溃
                with guard:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        samples = [f"{type(e).__name__}: {e}" for e in errors[:2]]
        return len(memories.memories_of(user)), len(errors), samples

    kept, failed, samples = race("u-race", 8)
    print(f"   8 个线程并发写不同的事实 -> 落盘 {kept} 条，失败 {failed} 次")
    for sample in samples:
        print(f"      异常样本：{sample}")
    check("一条都没丢、一次都没报错", kept == 8 and failed == 0, f"{kept} 条 / {failed} 次异常")

    dup_kept, dup_failed, dup_samples = race("u-dup", 8, same=True)
    hits = memories.memories_of("u-dup")[0]["hits"] if dup_kept else 0
    print(f"   8 个线程并发写**同一条** -> 落盘 {dup_kept} 条，hits={hits}，失败 {dup_failed} 次")
    for sample in dup_samples:
        print(f"      异常样本：{sample}")
    check("去重没被并发冲垮（只留 1 条）", dup_kept == 1, f"{dup_kept} 条")
    check("每次写都算进 hits", hits == 8 and dup_failed == 0, f"hits={hits} / {dup_failed} 次异常")
    print()


def test_legacy_import() -> None:
    print("== 4. 老 JSON 一次性迁移 ==")
    fresh_db()
    LEGACY.write_text(
        json.dumps(
            {
                "old-user": [
                    {
                        "text": "老数据里的偏好",
                        "kind": "偏好",
                        "source": "t-old",
                        "created_at": "2026-01-01T00:00:00+08:00",
                        "last_seen": "2026-02-02T00:00:00+08:00",
                        "hits": 7,
                    },
                    {"text": "老的脏数据", "kind": "不认识的类别"},
                ],
                "another-user": [{"text": "别人的"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stored = memories.memories_of("old-user")
    print(f"   导入 {len(stored)} 条：{[i['text'] for i in stored]}")
    check("老数据进了库", len(stored) == 2)
    check("时间戳没有被改成现在", stored[0]["created_at"] == "2026-01-01T00:00:00+08:00")
    check("hits 原样保留", stored[0]["hits"] == 7)
    check("未知 kind 照样归「其他」", stored[1]["kind"] == "其他")
    check("别的用户也导进来了", len(memories.memories_of("another-user")) == 1)
    check("老文件被改名成 .migrated", not LEGACY.exists() and LEGACY.with_name(LEGACY.name + ".migrated").exists())

    check("不会重复导入", len(memories.memories_of("old-user")) == 2)
    print()


def cleanup() -> None:
    """收尾：尽量删掉临时库。删不掉也不影响结论，只是留几个文件在 tmp 里。"""
    for path in [LEGACY, LEGACY.with_name(LEGACY.name + ".migrated"), *_created]:
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(path) + suffix)
            try:
                if side.exists():
                    side.unlink()
            except OSError:
                pass


def main() -> int:
    try:
        test_storage()
        test_limits()
        test_concurrent()
        test_legacy_import()
    finally:
        cleanup()

    passed = sum(RESULTS)
    print("结果:", f"{passed}/{len(RESULTS)} 通过" if all(RESULTS) else f"有失败（{passed}/{len(RESULTS)}）")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
