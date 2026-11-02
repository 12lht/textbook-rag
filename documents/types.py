"""解析链路的中间数据形态。单独一个文件，是为了让切分和解析两边都能引用，
而不必互相 import——那会立刻绕成循环。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class DocumentError(Exception):
    """可预期的失败：格式不支持、文件损坏、依赖缺失、超限。

    和「代码 bug」分开，因为这些错**要原样告诉用户**（写进 `documents.error`
    并在列表里显示），而不是吞掉或 500。
    """


@dataclass
class _Element:
    """切分前的中间形态：一段正文、一张图、或一个公式编号。

    `content` 对图/公式来说是**题注文本**——它不是正文，但必须留着：
    「图 3-4 抽样过程」这句话本身就是检索命中的抓手。
    """

    kind: str  # text / figure / formula
    content: str
    page: int
    y: float = 0.0
    asset_id: str | None = None
    label: str | None = None
    chapter: str | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class _Parsed:
    page_count: int
    elements: list[_Element] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    toc: list[list] = field(default_factory=list)
    warning: str | None = None
    section_per_page: bool = False
    # 图片 sha1 → asset_id，只在单次解析内用来去重，不入库
    asset_hashes: dict[str, str] = field(default_factory=dict)
