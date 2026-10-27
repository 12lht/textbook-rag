"""图片判定：几何量判不了的那一小撮，交给 VLM 判「是教学内容还是装饰」。

只干一件事，不 import `documents`（否则成环），也不 import agent 相关的东西。

**失败一律返回 None**（调用方据此保留图）：宁可多留一张装饰图，也不能因为网络抖动
或额度用光把一张真插图删掉——删错了没法补，多留一张只是多占一点索引。

只在「灰区」被调用（见 `documents._figure_verdict`）：明确的装饰和明确的插图都不问模型，
因为一次调用 ≈2.3 秒 + 700 token，全书都问一遍比解析本身还贵。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import urllib.request
from pathlib import Path

_log = logging.getLogger("agent.documents")

KEY_ENV = "SILICON_API_KEY"
DEFAULT_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

PROMPT = """这是从教材页面里截出来的一块区域。判断它是不是教学内容的插图。

算插图：示意图、流程图、折线图、柱状图、网络拓扑、电路图、框架图、表格截图、公式截图、照片。
不算：装饰图标、Logo、项目符号、底纹、分隔线、页眉页脚的图案。

只输出一行 JSON，不要别的文字：{"figure": true 或 false, "why": "不超过 20 字"}"""

# 写图注的模型。**8B 今天完全不可用**（单张 60s 超时，或直接 HTTP 500），
# 换成 30B-A3B 后 1–6 秒稳定返回、质量还更好（MoE，激活 3B，不比 8B 贵多少）。
# 别再默认回 8B——除非先实测它当天能用。
DESC_MODEL = "Qwen/Qwen3-VL-30B-A3B-Instruct"

DESCRIBE_PROMPT = """这张教材插图没有题注。用一句中文说清它画的是什么，40 字以内。

- 说内容（对象、关系、趋势），不要写颜色、边框、阴影这类外观。
- 图里出现的文字、符号、数值要带上——它们是以后查这本书的线索。
- 不要以「这张图」「如图所示」「教材插图」开头，直接说内容。

只输出那一句话。"""


def _setting(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def available() -> bool:
    """延迟读环境变量：探针可能在 import 之后才 load_dotenv。"""
    return bool(os.getenv(KEY_ENV, "").strip())


def _encode_image(path: Path) -> tuple[str, str] | None:
    """图片 → (mime, base64)。长边先缩到 1024：MinerU 给的常是 1500px 原图，
    不缩的话 base64 体积翻几倍，32B 处理也慢——实测一张 1500px 的图会直接卡到超时。
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            rgb = img.convert("RGB")
            if max(rgb.size) > 1024:
                rgb.thumbnail((1024, 1024))
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=85)
        return "image/jpeg", base64.b64encode(buffer.getvalue()).decode()
    except Exception:  # noqa: BLE001 - 缩不了就按原样发
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        suffix = path.suffix.lower().lstrip(".") or "png"
        mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else "image/png"
        return mime, base64.b64encode(raw).decode()


def _ask_vlm(path: Path, prompt: str, model: str, max_tokens: int, timeout: float) -> str | None:
    """发一次「图 + 一句话要求」，返回模型的纯文本；任何失败都返回 None。"""
    if not available():
        return None
    encoded = _encode_image(path)
    if encoded is None:
        return None
    mime, data = encoded
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
            ],
        }],
    }).encode()

    request = urllib.request.Request(
        f"{_setting('VISION_BASE_URL', DEFAULT_BASE)}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {os.getenv(KEY_ENV, '').strip()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        return str(payload["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001 - 判不了 / 描述不出来都当没有
        _log.warning("VLM 调用失败（%s）：%s", path.name, exc)
        return None


def judge(path: Path, timeout: float = 45.0) -> bool | None:
    """True = 插图、False = 装饰、None = 判不了（调用方应保留）。"""
    text = _ask_vlm(
        path, PROMPT, _setting("VISION_MODEL", DEFAULT_MODEL), 64, timeout)
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        verdict = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    value = verdict.get("figure")
    return value if isinstance(value, bool) else None


def _clean(sentence: str) -> str:
    """模型爱写「这张图展示了…」「如图所示，…」，这些词对检索没用，切掉。

    只剥**整词前缀**，不用 `lstrip(字符集)`——那样「示意图中…」会被剥成「意图中…」。
    """
    text = " ".join(sentence.strip().split())
    for prefix in ("这张图", "这幅图", "该图", "此图", "图中", "图片"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip("，,：: ")
            for lead in ("展示了", "展示", "描绘了", "描绘", "显示了", "显示",
                         "给出了", "给出", "是", "为"):
                if text.startswith(lead):
                    text = text[len(lead):].lstrip("，,：: ")
                    break
            break
    for suffix in "。；;":
        text = text.rstrip(suffix)
    # 方括号圆括号会打断 markdown 的 `![alt](path)`，整行就解析不回图了
    for ch in "[]()（）【】!":
        text = text.replace(ch, " ")
    return " ".join(text.split()).strip()


def describe(path: Path, timeout: float = 60.0) -> str | None:
    """一句话说明这张图在讲什么，给没有题注的图当图注用。失败返回 None（调用方退占位）。

    超时给到 60s：服务端忙的时候同一张图能跑 38s（实测 30s 会假失败），
    宁可慢一点也别把好好的图判成「写不出来」。
    """
    text = _ask_vlm(
        path, DESCRIBE_PROMPT, _setting("VISION_DESC_MODEL", DESC_MODEL), 128, timeout)
    if not text:
        return None
    first = next((line for line in text.strip().splitlines() if line.strip()), "")
    cleaned = _clean(first.strip().strip('"').strip("「」"))
    if len(cleaned) < 4:
        return None
    if len(cleaned) <= 60:
        return cleaned
    # 超长时落在句读上截断，别把「…出现三个」这种半句话切下来
    cut = max(cleaned.rfind(ch) for ch in "，、；,;")
    return cleaned[:cut] if cut >= 30 else cleaned[:60]
