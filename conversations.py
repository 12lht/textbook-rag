"""会话元数据存储：会话 id、可读标题、「标题被手动改过」的标记、以及会话归属。

这一层是纯应用层：只读写 `data/` 下的三个 JSON 文件，**不 import 任何 agent 相关的东西**
（只从 `users.py` 取一个默认账号常量，那是身份层，同样和 agent 无关）。
Agent 编排（LangGraph 图、工具、模型、消息状态）都在 `agent_core.py`；
把两边拼成用例的是 `service.py`。

三个文件是拆开的，不是「多此一举」：`conversation_summaries.json` 是扁平 dict
（`thread_id -> 标题`），被 `/api/conversations` 和前端直接消费；往里加字段会牵动
太多地方。所以「重命名标记」和「归属」各自单独一个文件，格式都一样：

```text
conversation_summaries.json  {thread_id: 标题}
conversation_renames.json    {thread_id: true}
conversation_owners.json     {thread_id: user_id}
conversation_touched.json    {thread_id: ISO 时间}
```

第四个（`conversation_touched`）是 2026-10-22 加的，给**访客会话清理**用：
访客的会话不归任何人管，不清就会一直堆在 checkpoint 里。为什么不塞进
`summaries`：那个文件是 `thread_id -> 标题` 的扁平结构，前端直接消费，
加字段要动所有读它的地方。

## 归属为什么要有兜底

本次改造之前建的会话，在归属文件里**没有记录**。它们当时全部属于硬编码的
`user_id = "1"`，所以查不到就按默认账号算——这是**把旧事实补上，不是猜**。
兜底只写在 `_owner_from` 一处，凡是判断「这个会话是不是你的」都经过它，
免得某个入口忘了兜底、把老会话判成别人的（那是让用户丢数据）。

归属文件里**不删残留**：会话被删掉后若归属记录还在，`ids_of_user` 也不受影响，
因为它以**会话表**为准去遍历（见那里的注释）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from users import GUEST_USER_ID

DATA_DIR = Path(__file__).with_name("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY_PATH = DATA_DIR / "conversation_summaries.json"
RENAME_PATH = DATA_DIR / "conversation_renames.json"
OWNER_PATH = DATA_DIR / "conversation_owners.json"
TOUCH_PATH = DATA_DIR / "conversation_touched.json"

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


def load_conversation_owners() -> dict[str, str]:
    return _load(OWNER_PATH)


def save_conversation_owners(owners: dict[str, str]) -> None:
    _save(OWNER_PATH, owners)


def load_touched() -> dict[str, str]:
    return _load(TOUCH_PATH)


def save_touched(touched: dict[str, str]) -> None:
    _save(TOUCH_PATH, touched)


def _now_stamp() -> str:
    """UTC ISO 时间戳，秒级。统一用 UTC，和 `users._stamp` 的习惯一致。

    只用来比「多久没动静」，不带时区对人类阅读的困扰不影响判断。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def touch_conversation(thread_id: str) -> None:
    """记下这个会话最后一次有动静的时刻。

    **清理只靠这个时间戳**：checkpoint 库里没有可靠的时间列（LangGraph 的
    `checkpoints` 表只有 `thread_id` / `checkpoint_id`），拿不到「上次聊天是什么时候」。
    每次发消息顺手写一次，清理时才有判据。
    """
    touched = load_touched()
    touched[str(thread_id)] = _now_stamp()
    save_touched(touched)


def age_days(thread_id: str, touched: dict[str, str] | None = None) -> float | None:
    """这个会话最后一次有动静是几天前。没有记录返回 None（调用方自己决定怎么办）。

    解析失败也返回 None：时间戳是辅助信息，不该因为它格式坏了就把清理流程
    带崩——那属于「清理代码自己制造故障」。
    """
    stamp = (touched if touched is not None else load_touched()).get(str(thread_id))
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() / 86400


def is_renamed(thread_id: str) -> bool:
    return bool(load_renamed_flags().get(thread_id))


def clear_renamed_flag(thread_id: str) -> None:
    flags = load_renamed_flags()
    if flags.pop(thread_id, None) is not None:
        save_renamed_flags(flags)


