"""会话内临时附件：目前只有图片。

和 `documents` 的区别是生命周期：这里的文件是「这一次提问用的材料」，**不解析、不切块、
不进检索池**，用完就随过期清理掉。`documents` 里每个函数都默认「有解析产物」，硬塞进去
会让 `create_upload` 到处写 if。

存储按用户分目录：`data/attachments/<user_id>/<att_id>.<ext>`。读的时候要求 user_id 与
目录一致，越权读别人的 att_id 会直接拿不到文件（不是靠调用方记得传对参数）。

为什么不用 base64 直接塞进对话历史：历史存在 langgraph checkpoint 里，**每个 checkpoint
是完整 state 快照**，图片进历史等于每轮存一份副本。详见 docs/adr/0001-session-attachments.md。
"""
from __future__ import annotations

import base64
import logging
import re
import secrets
import time
from pathlib import Path

_log = logging.getLogger("agent.attachments")

BASE_DIR = Path(__file__).resolve().parent
ATTACH_DIR = BASE_DIR / "data" / "attachments"

# 单图 8MB。DeepSeek 那边 base64 内联上限 32MB、请求体 48MB，这里远低于它——
# 前端反正会缩到长边 1024，正常只有一两百 KB，8MB 是防呆不是常态。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 临时材料留 7 天。用户可能第二天回来接着问，但没必要永久存。
TTL_SECONDS = 7 * 24 * 3600

# 两个 id 都参与拼路径，必须严格校验，否则 `../` 能穿出去。
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ATT_RE = re.compile(r"^a-[A-Za-z0-9]{16}$")

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


class AttachmentError(ValueError):
    """文件本身不能用（格式不支持、太大、不存在）。和系统故障区分开，上层回 4xx。"""


def sniff_image(data: bytes) -> str | None:
    """按文件头判断真实格式，返回 MIME 或 None。

    不相信扩展名，也不相信上传时声明的 Content-Type：两个都来自客户端。
    DeepSeek 文档同样说明格式由内容判断——我们提前挡掉，比等 API 返回 400 好排查。
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _user_dir(user_id: str) -> Path:
    if not _USER_RE.match(user_id or ""):
        raise AttachmentError("用户标识不合法")
    return ATTACH_DIR / user_id


def save_image(user_id: str, data: bytes) -> dict[str, object]:
    """存一张图，返回元信息。

    不做尺寸校验以外的任何处理（不缩放、不转码）：缩放是前端的事，后端只负责可靠地存和取。
    后端再转一次会让「用户看到的缩略图」和「模型看到的图」变成两张不同的图，排查时对不上。
    """
    if not data:
        raise AttachmentError("文件是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError(f"图片太大，上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB")
    mime = sniff_image(data)
    if mime is None:
        raise AttachmentError("只支持 JPEG / PNG / WebP / GIF")

    directory = _user_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    # token_hex(8) 固定 16 个 hex 字符，和 _ATT_RE 的 {16} 严格对齐。
    # 用 token_urlsafe 会掺进 - 和 _，长度也不固定，正则一收紧就找不到文件。
    att_id = "a-" + secrets.token_hex(8)
    path = directory / f"{att_id}.{_EXT[mime]}"
    path.write_bytes(data)

    kind = "image"
    return {
        "att_id": att_id,
        "kind": kind,
        "mime": mime,
        "bytes": len(data),
    }


def _locate(user_id: str, att_id: str) -> Path | None:
    """找到已经在磁盘上的附件。找不到返回 None（过期被清掉、或 id 不是自己的）。"""
    if not _ATT_RE.match(att_id or ""):
        return None
    try:
        directory = _user_dir(user_id)
    except AttachmentError:
        return None
    for ext in set(_EXT.values()):
        candidate = directory / f"{att_id}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def load_image(user_id: str, att_id: str) -> tuple[bytes, str] | None:
    path = _locate(user_id, att_id)
    if path is None:
        return None
    # 只读一次：先 read 再切片判格式，别为了嗅探把整文件读两遍。
    data = path.read_bytes()
    mime = sniff_image(data[:16]) or "image/jpeg"
    return data, mime


def to_data_url(user_id: str, att_id: str) -> str | None:
    """转成 data URL，直接塞进多模态 content。取不到就是 None，由调用方决定怎么降级。

    **不缓存**结果：一张图 base64 后 200KB 上下，缓存住等于把同一份数据在内存里再存一遍；
    而从磁盘读 + 编码只要几毫秒，不值得为它引入淘汰策略。
    """
    found = load_image(user_id, att_id)
    if found is None:
        return None
    data, mime = found
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def gc(ttl_seconds: int = TTL_SECONDS) -> int:
    """清掉过期附件，返回删除的文件数。启动时调一次即可，不必挂定时任务。"""
    if not ATTACH_DIR.is_dir():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for path in ATTACH_DIR.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        _log.info("清理过期附件 %s 个", removed)
    return removed
