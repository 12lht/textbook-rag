"""把 PDF / PPTX / DOCX 变成 `_Parsed`（元素序列 + 图 + 目录）。

只管「读文件、认版面、抽图注」，不碰数据库、不切 chunk——
写库和切分留在 `documents.py`，那边才是编排者。
所以这里没有 `_connect`、没有 `_store`，加功能时也别加。
"""
from __future__ import annotations

import difflib
import hashlib
import html.parser
import io
import logging
import os
import re
import secrets
import tempfile
import time
import unicodedata
from pathlib import Path

from .types import DocumentError, _Element, _Parsed

_log = logging.getLogger("agent.documents")

_CAPTION_RE = re.compile(
    r"^\s*(图|表|Figure|Fig\.?|Table)\s*[（(]?\s*"
    r"([0-9]+(?:\s*[-－.．–—‑]\s*[0-9]+)*|[A-Z]\s*[-－.．–—‑]\s*[0-9]+)"
    r"\s*[)）]?\s*[:：.．、]?\s*(.*)$",
    re.IGNORECASE,
)
_CAPTION_VERBS = (
    "所示", "中", "为", "是", "表示", "展示", "给出", "描述", "说明", "显示",
    "列出", "对比", "描绘", "列举", "呈现", "可见", "反映了", "是一种",
)
_HEADING_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百]+\s*[章节篇部]"
    r"|[0-9]+(\.[0-9]+){1,2}\s*[、.．]?\s*\S"
    r"|Chapter\s+[0-9]+)",
    re.IGNORECASE,
)

def _require(module: str, hint: str):
    """延迟 import 第三方解析库。缺了给出**能照做的**提示，而不是 ModuleNotFoundError。"""
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise DocumentError(f"缺少解析依赖 {module}，请先安装：{hint}") from exc


def _parse_pdf(data: bytes, asset_dir: Path) -> _Parsed:
    """PDF 解析入口：只有 MinerU 一条路。

    **不设本地兜底**——没有 OCR、没有公式结构的备用管线，兜出来的结果只会让人以为解析成功了。
    本地两档的实现在 `D://pdf解析` 备份着，刻意不 import，免得又把分支带回来。
    """
    return _parse_pdf_mineru(data, asset_dir)

def _clean_heading(text: str) -> str:
    """章节名要纯文本：MinerU 的标题里会夹 `<sup>` 标记和 `$`，直接进引用摘要会显示成
    `指数阶 O(2<sup>n</sup>)`。这里转成 `^` / `_` 的写法，正文元素本身不动（前端能渲染）。"""
    text = re.sub(r"<sup>(.*?)</sup>", r"^\1", text or "")
    text = re.sub(r"<sub>(.*?)</sub>", r"_\1", text)
    text = re.sub(r"<[^>]{1,20}>", "", text).replace("$", "")
    return re.sub(r"\s{2,}", " ", text).strip()


_INLINE_MATH_TYPES = {"equation_inline", "inline_equation", "equation", "math"}


def _inline_text(items: list | None) -> str:
    r"""v2 的行内元素是 `[{type, content}]`，拼成一行文本。

    **`equation_inline` 是裸 LaTeX 源码**，不包 `$` 的话前端不渲染、检索里全是反斜杠噪声
    （实测整库 5749 处，`\boldsymbol`、`\sqrt` 满屏）。包成 `$..$` 交给 marked + KaTeX。
    """
    parts: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content", ""))
        if (
            item.get("type") in _INLINE_MATH_TYPES
            and text.strip()
            and not text.strip().startswith("$")
        ):
            text = f"${text.strip()}$"
        parts.append(text)
    return "".join(parts).strip()


