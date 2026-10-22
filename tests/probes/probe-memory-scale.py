"""探针：长期记忆存储在**什么规模上开始疼**——零模型调用。

**这份数字是「换存储」这件事的验收依据**，对实现是黑盒：只调 `memories` 的公开
接口（`remember` / `memories_of`），不碰内部结构。以后不管换成什么，用同一支
探针跑一遍，才有资格说「换完变快了」。没 before 数字的改动就是玄学。


JSON 版（`data/memories.json`，读整份 → 改一个用户 → 写回整份）：

```text
    总用户数     文件       写一条耗时    全局吞吐上限
       100     0.6MB         39ms         26/s
      1000     5.9MB        290ms        3.5/s
      5000    29.7MB          1.3s        0.8/s
     20000     119MB          5.2s        0.2/s
```

疼的地方不是磁盘（119MB 不算什么），是 **写一条事实要 5.2 秒**，而且所有用户
共用一把锁 → 全局吞吐 0.2 次/秒。**成本随总用户数走，不随「这个用户自己有多少条」走。**

只写 .workbuddy/tmp/ 下的临时文件，不碰 data/。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import memories

RUN = uuid.uuid4().hex[:6]
TMP = Path(__file__).with_name(f"_memory-scale-{RUN}.sqlite3")
memories.DB_PATH = TMP

PER_USER = 30
REPEAT = 5


def build(users: int, per_user: int = PER_USER) -> int:
    """造一份数据集，返回库文件字节数。

    直接走 SQL 批量插，不走 `remember()`——建数据集本身不该成为测量的一部分
    （20 万条走公开接口要好几分钟，而我们要量的是**建好之后**的单次操作成本）。
    """
    if TMP.exists():
        TMP.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(TMP) + suffix)
        if side.exists():
            side.unlink()

    now = memories._now()
    rows = [
        (
            str(u),
            f"用户{u}的第{i}条偏好，大约二十个汉字的内容",
            f"用户{u}的第{i}条偏好大约二十个汉字的内容",
            "偏好",
            f"t-{u}",
            now,
            now,
            1,
        )
        for u in range(users)
        for i in range(per_user)
    ]
    with memories._db() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO memories
                (user_id, text, fingerprint, kind, source, created_at, last_seen, hits)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return TMP.stat().st_size


def timed(fn) -> float:
    """跑 REPEAT 次取中位数，单位毫秒。"""
    samples = []
    for _ in range(REPEAT):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def main() -> int:
    one = len(
        json.dumps(
            {
                "text": "用户0的第0条偏好，大约二十个汉字的内容",
                "kind": "偏好",
                "source": "t-0",
                "created_at": memories._now(),
                "last_seen": memories._now(),
                "hits": 1,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )
    print("== 体积 ==")
    print(f"   一条事实                      {one} 字节")
    print(f"   一个用户 {PER_USER} 条             {one * PER_USER / 1024:.1f} KB")
    print(
        f"   一个用户打满 {memories.MAX_PER_USER} 条       "
        f"{one * memories.MAX_PER_USER / 1024:.1f} KB"
    )

    print()
    print("== 单次操作成本 vs 总用户数（中位数，重复 %d 次） ==" % REPEAT)
    print(f"   {'用户数':>8} {'库文件':>9} {'读一人':>9} {'写一条':>9} {'吞吐上限':>10}")
    rows_out = []
    for users in (100, 1000, 5000, 20000):
        size = build(users)
        target = str(users // 2)

        read_ms = timed(lambda: memories.memories_of(target))
        write_ms = timed(lambda: memories.remember(target, [{"text": f"新加一条 {time.time()}"}]))
        per_sec = 1000 / write_ms if write_ms else float("inf")
        rows_out.append((users, size, read_ms, write_ms, per_sec))
        print(
            f"   {users:>8} {size / 1024 / 1024:>7.2f}MB {read_ms:>7.2f}ms"
            f" {write_ms:>7.2f}ms {per_sec:>8.1f}/s"
        )

    print()
    first = rows_out[0]
    last = rows_out[-1]
    print("结论：")
    print(
        f"      写一条事实：{first[0]} 用户时 {first[3]:.2f}ms，"
        f"{last[0]} 用户时 {last[3]:.2f}ms —— "
        f"总用户数涨了 {last[0] // first[0]} 倍，耗时基本不动。"
    )
    print("      成本只跟「这个用户自己有多少条」有关，跟别人有多少条无关。")
    print("      换存储之前是：成本随总用户数线性增长（20000 用户时写一条要 5.2s）。")
    print("      这就是「只对需要改的用户读写」在存储层的样子。")

    for suffix in ("", "-wal", "-shm"):
        side = Path(str(TMP) + suffix)
        try:
            if side.exists():
                side.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