def conversation_exists(thread_id: str) -> bool:
    return thread_id in load_conversation_summaries()


def _owner_from(owners: dict[str, str], thread_id: str) -> str:
    """归属规则**只在这一处**：没记录的算默认账号（理由见模块开头）。"""
    return owners.get(thread_id) or GUEST_USER_ID


def owner_of(thread_id: str) -> str:
    """这个会话属于谁。**任何越权判断都必须经过它。**

    注意它对**不存在**的会话也会返回默认账号——它只回答「归属」，
    不回答「存不存在」。要连存在性一起判，用 `belongs_to`。
    """
    return _owner_from(load_conversation_owners(), thread_id)


def ids_of_user(user_id: str) -> set[str]:
    """这个用户名下的全部 thread_id。

    以**会话表**为准去遍历（不是以归属表为准），好处是「会话已删、归属记录
    还留在文件里」的残渣自动被滤掉，不用专门去清理那个文件。

    归属判断走 `_owner_from`，所以老会话（没有记录的）会正确落到默认账号名下。
    """
    owners = load_conversation_owners()
    return {
        thread_id
        for thread_id in load_conversation_summaries()
        if _owner_from(owners, thread_id) == user_id
    }


def belongs_to(thread_id: str, user_id: str) -> bool:
    """会话存在、且属于这个用户。**越权判断只走这一个入口。**"""
    return conversation_exists(thread_id) and owner_of(thread_id) == user_id


def new_thread_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


def create_conversation(user_id: str) -> str:
    """登记一个新会话、记下归属，返回 thread_id。只写元数据，不碰 Agent。

    `user_id` **不给默认值**：让每个调用方都必须先想清楚「这是谁的会话」。
    给个默认值的话，新写的入口很容易忘传，会话就静悄悄挂到默认账号名下了。
    """
    thread_id = new_thread_id()
    summaries = load_conversation_summaries()
    summaries[thread_id] = DEFAULT_SUMMARY
    save_conversation_summaries(summaries)

    owners = load_conversation_owners()
    owners[thread_id] = str(user_id)
    save_conversation_owners(owners)
    touch_conversation(thread_id)
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
    """删掉一个会话的四份元数据，返回它此前是否存在。等价于 `drop_conversations([id])`。

    四份都要删：留着归属记录不会让会话「复活」（`ids_of_user` 以会话表为准），
    但会一直堆在文件里，而且万一 thread_id 被复用就会串号。时间戳同理——
    留着它的唯一后果是清理时多一次无用的 IO。
    """
    return drop_conversations([thread_id]) > 0


def drop_conversations(thread_ids: list[str]) -> int:
    """批量删元数据，**只重写一次文件**。返回实际删掉几个。

    和 `drop_conversation` 的关系：单个删除复用这里（传一个元素的列表）。
    需要批量版本的唯一原因是清理：访客会话一次可能清掉几十上百个，
    逐个调 `drop_conversation` 会把四个 JSON 文件各重写上百遍（O(n²) IO）。

    *不* 检查会话是否属于谁——归属判断在 `service`，这一层只负责「删」。
    调用方必须是已经判定过可以删的（见 `service.purge_stale_guests`）。
    """
    wanted = {str(thread_id) for thread_id in thread_ids}
    if not wanted:
        return 0

    summaries = load_conversation_summaries()
    removed = [thread_id for thread_id in wanted if thread_id in summaries]
    if not removed:
        return 0
    for thread_id in removed:
        del summaries[thread_id]
    save_conversation_summaries(summaries)

    flags = load_renamed_flags()
    owners = load_conversation_owners()
    touched = load_touched()
    changed_flags = changed_owners = changed_touched = False
    for thread_id in removed:
        if flags.pop(thread_id, None) is not None:
            changed_flags = True
        if owners.pop(thread_id, None) is not None:
            changed_owners = True
        if touched.pop(thread_id, None) is not None:
            changed_touched = True

    if changed_flags:
        save_renamed_flags(flags)
    if changed_owners:
        save_conversation_owners(owners)
    if changed_touched:
        save_touched(touched)
    return len(removed)
