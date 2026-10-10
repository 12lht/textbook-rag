"""录制回放：把工具的返回值冻结下来，让评测前后可比。

## 为什么需要它

外部 API 的返回内容每次都在变：搜索结果正文长度实测波动 1.1~2.5 倍，天气和时间
也在变。这些波动**远大于**被测的代码改动，导致评测分不清差异是改动带来的还是
外部内容带来的（见 learning-notes.md 第 16 节）。

做法：按「工具名 + 参数」给每次外部调用存一份返回值。评测先跑一轮 record，
之后都走 replay，两次运行拿到的工具输出逐字一致，差异只可能来自代码和模型。

## 三种模式

环境变量 `TOOL_CASSETTE_MODE`（**调用时读取**，不是 import 时）：

    off     默认。直连外部 API，不读不写。生产路径走的就是这个分支。
    record  正常调用外部 API，把结果追加进 cassette。已存在的键不覆盖，
            所以可以反复跑 record 来「加宽」覆盖面（并集）。
    replay  只读 cassette。命中就返回录下来的内容；未命中则**照常真实调用**
            并计入 miss，这样评测不会因为缺料而崩，同时 miss 数会如实报出来。

## 为什么录制点放在工具函数这一层

改造前后工具跑在不同的地方（改造后在 MCP 子进程里），但**工具函数体是同一段代码**。
把录制点放在这里，两版代码共用同一套机制、同一个 cassette 文件，A/B 才算是同一个
实验。如果录制点放在 agent_core 那一层，改造前要包一层、改造后要包另一层，两边
机制不同，对比就又不干净了。

## 文件位置

`TOOL_CASSETTE_PATH` 指定，默认 `eval/cassettes/default.json`。
只依赖标准库，因为 `tools.py` 会被 MCP server 子进程加载，那一层不许有框架依赖。
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_PATH = Path(__file__).resolve().parent / "eval" / "cassettes" / "default.json"

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_path: str | None = None


def mode() -> str:
    """当前模式。每次调用都重新读环境变量，方便同一进程内切换。"""
    value = (os.getenv("TOOL_CASSETTE_MODE") or "off").strip().lower()
    return value if value in {"off", "record", "replay"} else "off"


def path() -> Path:
    raw = os.getenv("TOOL_CASSETTE_PATH")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_PATH


def _empty() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interactions": {},
        "counters": {"records": 0, "hits": 0, "misses": 0},
    }


def _key(tool_name: str, arguments: dict[str, Any]) -> str:
    """键 = 工具名 + 规范化后的参数。sort_keys 保证参数顺序不影响键。"""
    payload = json.dumps(
        {"tool": tool_name, "args": arguments}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load() -> dict[str, Any]:
    """带缓存的读取。子进程和评测进程各有一份缓存，文件坏了也不阻塞评测。"""
    global _cache, _cache_path
    target = path()
    if _cache is not None and _cache_path == str(target):
        return _cache
    data = _empty()
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = _empty()
    data.setdefault("interactions", {})
    counters = data.setdefault("counters", {})
    for name in ("records", "hits", "misses"):
        counters.setdefault(name, 0)
    _cache, _cache_path = data, str(target)
    return data


def _flush(data: dict[str, Any]) -> None:
    """先写临时文件再原子替换，避免并发或中途退出写坏 cassette。"""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(temporary, target)


def counters() -> dict[str, int]:
    """直接从磁盘读累计计数。评测进程用它算前后差值。"""
    target = path()
    if not target.exists():
        return {"records": 0, "hits": 0, "misses": 0}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"records": 0, "hits": 0, "misses": 0}
    stored = data.get("counters", {})
    return {name: int(stored.get(name, 0)) for name in ("records", "hits", "misses")}


def interaction_count() -> int:
    target = path()
    if not target.exists():
        return 0
    try:
        return len(json.loads(target.read_text(encoding="utf-8")).get("interactions", {}))
    except (json.JSONDecodeError, OSError):
        return 0


def recorded(tool_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """给工具函数加录制回放。用 functools.wraps 保留 docstring 和签名。

    这一点很关键：`tools.py` 里每个函数的 docstring 就是模型看到的工具描述，
    `mcp_server.py` 还要靠 `inspect.signature` 生成参数 schema。wraps 会复制
    `__doc__` 并设置 `__wrapped__`，两者都不受影响（改完用 probe-mcp-server.py 核对过）。
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        # 签名在装饰时算一次就够了，不必每次调用都算。
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            current = mode()
            if current == "off":
                return func(*args, **kwargs)

            bound = signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            key = _key(tool_name, arguments)

            if current == "replay":
                with _lock:
                    data = _load()
                    entry = data["interactions"].get(key)
                    if entry is not None:
                        data["counters"]["hits"] += 1
                        _flush(data)
                        return entry["value"]
                    data["counters"]["misses"] += 1
                    _flush(data)
                return func(*args, **kwargs)

            value = func(*args, **kwargs)
            with _lock:
                data = _load()
                if key not in data["interactions"]:
                    data["interactions"][key] = {
                        "tool": tool_name,
                        "args": arguments,
                        "value": value,
                        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    data["counters"]["records"] += 1
                    _flush(data)
            return value

        return wrapper

    return decorate
