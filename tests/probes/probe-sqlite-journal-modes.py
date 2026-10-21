"""探针：SQLite 各种**日志模式**下一次「插一行 + 提交」要多久。

为什么关心：`memories.py` 换到 SQLite 之后，写入成本已经和总用户数无关了
（`probe-memory-scale.py` 量过），但绝对值停在 ~30ms。拆到语句级发现
connect 0.9ms / INSERT 0.9ms / **commit 27ms** / close 0.1ms，而且裸 sqlite3
（绕开本模块）也是 28.8ms —— 所以跟我们的代码无关，是提交本身。

本探针把「提交本身」拆开，结论（2026-10-10 实测）：

```text
      DELETE（SQLite 默认）   33.0ms   ← 每次提交都建一个 -journal，提交完删掉
    TRUNCATE                  2.9ms   ← 文件建一次，之后原地截断
     PERSIST                   3.0ms   ← 文件留着，只清头部
      MEMORY                   0.8ms   ← 日志只在内存，崩溃就没
         OFF                   0.7ms
         WAL                   2.2ms   ← 但本环境并发写不可用（见 memories.py）
```

贵的就是 **DELETE 模式下每次提交都要新建 + 删除一个 `-journal` 文件**。
本环境里「建一个文件再删掉」极其贵：Python 层的 `open(w)` + `unlink()` 一对
实测 ~1.6 秒（另外有 `_floor4` 的对照，已删）；SQLite 走 C 层，一对约 30ms。

**没有据此改代码**：30ms 对「隔几轮才写一次记忆」完全够用，而且这个数字很可能是
本沙箱文件拦截的特性、不代表用户机器上的真实成本（SQLite 官方文档里，
PERSIST 正是为「删文件很慢的系统」准备的）。要改是个带持久化语义权衡的决定，
留给用户拍板。
"""
import sqlite3, time, statistics
from pathlib import Path

HERE = Path(__file__).with_name
TMP = HERE("_journal-modes.sqlite3")

def bench(model, n=12):
    for s in ("", "-wal", "-shm", "-journal"):
        p = Path(str(TMP) + s)
        if p.exists(): p.unlink()
    c = sqlite3.connect(str(TMP))
    c.execute("CREATE TABLE IF NOT EXISTS m (u TEXT, t TEXT)")
    c.execute(f"PRAGMA journal_mode={model}")
    c.commit()
    actual = c.execute("PRAGMA journal_mode").fetchone()[0]
    c.close()

    xs = []
    for i in range(n):
        c = sqlite3.connect(str(TMP))
        c.execute(f"PRAGMA journal_mode={model}")
        t = time.perf_counter()
        c.execute("INSERT INTO m VALUES (?, ?)", ("u", f"t{i}"))
        c.commit()
        xs.append((time.perf_counter() - t) * 1000)
        c.close()
    print(f"   {model:>9}  (实际 {actual:>8})  {statistics.median(xs):8.2f} ms")

print("== 一次「插一行 + 提交」在各种日志模式下 ==")
for model in ("DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF", "WAL"):
    try:
        bench(model)
    except Exception as e:
        print(f"   {model:>9}  失败: {type(e).__name__}: {e}")

for s in ("", "-wal", "-shm", "-journal"):
    p = Path(str(TMP) + s)
    if p.exists(): p.unlink()
