"""探针：走**完整生产链路**（`service.chat_events`）跑一轮，看记忆是否真的落盘。

前两个探针一个直接调 ToolNode、一个直接调 `stream_agent` 并手动注入后端。
这个补上最后一环：走 api/cli 真正会走的那条路——`service` 自己注入后端、
自己起标题、自己转发事件——确认整条链没断。

代价：一次真实模型调用。所有数据文件都指到临时文件，不碰 data/ 下的真东西。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent_core
import conversations
import memories
import service

# 全部改到临时文件——这个探针会真的建会话、写标题、写记忆
#
# **`memories` 的变量名是 `DB_PATH`**（2026-10-10 从 JSON 换 SQLite 时改的名）。
# 之前这里写的是 `memories.MEMORY_PATH`：赋值不报错、也不生效，只是给模块加了个
# 没人读的属性，结果探针一直往真实的 data/memories.sqlite3 里写。
# 换了个存储就悄悄失效——**探针里的「指向临时文件」这两行，改名时要一起改。**
SUFFIX = uuid.uuid4().hex[:6]
memories.DB_PATH = Path(__file__).with_name(f"probe-svc-memories-{SUFFIX}.sqlite3")
conversations.SUMMARY_PATH = Path(__file__).with_name(f"probe-svc-summaries-{SUFFIX}.json")
conversations.RENAME_PATH = Path(__file__).with_name(f"probe-svc-renames-{SUFFIX}.json")
# 会话归属（2026-10-10 加登录时新增的第三个文件）。漏了它探针会往真实 data/ 里写。
conversations.OWNER_PATH = Path(__file__).with_name(f"probe-svc-owners-{SUFFIX}.json")

TMP_FILES = [
    memories.DB_PATH,
    conversations.SUMMARY_PATH,
    conversations.RENAME_PATH,
    conversations.OWNER_PATH,
]

# 探针建的会话，收尾时清掉，别把 checkpoint 撑大
_THREADS: list[str] = []

RESULTS: list[bool] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append(bool(condition))
    print(f"   {'✓' if condition else '✗'} {label}{('  ' + detail) if detail else ''}")
    return bool(condition)


async def main() -> int:
    user_id = f"svc-user-{SUFFIX}"
    # 建会话、跑图都要显式给身份了（登录改造之后）。
    # `memory_enabled` 走默认 True，对应「登录用户」那一档。
    created = service.create_conversation(user_id)
    thread_id = created["thread_id"]
    _THREADS.append(thread_id)

    print("== 走 service.chat_events 跑一轮（真模型） ==")
    question = "记住：我对花生过敏，别给我推荐带花生的菜。"
    kinds: list[str] = []
    tools: list[str] = []
    async for event in service.chat_events(user_id, thread_id, question):
        kinds.append(str(event.get("type")))
        if event.get("type") == "tool_call":
            tools.append(str(event.get("tool")))
    print(f"   事件类型：{kinds}")
    print(f"   工具调用：{tools}")

    check("链路没断（收到了 assistant_delta）", "assistant_delta" in kinds)
    check("会话标题照常生成", "title" in kinds)
    check("模型调了记忆工具", any(t in {"save_memory", "recall_memory"} for t in tools), f"{tools}")

    stored = memories.memories_of(user_id)
    print(f"   落盘：{[e['text'] for e in stored]}")
    check("记忆真的存进去了（用的是 service 注入的后端）", len(stored) >= 1)
    check("记住了过敏这件事", any("花生" in e["text"] for e in stored))
    check("来源会话对得上", all(e.get("source") == thread_id for e in stored))

    print()
    print("结果:", f"{sum(RESULTS)}/{len(RESULTS)} 通过" if all(RESULTS) else "有失败")
    return 0 if all(RESULTS) else 1


async def run() -> int:
    try:
        return await main()
    finally:
        for thread_id in _THREADS:
            try:
                await agent_core.delete_checkpoint_thread(thread_id)
            except Exception:
                pass
        await service.shutdown()
        for path in TMP_FILES:
            # SQLite 会有 -wal / -shm 边车文件，一起收
            for suffix in ("", "-wal", "-shm"):
                side = Path(str(path) + suffix)
                try:
                    if side.exists():
                        side.unlink()
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
