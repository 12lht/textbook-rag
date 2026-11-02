"""对 Markdown 正文的修复：裸 LaTeX 包裹、项目符号归一、符号字体还原。

全是纯函数（输入字符串、输出字符串），不碰数据库也不碰文件——
所以它们能被单独验证，也值得被单独验证：这几条规则改错会让整库正文变形。
"""
from __future__ import annotations

import re

_PLACEHOLDER_ALT_RE = re.compile(r"!\[第 (\d+) 页 插图\]\((as_[0-9a-f]{4,})\.png\)")

# ── 存量修复：把正文里的裸 LaTeX 包成 `$..$` ───────────────────────────────────
# 老解析把 MinerU 的 `equation_inline` 直接拼成文本（整库 5749 处），前端不渲染、检索是噪声。
# 代码已修（`_inline_text` 现在会包），这里只处理**已入库的 Markdown**：重新解析一轮太贵，
# 而这些片段在中文正文里几乎不会与其他东西混淆，所以用边界扫描找回公式的边界。
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_BLOCK_MATH_RE = re.compile(r"\$\$.*?\$\$", re.S)
_INLINE_MATH_RE = re.compile(r"\$[^$\n]+\$")
# 图片引用必须整段保护：`![图注](as_x.png)` 里既有 `]` 又有 `(`，
# 公式边界一跨过去就会包成 `$...](as_x.png)$`，前端渲染直接报红（11_隐马尔科夫-I 踩过）。
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]{2,}")
_CJK_PUNCT = set("，。；：、！？（）《》「」【】…—·～“”‘’")


def _is_stop(ch: str) -> bool:
    """公式片段到这里就结束：汉字、中文标点、换行、反引号，以及被保护起来的占位符。"""
    if ch in "\x00\n`":
        return True
    if ch in _CJK_PUNCT:
        return True
    return "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"


def wrap_inline_math(markdown: str) -> tuple[str, int]:
    """把正文里裸的 LaTeX 片段包成 `$..$`，返回 (新文本, 包裹了几处)。

    先保护掉代码块、`$$..$$`、已有的 `$..$`（换成占位符），再以每个 `\\命令` 为锚点向左右
    扩到边界，重叠区间合并后一次性重建。**表格行整行跳过**——`|` 既是列分隔又可能是
    `\\left| x \\right|`，分不清就别动。
    """
    stash: list[str] = []

    def protect(match: re.Match) -> str:
        stash.append(match.group(0))
        return f"\x00{len(stash) - 1}\x00"

    text = markdown
    for pattern in (_CODE_FENCE_RE, _IMAGE_REF_RE, _BLOCK_MATH_RE, _INLINE_MATH_RE):
        text = pattern.sub(protect, text)

    spans: list[tuple[int, int]] = []
    for match in _LATEX_CMD_RE.finditer(text):
        start, end = match.start(), match.end()
        if spans and start < spans[-1][1]:
            continue
        left = start
        while left > 0 and not _is_stop(text[left - 1]):
            left -= 1
        right = end
        while right < len(text) and not _is_stop(text[right]):
            right += 1
        segment = text[left:right]
        left += len(segment) - len(segment.lstrip())
        right -= len(segment) - len(segment.rstrip())
        if right <= left:
            continue
        # 表格行：这一行的第一个非空白字符是 `|` 就整行不动。
        # **不能拿 `text[line_start:left]` 判**：`|` 不是停止符，第一格以 `\left\| x`
        # 开头时 left 会一路退到行首，`lstrip().startswith("|")` 就变成「不以 | 开头」，
        # 整行被包成 `$| \left\| x \right\| |$`，表格结构直接毁掉。
        line_start = text.rfind("\n", 0, left) + 1
        line_end = text.find("\n", right)
        line = text[line_start: line_end if line_end >= 0 else len(text)]
        first_char = next((ch for ch in line if not ch.isspace()), "")
        if first_char == "|":
            continue
        if spans and left <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], right))
        else:
            spans.append((left, right))

    if not spans:
        return markdown, 0

    parts: list[str] = []
    cursor = 0
    for left, right in spans:
        parts.append(text[cursor:left])
        parts.append(f"${text[left:right]}$")
        cursor = right
    parts.append(text[cursor:])
    wrapped = "".join(parts)
    for index, original in enumerate(stash):
        wrapped = wrapped.replace(f"\x00{index}\x00", original)
    return wrapped, len(spans)
_PAGE_ANCHOR_RE = re.compile(r"^<!--\s*page:(\d+)\s*-->\s*$")
_CHAPTER_ANCHOR_RE = re.compile(r"^<!--\s*chapter:(.+?)\s*-->\s*$")
_IMAGE_LINE_RE = re.compile(r"^!\[(.*?)\]\(([^)]+)\)\s*$")

ASSET_REF_RE = re.compile(r"(!\[.*?\]\()(as_[0-9a-f]{4,})\.png(\))")

