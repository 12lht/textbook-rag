"""会话元数据存储：会话 id、可读标题、以及「标题被手动改过」的标记。

这一层是纯应用层：只读写 `data/` 下的两个 JSON 文件，**不 import 任何 agent 相关的东西**。
Agent 编排（LangGraph 图、工具、模型、消息状态）都在 `agent_core.py`；
把两边拼成用例的是 `service.py`。

顺带说明为什么「重命名标记」单独放一个文件：`conversation_summaries.json` 的格式
（扁平 dict）被 `/api/conversations` 和前端直接消费，加字段会牵动太多地方。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY_PATH = DATA_DIR / "conversation_summaries.json"
RENAME_PATH = DATA_DIR / "conversation_renames.json"

MAX_TITLE_LENGTH = 30
DEFAULT_SUMMARY = "新会话（暂无消息）"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_conversation_summaries() -> dict[str, str]:
    return _load(SUMMARY_PATH)


def save_conversation_summaries(summaries: dict[str, str]) -> None:
    _save(SUMMARY_PATH, summaries)


def load_renamed_flags() -> dict[str, bool]:
    return _load(RENAME_PATH)


def save_renamed_flags(flags: dict[str, bool]) -> None:
    _save(RENAME_PATH, flags)


def is_renamed(thread_id: str) -> bool:
    return bool(load_renamed_flags().get(thread_id))


def clear_renamed_flag(thread_id: str) -> None:
    flags = load_renamed_flags()
    if flags.pop(thread_id, None) is not None:
        save_renamed_flags(flags)


def conversation_exists(thread_id: str) -> bool:
    return thread_id in load_conversation_summaries()


def new_thread_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


def create_conversation() -> str:
    """登记一个新会话，返回 thread_id。只写元数据，不碰 Agent。"""
    thread_id = new_thread_id()
    summaries = load_conversation_summaries()
    summaries[thread_id] = DEFAULT_SUMMARY
    save_conversation_summaries(summaries)
    return thread_id


def rename_conversation(thread_id: str, title: str) -> str:
    """手动重命名会话，返回清洗后实际保存的标题。

    清洗规则：所有空白（含换行）折叠成单个空格，去首尾，截断到 MAX_TITLE_LENGTH。
    清洗后为空抛 ValueError；会话不存在抛 KeyError。

    同时写入「已手动命名」标记：不然用户改完名字，只要在前几轮内再发一条消息，
    自动起标题就会把它覆盖掉。
    """
    cleaned = " ".join(str(title).split())[:MAX_TITLE_LENGTH]
    if not cleaned:
        raise ValueError("会话标题不能为空")

    summaries = load_conversation_summaries()
    if thread_id not in summaries:
        raise KeyError(thread_id)
    summaries[thread_id] = cleaned
    save_conversation_summaries(summaries)

    flags = load_renamed_flags()
    flags[thread_id] = True
    save_renamed_flags(flags)
    return cleaned


def set_auto_summary(thread_id: str, title: str) -> None:
    """写入自动生成的标题（不写手动命名标记）。"""
    summaries = load_conversation_summaries()
    summaries[thread_id] = title
    save_conversation_summaries(summaries)


def reset_summary(thread_id: str) -> None:
    """标题还原成默认值，并撤销手动命名标记（会话被清空时用）。"""
    summaries = load_conversation_summaries()
    if thread_id in summaries:
        summaries[thread_id] = DEFAULT_SUMMARY
        save_conversation_summaries(summaries)
    clear_renamed_flag(thread_id)


def drop_conversation(thread_id: str) -> bool:
    """删掉会话的元数据（标题 + 重命名标记）。返回它此前是否存在。"""
    summaries = load_conversation_summaries()
    if thread_id not in summaries:
        return False
    del summaries[thread_id]
    save_conversation_summaries(summaries)
    clear_renamed_flag(thread_id)
    return True
