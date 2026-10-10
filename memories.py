"""跨会话的长期记忆存储：按 `user_id` 存「下次换个会话也该记得」的事实。

和 `conversations.py` 一样，这一层是纯应用层：只读写 `data/memories.json`，
**不 import 任何 agent 相关的东西**。分工是：

- 什么时候记、记什么 —— **模型自己判断**（它手里有 `save_memory` / `recall_memory`）
- 把这两个工具接到这一层 —— `service.py` 通过 `agent_core.set_memory_backend()` 注入
- 存成什么、怎么去重 —— 就在这里

早先的做法是每轮对话结束后由 service 主动提炼一遍，现在那套已经删掉了：
改成工具触发之后，成本从「每轮必付」变成「需要时才付」，而且多了「作废旧条目」
的能力（`forget`）。

存储格式（一个 user_id 对应一条数组）：

```json
{
  "1": [
    {
      "text": "不吃香菜",
      "kind": "偏好",
      "source": "t-ab12cd34",
      "created_at": "2026-10-10T20:00:00+08:00",
      "last_seen": "2026-10-10T20:00:00+08:00",
      "hits": 2
    }
  ]
}
```

**去重按归一化后的文本**，不是语义：同一条事实反复抽到只会累加 `hits`、
刷新 `last_seen`，不会多出一条。归一化只做最小处理（去空白、去标点、转小写），
够挡住「不吃香菜。」和「不吃香菜」这种差异。

语义级的合并（「不吃香菜」和「受不了香菜」算不算一条）是以后上向量库时的事，
现在**不要**假装做到了。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_PATH = DATA_DIR / "memories.json"

# 单条事实的长度上限。模型偶尔会吐一整段话进来，截断比存下来更好。
MAX_TEXT_LENGTH = 120
# 每个用户最多存多少条。超了先丢「最久没再出现」的那些。
MAX_PER_USER = 200
# kind 只认这几个，其余一律归到「其他」。模型不守规矩时不能让脏值落盘。
KINDS = ("偏好", "身份", "计划", "事实")
DEFAULT_KIND = "其他"

_STRIP_CHARS = set(" \t\r\n，。！？、；：,.!?;:\"'“”‘’（）()【】[]《》<>-—_~`*#")

# 这是**写**的锁，不是 asyncio 的锁。原因：读写是「读整个文件 → 改 → 整份写回」，
# 两步之间一旦插进另一个写者，后写的会把先写的整份覆盖掉（不是合并）。
#
# 什么时候会并发：模型在**一条消息里发多个工具调用**时，ToolNode 用
# `asyncio.gather` 同时执行它们；而这些工具是同步函数，会各自被丢进线程池，
# 于是就真的并行跑了。实测踩到过——「不吃香菜」和「养了只猫叫豆豆」两条同时写，
# 只剩一条落盘。
#
# 用 `threading.Lock` 而不是 `asyncio.Lock`：写发生在工作线程里，
# 事件循环的锁在这里根本管不住。
#
# 单进程内够用。真要多个进程写同一份文件，得换成文件锁或数据库——那是后话。
_write_lock = threading.Lock()


def _load() -> dict:
    if not MEMORY_PATH.exists():
        return {}
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    MEMORY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _normalize(text: str) -> str:
    """归一化：去空白、去标点、转小写。**只做机械处理，不做语义**。"""
    return "".join(ch for ch in str(text).strip().lower() if ch not in _STRIP_CHARS)


def _clean_kind(kind: object) -> str:
    return kind if kind in KINDS else DEFAULT_KIND


def _clean_text(text: object) -> str:
    """折叠空白并截断。清洗后为空返回空串，由调用方跳过。"""
    return " ".join(str(text).split())[:MAX_TEXT_LENGTH]


def memories_of(user_id: str) -> list[dict]:
    """读某个用户已有的长期记忆。没有就返回空列表。"""
    stored = _load().get(str(user_id))
    return stored if isinstance(stored, list) else []


def remember(
    user_id: str,
    facts: list,
    source_thread: str | None = None,
) -> dict[str, int]:
    """把一批事实写进某个用户的长期记忆，返回 `{"added", "merged", "total"}`。

    `facts` 里每项可以是字符串，也可以是 `{"text": ..., "kind": ...}`——
    调用方直接把这层模型的输出原样丢进来即可，形状不对的项会被跳过，
    不让脏数据落盘。这样解析的容错只写在这一处。
    """
    # 整个「读—改—写」必须在锁里完成，理由见 `_write_lock`。
    with _write_lock:
        data = _load()
        key = str(user_id)
        stored = data.get(key)
        entries = list(stored) if isinstance(stored, list) else []

        # 归一化文本 -> 已有条目，用于去重
        index = {_normalize(item.get("text", "")): item for item in entries}
        now = _now()
        added = merged = 0

        for raw in facts:
            if isinstance(raw, str):
                text, kind = raw, DEFAULT_KIND
            elif isinstance(raw, dict):
                text, kind = raw.get("text", ""), raw.get("kind")
            else:
                continue

            text = _clean_text(text)
            if not text:
                continue

            fingerprint = _normalize(text)
            if not fingerprint:
                continue

            existing = index.get(fingerprint)
            if existing is not None:
                existing["hits"] = int(existing.get("hits", 1)) + 1
                existing["last_seen"] = now
                existing["kind"] = _clean_kind(kind) if kind else existing.get("kind")
                merged += 1
                continue

            entry = {
                "text": text,
                "kind": _clean_kind(kind),
                "source": source_thread,
                "created_at": now,
                "last_seen": now,
                "hits": 1,
            }
            entries.append(entry)
            index[fingerprint] = entry
            added += 1

        if len(entries) > MAX_PER_USER:
            # 先丢最久没再出现的。同一天内按加入顺序稳定排序，避免每次写入都抖动。
            entries.sort(key=lambda item: str(item.get("last_seen", "")))
            entries = entries[-MAX_PER_USER:]

        data[key] = entries
        _save(data)
        return {"added": added, "merged": merged, "total": len(entries)}


def forget(user_id: str, text: str) -> bool:
    """按文本删掉一条记忆（同样走归一化匹配）。删掉了返回 True。"""
    with _write_lock:
        data = _load()
        key = str(user_id)
        entries = data.get(key)
        if not isinstance(entries, list):
            return False

        fingerprint = _normalize(text)
        kept = [
            item for item in entries if _normalize(item.get("text", "")) != fingerprint
        ]
        if len(kept) == len(entries):
            return False

        data[key] = kept
        _save(data)
        return True


def clear(user_id: str) -> int:
    """清空某个用户的长期记忆，返回清掉了多少条。"""
    with _write_lock:
        data = _load()
        key = str(user_id)
        removed = len(data.pop(key, []) or [])
        if removed:
            _save(data)
        return removed


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