_BULLET_GLYPHS = "\uf000-\uf8ff➢➤►▶▸◆◇▪▫●○•·❖"

_BULLET_LINE_RE = re.compile("^[ \t\u200b]*[" + _BULLET_GLYPHS + "][ \t\u3000]*")


def normalize_bullets(text: str) -> str:
    """把行首条目符换成 Markdown 的 `- `。
    私用区那几个是方块、箭头占字符还干扰分词；统一之后正文列表和幻灯片要点在存储里是同一种
    形态，下游（检索 / 引用 / 导出）只认一种。
    """
    if not any(ch in text for ch in "➢➤►▶▸◆◇▪▫●○•·❖") and not any(
        "\uf000" <= ch <= "\uf8ff" for ch in text
    ):
        return text  # 绝大多数段落走这条快路，不做逐行正则
    return "\n".join(
        "- " + line[m.end() :] if (m := _BULLET_LINE_RE.match(line)) else line
        for line in text.splitlines()
    )


"""Symbol 字体（SymbolMT 等）排的数学符号没有 ToUnicode，两个引擎都原样吐出 U+F0xx，
编辑器里显示成空白——不映射就等于符号全丢。下面是 Adobe Symbol 的内建编码（0x20-0xFE）。"""
_SYMBOL_GREEK_UP = "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"
_SYMBOL_GREEK_LO = "αβχδεφγηιϕκλμνοπθρστυϖωξψζ"
_SYMBOL_MAP = {
    0x20: " ", 0x21: "!", 0x22: "∀", 0x23: "#", 0x24: "∃", 0x25: "%", 0x26: "&",
    0x27: "∋", 0x28: "(", 0x29: ")", 0x2A: "∗", 0x2B: "+", 0x2C: ",", 0x2D: "−",
    0x2E: ".", 0x2F: "/", 0x3A: ":", 0x3B: ";", 0x3C: "<", 0x3D: "=", 0x3E: ">",
    0x3F: "?", 0x40: "≅", 0x5B: "[", 0x5C: "∴", 0x5D: "]", 0x5E: "⊥", 0x5F: "_",
    0x60: "‾", 0x7B: "{", 0x7C: "|", 0x7D: "}", 0x7E: "∼",
    0xA0: "€", 0xA1: "ϒ", 0xA2: "′", 0xA3: "≤", 0xA4: "⁄", 0xA5: "∞", 0xA6: "ƒ",
    0xA7: "♣", 0xA8: "♦", 0xA9: "♥", 0xAA: "♠", 0xAB: "↔", 0xAC: "←", 0xAD: "↑",
    0xAE: "→", 0xAF: "↓", 0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×",
    0xB5: "∝", 0xB6: "∂", 0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈",
    0xBC: "…", 0xBD: "⏐", 0xBE: "⎯", 0xBF: "↵", 0xC0: "ℵ", 0xC1: "ℑ", 0xC2: "ℜ",
    0xC3: "℘", 0xC4: "⊗", 0xC5: "⊕", 0xC6: "∅", 0xC7: "∩", 0xC8: "∪", 0xC9: "⊃",
    0xCA: "⊇", 0xCB: "⊄", 0xCC: "⊂", 0xCD: "⊆", 0xCE: "∈", 0xCF: "∉", 0xD0: "∠",
    0xD1: "∇", 0xD2: "®", 0xD3: "©", 0xD4: "™", 0xD5: "∏", 0xD6: "√", 0xD7: "·",
    0xD8: "¬", 0xD9: "∧", 0xDA: "∨", 0xDB: "⇔", 0xDC: "⇐", 0xDD: "⇑", 0xDE: "⇒",
    0xDF: "⇓", 0xE0: "◊", 0xE1: "⟨", 0xE5: "∑", 0xF1: "⟩", 0xF2: "∫", 0xF3: "⌠",
    0xF4: "⎮", 0xF5: "⌡",
}
# 矩阵/分式的括号拼块（0xE6-0xEF、0xF6-0xFE）本身没有语义，留着只是噪音
_SYMBOL_BOX_CODES = (0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEE,
                     0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE)


def _build_symbol_table() -> dict[int, str]:
    table = {0xF000 + code: ch for code, ch in _SYMBOL_MAP.items()}
    table.update({0xF030 + i: chr(0x30 + i) for i in range(10)})
    table.update({0xF041 + i: ch for i, ch in enumerate(_SYMBOL_GREEK_UP)})
    table.update({0xF061 + i: ch for i, ch in enumerate(_SYMBOL_GREEK_LO)})
    table.update({0xF000 + code: " " for code in _SYMBOL_BOX_CODES})
    return table


_SYMBOL_TABLE = _build_symbol_table()


def fix_symbol_font(text: str) -> str:
    if not text:
        return text
    return text.translate(_SYMBOL_TABLE)
