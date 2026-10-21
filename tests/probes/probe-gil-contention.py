"""探针：CPU 密集型线程会不会拖慢事件循环？拖慢的机制是什么？

做法：让事件循环反复做 `await asyncio.sleep(0.01)`，量每次实际耗时。
第一轮没有干扰（干净基线），第二轮同时起一个纯 CPU 线程（一直抱着 GIL）。
如果耗时明显变大，说明事件循环被 GIL 争抢拖慢了。
"""
from __future__ import annotations

import asyncio
import threading
import time

ROUNDS = 20
SLEEP = 0.01
BUSY_SECONDS = 1.2


def burn_cpu(stop_flag: threading.Event) -> None:
    """纯 CPU 死循环，不 sleep、不 IO —— 每 5ms 被切走一次但立刻抢回来。"""
    while not stop_flag.is_set():
        pass


async def measure() -> list[float]:
    delays = []
    for _ in range(ROUNDS):
        start = time.perf_counter()
        await asyncio.sleep(SLEEP)
        delays.append((time.perf_counter() - start) * 1000)
    return delays


def summary(name: str, delays: list[float]) -> None:
    worst = max(delays)
    avg = sum(delays) / len(delays)
    print(f"  {name}: 平均 {avg:.2f} ms（目标 {SLEEP * 1000:.0f} ms），最差 {worst:.2f} ms，"
          f"超时 {sum(1 for d in delays if d > SLEEP * 1500)}/{ROUNDS} 次")


async def main() -> None:
    print(f"逻辑核数：{__import__('os').cpu_count()}")
    print()

    clean = await measure()
    summary("没有干扰时", clean)

    stop = threading.Event()
    worker = threading.Thread(target=burn_cpu, args=(stop,), daemon=True)
    worker.start()
    try:
        dirty = await measure()
    finally:
        stop.set()
        worker.join(timeout=2)
    summary("有 CPU 线程抢 GIL 时", dirty)

    print()
    ratio = (sum(dirty) / len(dirty)) / (sum(clean) / len(clean))
    print(f"  平均耗时变成 {ratio:.2f} 倍")
    print()
    print("  注意：事件循环并没有『被占住』——它照样在跑、照样被唤醒。")
    print("  变慢的原因是它和 CPU 线程在抢同一把 GIL，每段 Python 字节码都要排队。")


if __name__ == "__main__":
    asyncio.run(main())