class _HtmlTable(html.parser.HTMLParser):
    """MinerU 的表格以 HTML 给出，转成 markdown pipe 表格才能进 chunk。

    必须展开 `rowspan`：MinerU 把「整数」在 4 行上写成 `rowspan="4"`，不展开就会让后面
    每行少一格、整表左移一列（`short` 的「2 字节」跑到「符号」列）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: dict[int, str] | None = None
        self._cell: list[str] | None = None
        self._rowspan = 1
        self._carry: dict[int, list] = {}
        self._cursor = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("sup", "sub"):
            # 表格里的指数写在 <sup> 里，只收文本会把 2^63 变成 263（等于改错数据）。
            # 标签原样留给 marked 渲染成真上标，和正文里 MinerU 给的写法保持一致。
            if self._cell is not None:
                self._cell.append(f"<{tag}>")
        elif tag == "tr":
            self._row = {}
            for col in list(self._carry):
                value, left = self._carry[col]
                self._row[col] = value
                if left <= 1:
                    del self._carry[col]
                else:
                    self._carry[col][1] = left - 1
            self._cursor = 0
        elif tag in ("td", "th"):
            self._cell = []
            self._rowspan = 1
            for key, value in attrs:
                if key == "rowspan" and value and value.isdigit():
                    self._rowspan = max(1, int(value))

    def handle_endtag(self, tag):
        if tag in ("sup", "sub") and self._cell is not None:
            self._cell.append(f"</{tag}>")
        elif tag == "tr" and self._row is not None:
            width = (max(self._row) + 1) if self._row else 0
            self.rows.append([self._row.get(i, "") for i in range(width)])
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                while self._cursor in self._row:
                    self._cursor += 1
                value = "".join(self._cell).strip()
                self._row[self._cursor] = value
                if self._rowspan > 1:
                    self._carry[self._cursor] = [value, self._rowspan - 1]
                self._cursor += 1
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _html_table_to_markdown(body: str) -> str:
    if not body or "<t" not in body.lower():
        return ""
    parser = _HtmlTable()
    parser.feed(body)
    rows = [row for row in parser.rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").strip()

    lines = ["| " + " | ".join(cell(v) for v in rows[0]) + " |", "|" + "---|" * width]
    for row in rows[1:]:
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def _caption_label(caption: str) -> str:
    """题注 → 图号。不能按空格切：`图0-1 本书主要内容` 切成 `图0-1`，
    但 `图 2-7 算法…` 会只剩一个 `图`——同一套 `_match_caption` 才稳。"""
    if not caption:
        return ""
    hit = _match_caption(caption)
    return hit[0] if hit else "插图"


# 装饰图的几何判据。阈值是拿课件样本量的：装饰 193–296px / 占页面 1.2%–2.9%，
# 真插图最小 606px / 6.5%，中间那段（320–450px、4%–8%）就是「判不了」的灰区。
_DECOR_SHORT_SIDE = 320
_DECOR_AREA_RATIO = 0.04
_DECOR_ASPECT = 8.0
# 灰区的另一头：够大就别问了，直接当插图（一次 VLM 调用 ≈2.3s，全书问一遍比解析还贵）
_ILLUS_SHORT_SIDE = 450
_ILLUS_AREA_RATIO = 0.08
_VLM_ASK_BUDGET = 30  # 单本最多问几次「是不是装饰」；超预算一律保留
_VLM_DESC_BUDGET = 500  # 单本最多生成几条图注；超了退「第 N 页 插图」
_VLM_INTERVAL = 0.3  # 每次调用前的间隔（秒），连着打会被服务端限流
_VLM_FAIL_LIMIT = 5  # 连续失败几次就整本停掉（熔断）；3 太急，一次限流就全军覆没


def _mineru_image_path(media_dir: Path, rel_path: str | None) -> Path | None:
    """MinerU 块里的图片引用 → 真实文件。

    `media_dir` 是**整个临时目录**（每批一个 `batch_N/images/` 子目录），块里的路径形如
    `batch_2/images/xxx.jpg`，所以要先按相对路径找，找不到再退回按文件名找（兼容老格式）。
    不能只取 `Path(rel).name`：MinerU 每批从 0 重新编号，不同批的同名图会被后一批盖掉。
    """
    if not rel_path:
        return None
    parts = [
        part for part in str(rel_path).replace("\\", "/").split("/")
        if part not in ("", ".", "..")
    ]
    if not parts:
        return None
    for candidate in (media_dir.joinpath(*parts), media_dir / parts[-1]):
        if candidate.is_file():
            return candidate
    return None


def _image_size(media_dir: Path, rel_path: str | None) -> tuple[int, int] | None:
    src = _mineru_image_path(media_dir, rel_path)
    if src is None:
        return None
    try:
        from PIL import Image

        with Image.open(src) as img:
            return img.size
    except Exception:  # noqa: BLE001 - 读不出尺寸就当不知道，别因此丢图
        return None


def _bbox_area(bbox) -> float:
    """bbox 是 0-1000 归一化坐标，面积占比直接就能算，不需要真实页面尺寸。"""
    if not bbox or len(bbox) < 4:
        return 1.0
    x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    return max(0.0, (x1 - x0) / 1000) * max(0.0, (y1 - y0) / 1000)


def _figure_verdict(width: int, height: int, area: float, caption: str) -> str:
    """三档：`keep` 明确是插图、`drop` 明确是装饰、`ask` 判不了（交给 VLM）。

    灰区才值得花一次模型调用——大多数图靠几何量就够了。有题注的永远 keep：
    作者写了图号就是插图，这一条不需要问模型。
    """
    if caption:
        return "keep"
    short, long = min(width, height), max(width, height)
    if long / max(1, short) >= _DECOR_ASPECT:
        return "drop"  # 分隔条、页边竖线
    # **两个条件必须同时成立**：单看尺寸会误杀窄高的真流程图，单看面积会误杀整页大图里的居中小图
    if short < _DECOR_SHORT_SIDE and area < _DECOR_AREA_RATIO:
        return "drop"
    if short >= _ILLUS_SHORT_SIDE or area >= _ILLUS_AREA_RATIO:
        return "keep"
    return "ask"


def _looks_decorative(width: int, height: int, area: float, caption: str) -> bool:
    """几何上能确定的装饰图。灰区算 False——它问的是「确定是装饰吗」，不是「像不像」。"""
    return _figure_verdict(width, height, area, caption) == "drop"


def _save_mineru_image(
    media_dir: Path, asset_dir: Path, rel_path: str | None, parsed: _Parsed,
    page: int, caption: str,
) -> str | None:
    """MinerU 的图是 jpg，但 `render_markdown` 固定按 `.png` 拼引用，统一转存 PNG。

    **同一本书里字节相同的图只存一份**：课件/教材里同一张图常出现多次（页眉示意图、
    重复的流程框图），实测 Hello-Algo 一本书就有 148 张重复、49.5MB。重复的判定用 PNG
    字节的 sha1，所以 `_Parsed.asset_hashes` 只在单次解析内有效——重解析会重新算。
    """
    src = _mineru_image_path(media_dir, rel_path)
    if src is None:
        return None
    try:
        from PIL import Image

        buffer = io.BytesIO()
        with Image.open(src) as img:
            rgb = img.convert("RGB")
            rgb.save(buffer, format="PNG")
            width, height = rgb.size
        blob = buffer.getvalue()
    except Exception:  # noqa: BLE001 - 坏图不该让整本失败
        return None

    digest = hashlib.sha1(blob).hexdigest()
    existing = parsed.asset_hashes.get(digest)
    if existing:
        return existing

    asset_id = f"as_{secrets.token_hex(4)}"
    dst = asset_dir / f"{asset_id}.png"
    dst.write_bytes(blob)
    parsed.asset_hashes[digest] = asset_id
    label = _caption_label(caption)
    if label == "插图":
        # `assets.label` 只放**图号**。「插图」是没编号时的兜底文案，写进库就是 795 行
        # 字面量重复（既不能按图号导航，也看不出区别）；没有编号就留空。
        label = ""
    parsed.assets.append({
        "asset_id": asset_id,
        "kind": "figure",
        "label": label or None,
        "caption": caption or None,
        "page": page,
        "file_path": str(dst),
        "width": width,
        "height": height,
    })
    return asset_id


_MINERU_NOISE = {"page_header", "page_number", "page_footer", "header", "footer", "aside_text"}


def _vision_budget():
    """两个模型的闸门：`ask` 判灰区图是不是装饰，`describe` 给无题注图写一句图注。

    两件事都会失败（网络、额度、回答跑偏、服务端限流），处理方式不同：
    - `ask` 拿不到答案 → 保留图（删错了补不回来）
    - `describe` 拿不到答案 → 退回「第 N 页 插图」（至少 chunk 不是空的）

    三个兜底，缺一个都会让解析变慢或变脏：
    - **次数上限**：一本 200 页的课件可能上百张图，不限的话解析会被模型拖到十几分钟；
    - **轻微节流**：连着打会被服务端限流，实测响应从 4s 掉到 40s；
    - **连续失败熔断**：连着 3 张拿不到结果就整本停掉，剩下的一律走占位——
      否则限流时 40 张各等 30s，光等就是 20 分钟。
    """
    import figures

    if not figures.available():
        return None, None
    asks, descriptions, failures = [_VLM_ASK_BUDGET], [_VLM_DESC_BUDGET], [0]

    def _call(func, path: Path):
        if failures[0] >= _VLM_FAIL_LIMIT:
            return None
        time.sleep(_VLM_INTERVAL)
        result = func(path)
        failures[0] = 0 if result is not None else failures[0] + 1
        return result

    def ask(path: Path) -> bool:
        if asks[0] <= 0:
            return False
        asks[0] -= 1
        return _call(figures.judge, path) is False

    def describe(path: Path) -> str | None:
        if descriptions[0] <= 0:
            return None
        descriptions[0] -= 1
        return _call(figures.describe, path)

    return ask, describe


def _parse_pdf_mineru(data: bytes, asset_dir: Path) -> _Parsed:
    """走 MinerU 云端，再把 v2 blocks 折成本项目的元素流。"""
    import pymupdf

    import mineru as mineru_client

    if not mineru_client.available():
        raise DocumentError("没配 MINERU_API_KEY")
    # 每次全新临时目录：清理想做但做不成时（宿主批量删除保护）也不会读到上一轮的旧 json
    workdir = Path(tempfile.mkdtemp(prefix="mineru_parse_"))
    try:
        payload = mineru_client.parse(
            data, "document.pdf", workdir, model=os.getenv("MINERU_MODEL", "vlm")
        )
        pdf = pymupdf.open(stream=data, filetype="pdf")
        try:
            def repair(page_index: int, bbox, text: str) -> str:
                if not 0 <= page_index < pdf.page_count:
                    return text
                return _astral_repair(text, pdf[page_index], bbox)

            ask, describe = _vision_budget()
            return _elements_from_mineru(
                payload, asset_dir, repair=repair, ask=ask, describe=describe)

        finally:
            pdf.close()
    finally:
        # 解压出来的 json + 原图有几十 MB，图已转存到 asset_dir，原件不必留
        mineru_client.purge_dir(workdir)


_UFFFD = "\ufffd"


def _astral_repair(text: str, page, bbox) -> str:
    """MinerU 把**星平面字符**（U+1D400 起的数学斜体字母等，UTF-16 要两个码元）一律吐成 U+FFFD，
    `𝑂(2𝑛)` 变成 `�(2�)`。PDF 文字层里这些字符还在，按块 bbox 取回、NFKC 归一化后回填。

    先按 difflib 对齐填；对齐覆盖不到的再用剩余字符按顺序兜。填不上就原样留着，
    宁可留个 U+FFFD 也不猜错——猜错等于把符号改成了另一个变量。
    """
    if not text or _UFFFD not in text or page is None or not bbox:
        return text
    import pymupdf

    try:
        width, height = page.rect.width, page.rect.height
        x0, y0, x1, y1 = (float(value) for value in bbox[:4])
        clip = pymupdf.Rect(
            x0 / 1000 * width - 2, y0 / 1000 * height - 2,
            x1 / 1000 * width + 2, y1 / 1000 * height + 2,
        )
        reference = page.get_text("text", clip=clip)
    except Exception:  # noqa: BLE001 - 取不到文字层就保持原样
        return text
    pool = [
        unicodedata.normalize("NFKC", char)
        for char in reference
        if ord(char) > 0xFFFF and unicodedata.normalize("NFKC", char)
    ]
    if not pool:
        return text

    out: list[str] = []
    used = 0
    matcher = difflib.SequenceMatcher(None, text, reference, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        segment = text[i1:i2]
        if tag == "equal" or _UFFFD not in segment:
            out.append(segment)
            continue
        candidates = [
            c for c in reference[j1:j2]
            if ord(c) > 0xFFFF and unicodedata.normalize("NFKC", c)
        ]
        candidates = [unicodedata.normalize("NFKC", c) for c in candidates]
        cursor = 0
        for char in segment:
            if char == _UFFFD and cursor < len(candidates):
                out.append(candidates[cursor])
                cursor += 1
                used += 1
            else:
                out.append(char)
    repaired = "".join(out)

    spare = pool[used:]
    if _UFFFD in repaired and spare:
        filled, cursor = [], 0
        for char in repaired:
            if char == _UFFFD and cursor < len(spare):
                filled.append(spare[cursor])
                cursor += 1
            else:
                filled.append(char)
        repaired = "".join(filled)
    return repaired


def _elements_from_mineru(
    payload: dict, asset_dir: Path, repair=None, ask=None, describe=None,
) -> _Parsed:
    parsed = _Parsed(page_count=int(payload["page_count"]))
    media_dir: Path = payload["images_dir"]  # 临时目录根，每批一个 batch_N/images/
    chapter: str | None = None
    for block in payload["blocks"]:
        kind = block.get("type")
        if not kind or kind in _MINERU_NOISE:
            continue
        page = int(block.get("page_idx", 0)) + 1
        content = block.get("content") or {}
        raw_bbox = block.get("bbox")
        # 必须是定长 4 元组：`_astral_repair` 里按 `x0, y0, x1, y1 = bbox[:4]` 解包，
        # 传进来 3 个元素会在那里 ValueError。不足 4 个就当没有坐标，走纯文本降级。
        bbox = (
            (float(raw_bbox[0]), float(raw_bbox[1]), float(raw_bbox[2]), float(raw_bbox[3]))
            if raw_bbox and len(raw_bbox) >= 4
            else None
        )

        # page/bbox 写成默认参数是**有意的**：默认值在 def 执行时求值，
        # 等于把这一轮的页码和坐标当场绑死。写成裸闭包会延迟绑定，
        # 现在因为在循环里立即调用所以碰巧没事——可一旦谁把 fix 存起来延后调用，
        # 就会全部拿到最后一轮的值，而那种错不会报错，只会静默标错页码。
        def fix(value: str, page: int = page, bbox=bbox) -> str:
            return repair(page - 1, bbox, value) if repair else value

        if kind == "title":
            text = fix(_inline_text(content.get("title_content")))
            if not text:
                continue
            # 云端会把「图 2-2 是该嵌套循环的流程框图。」这类正文句也标成标题
            if text.endswith(("。", "；", "：")) and len(text) > 20:
                parsed.elements.append(_Element(
                    kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
                continue
            chapter = _clean_heading(text) or text
            parsed.elements.append(_Element(
                kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
        elif kind == "paragraph":
            text = fix(_inline_text(content.get("paragraph_content")))
            if text:
                parsed.elements.append(_Element(
                    kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
        elif kind == "list":
            items = [
                fix(_inline_text(item.get("item_content")))
                for item in content.get("list_items") or []
            ]
            text = "\n".join(f"- {item}" for item in items if item)
            if text:
                parsed.elements.append(_Element(
                    kind="text", content=text, page=page, chapter=chapter, bbox=bbox))
        elif kind in {"code", "algorithm"}:
            body = fix(_inline_text(
                content.get("code_content") or content.get("algorithm_content")
            ))
            if not body:
                continue
            label = content.get("code_language") or ""
            parsed.elements.append(_Element(
                kind="code", content=body, page=page, label=str(label),
                chapter=chapter, bbox=bbox))
        elif kind == "table":
            markdown = _html_table_to_markdown(str(content.get("html") or ""))
            if not markdown:
                # 复杂表只给了截图、没给 HTML：退化成图，别整块消失
                if content.get("image_source"):
                    kind = "image"
                else:
                    _log.debug("表格既没有 html 也没有截图，跳过（第 %d 页）", page)
                    continue
            else:
                # 表格里的 U+FFFD 也要回填：MinerU 会把 `𝑂(2𝑛)` 这类星平面字符吐成替换符，
                # 表格行同样中招（Hello-Algo 就有）
                caption = fix(_inline_text(content.get("table_caption")))
                text = f"{caption}\n\n{fix(markdown)}" if caption else fix(markdown)
                parsed.elements.append(_Element(
                    kind="table", content=text, page=page, label=caption or None,
                    chapter=chapter, bbox=bbox))
                continue

        if kind == "image" or kind == "chart":
            # chart 是「算法画的图」，和 image 同构（都有 image_source + 题注），
            # 早先没接这一类，等于整张图连同题注一起消失。
            source = content.get("image_source") or {}
            rel = source.get("path")
            caption = fix(_inline_text(
                content.get("image_caption") or content.get("chart_caption")))
            path = _mineru_image_path(media_dir, rel)
            if not caption and path is not None:
                size = _image_size(media_dir, rel)
                if size:
                    verdict = _figure_verdict(size[0], size[1], _bbox_area(bbox), caption)
                    if verdict == "drop":
                        continue  # 明确的装饰：不存图、不产 chunk、不占 assets
                    if verdict == "ask" and ask and ask(path):
                        continue  # 灰区被判成装饰
                # 无题注的真插图：先让模型写一句「这图讲什么」，写不出来再退占位。
                # 占位能救「chunk 正文为空」，但没有语义，模型看不出该不该展示这张图。
                if describe:
                    caption = describe(path) or ""
                if not caption:
                    caption = f"第 {page} 页 插图"
            asset_id = _save_mineru_image(
                media_dir, asset_dir, rel, parsed, page, caption)
            parsed.elements.append(_Element(
                kind="figure", content=caption, page=page, asset_id=asset_id,
                label=_caption_label(caption) or None,
                chapter=chapter, bbox=bbox))
        elif kind in {"equation_interline", "equation", "inline_equation"}:
            # 行间公式是独立块（`math_content` 是 LaTeX 源码），之前没有分支，
            # 整条公式直接丢了。包成 `$$..$$` 才能被前端和切分当成公式。
            latex = str(content.get("math_content") or content.get("equation_content") or "").strip()
            if not latex:
                continue
            latex = fix(latex)
            if not latex.startswith("$"):
                latex = f"$${latex}$$"
            parsed.elements.append(_Element(
                kind="formula", content=latex, page=page, chapter=chapter, bbox=bbox))
        else:
            # 静默丢弃会让「少了一块」永远查不出来，至少留个线索
            _log.debug("MinerU 未处理的块类型：%s", kind)
    return parsed


_SENTENCE_END = "。！？；：.!?;:"


def _match_caption(text: str) -> tuple[str, str] | None:
    """一行是不是「图 3-4 xxx」这种独占题注，返回 (label, caption)。三道过滤：

    整行太长（是正文句子）；题注以句末标点收尾；题注以「展示了 / 反映了」这类动词
    开头（正文在引用图）。缺了第 3 条会去截隔壁那张图，编号还会重号。
    """
    stripped = text.strip()
    if len(stripped) > 120:
        return None
    match = _CAPTION_RE.match(stripped)
    if match is None:
        return None
    kind, number, caption = match.group(1), match.group(2), match.group(3).strip()
    if caption.endswith(("。", "！", "？", ".")) and len(caption) > 12:
        return None
    if len(caption) <= 1:
        return None
    if caption.startswith(_CAPTION_VERBS):
        return None
    if len(caption) > 45:
        return None
    prefix = "图" if kind.lower().startswith(("图", "fig")) else "表"
    return f"{prefix} {number.replace(' ', '')}", caption


def _parse_pptx(data: bytes, asset_dir: Path) -> _Parsed:
    """一套课件 → 元素流。**一张幻灯片一条 chunk**（或按长度切几条）。

    PPT 是「一页一个主题」，跨页合并会把互不相干的短句拼在一起，和 PDF 的「段落跨页要合并」
    正好相反。图片按 `shape.image.blob` 取原始字节存盘；公式是 OMML，v1 只做页级引用。
    """
    pptx = _require("pptx", "pip install python-pptx")
    from io import BytesIO

    presentation = pptx.Presentation(BytesIO(data))
    parsed = _Parsed(page_count=len(presentation.slides), section_per_page=True)

    for slide_index, slide in enumerate(presentation.slides, start=1):
        title = ""
        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                value = shape.text_frame.text.strip()
                if not value:
                    continue
                if shape == getattr(slide.shapes, "title", None):
                    title = value.splitlines()[0][:80]
                texts.append(value)
                continue
            if getattr(shape, "shape_type", None) == 13 and hasattr(shape, "image"):
                blob = shape.image.blob
                extension = shape.image.ext or "png"
                asset_id = f"as_{secrets.token_hex(4)}"
                path = asset_dir / f"{asset_id}.{extension}"
                path.write_bytes(blob)
                label = f"幻灯片 {slide_index} 插图"
                parsed.assets.append(
                    {
                        "asset_id": asset_id,
                        "kind": "figure",
                        "label": label,
                        "caption": None,
                        "page": slide_index,
                        "file_path": str(path),
                        "width": int(shape.width / 9525) if shape.width else 0,
                        "height": int(shape.height / 9525) if shape.height else 0,
                    }
                )
                texts.append(f"[{label}]")

        body = "\n".join(texts).strip()
        if not body:
            continue
        parsed.elements.append(
            _Element(
                kind="text",
                content=body,
                page=slide_index,
                y=0,
                chapter=title or None,
            )
        )

    return parsed


def _parse_docx(data: bytes, asset_dir: Path) -> _Parsed:
    """一份 Word 教材 → 元素流。

    页数「不知道」：Word 的页数取决于渲染时的字体和纸张，python-docx 不提供。`page_count` 记 0、
    前端显示「—」，不编一个数字；引用只能给到章节。表格按行转成 `a | b | c` 纯文本进正文
    （不转就整块丢失）。
    """
    docx = _require("docx", "pip install python-docx")
    from io import BytesIO

    document = docx.Document(BytesIO(data))
    parsed = _Parsed(page_count=0)
    chapter: str | None = None
    page_no = 0

    for block in _iter_docx_blocks(document):
        kind, payload = block
        if kind == "paragraph":
            text = (payload.text or "").strip()
            if not text:
                continue
            style = ""
            try:
                style = payload.style.name or ""
            except Exception:  # noqa: BLE001
                style = ""
            if style.lower().startswith("heading") or _HEADING_RE.match(text):
                chapter = text[:80]
                page_no += 1
            caption = _match_caption(text)
            if caption is not None and caption[0].startswith("图"):
                label, caption_text = caption
                parsed.elements.append(
                    _Element(
                        kind="text",
                        content=f"{label} {caption_text}".strip(),
                        page=page_no,
                        y=0,
                        chapter=chapter,
                    )
                )
                continue
            parsed.elements.append(
                _Element(kind="text", content=text, page=page_no, y=0, chapter=chapter)
            )
        elif kind == "table":
            rows = []
            for row in payload.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parsed.elements.append(
                    _Element(
                        kind="text",
                        content="\n".join(rows),
                        page=page_no,
                        y=0,
                        chapter=chapter,
                    )
                )
        elif kind == "image":
            asset_id = f"as_{secrets.token_hex(4)}"
            extension, blob, width, height = payload
            path = asset_dir / f"{asset_id}.{extension}"
            path.write_bytes(blob)
            label = f"图 {len(parsed.assets) + 1}"
            parsed.assets.append(
                {
                    "asset_id": asset_id,
                    "kind": "figure",
                    "label": label,
                    "caption": None,
                    "page": page_no,
                    "file_path": str(path),
                    "width": width,
                    "height": height,
                }
            )
            parsed.elements.append(
                _Element(
                    kind="figure",
                    content=label,
                    page=page_no,
                    y=0,
                    asset_id=asset_id,
                    label=label,
                    chapter=chapter,
                )
            )
    return parsed


def _iter_docx_blocks(document):
    """按文档顺序遍历段落和表格。`document.paragraphs` 和 `document.tables` 是两个独立列表，
    分开遍历会把表格全排到正文后面，所以走底层 XML 按 body 子元素顺序还原。
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield "paragraph", Paragraph(child, document)
        elif tag == "tbl":
            yield "table", Table(child, document)
        elif tag == "sectPr":
            continue

    for rel in document.part.rels.values():
        if "image" not in rel.reltype:
            continue
        try:
            blob = rel.target_part.blob
            extension = rel.target_part.partname.ext.lstrip(".") or "png"
        except Exception:  # noqa: BLE001
            continue
        yield "image", (extension.lower(), blob, 0, 0)
