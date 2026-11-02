"""切分质量回归：空锚点段、超短碎片、超长代码块（全离线）。

线上库里同时存在三种「能检索、但检索到等于没检索」的段落：
- 130 条内容只有 `<!-- page:N -->` 的 code chunk（围栏没闭合，把后半本吞进了代码块）；
- 213 条不足 5 字的正文段（「故」「则」「即」）；
- 13 条超过 3000 字，最坏 36106 字（一条代码块把整章都装进去了）。

原 `tests/probes/probe-chunking.py`，2026-11-03 迁到 pytest。
"""
from __future__ import annotations

import re

import documents

ANCHOR_ONLY = re.compile(r"^(?:\s*<!--\s*(?:page|chapter):[^>]*-->\s*)+$")


def body(text: str, size: int = 120) -> str:
    return (text * (size // max(1, len(text)) + 1))[:size]


def test_unterminated_fence_does_not_swallow_pages() -> None:
    """一个漏写的 ``` 曾把后面几十页全吞进代码块。"""
    markdown = "\n".join([
        "<!-- page:1 -->",
        "<!-- chapter:第 1 章 -->",
        "正文第一段。" + body("迭代与递归的差别在于是否显式维护调用栈。"),
        "```python",
        "def solve(n):",
        "    return n",
        "",
        "<!-- page:2 -->",
        "<!-- chapter:第 2 章 -->",
        "第二页的正文，讲的是分治算法的时间复杂度分析。" + body("归并排序的递归深度是 log n。"),
    ])
    chunks = documents.cut_chunks(markdown, "doc00001")
    kinds = [(c["kind"], c["content"][:24].replace("\n", " ")) for c in chunks]
    page2 = [c for c in chunks if "第二页的正文" in c["content"]]

    assert page2 and page2[0]["kind"] == "text", f"第 2 页正文变成了 {kinds}"
    assert page2[0]["page_start"] == 2, page2[0]["page_start"]
    assert not any(ANCHOR_ONLY.match(c["content"]) for c in chunks), "出现了只有锚点的空段"
    assert any(c["kind"] == "code" and "def solve" in c["content"] for c in chunks), kinds


def test_long_code_block_is_split_on_line_boundaries() -> None:
    """超长代码块按行切开，且不能丢内容、不能切半行。"""
    listing = "\n".join(f"line_{i:04d} = compute({i})  # 这一行有一点注释让它长一些" for i in range(400))
    long_markdown = "\n".join(["<!-- page:1 -->", "```python", listing, "```"])
    chunks = documents.cut_chunks(long_markdown, "doc00002")
    code_chunks = [c for c in chunks if c["kind"] == "code"]
    longest = max(len(c["content"]) for c in code_chunks)

    assert len(code_chunks) > 1, f"{len(code_chunks)} 块"
    assert longest <= documents.MAX_CODE_CHARS, f"最长 {longest} 超过上限 {documents.MAX_CODE_CHARS}"

    rebuilt = "".join(
        c["content"] if c["content"].endswith("\n") else c["content"] + "\n"
        for c in code_chunks
    )
    assert rebuilt.count("line_") == 400, f"拼回去只剩 {rebuilt.count('line_')} 行"
    assert all(c["content"].lstrip().startswith("line_") for c in code_chunks), \
        f"切点不在行首：{[c['content'][:12] for c in code_chunks[:3]]}"


def test_fragment_after_figure_is_merged_into_prose() -> None:
    """图后面那种「故」开头的单字碎句，不能独立成段。"""
    fragment_markdown = "\n".join([
        "<!-- page:1 -->",
        "<!-- chapter:第 1 章 -->",
        "![图 1-1 抽样过程](as_aaaabbbb.png)",
        "故",
        "抽样定理要求采样率高于信号最高频率的两倍，这样频谱才不会发生混叠。",
        "<!-- page:2 -->",
        "<!-- chapter:第 1 章 -->",
        "重建时用理想低通滤波器即可恢复原信号，实际用近似滤波器会带来误差。",
    ])
    chunks = documents.cut_chunks(fragment_markdown, "doc00003")
    tiny = [c for c in chunks if c["kind"] == "text" and len(c["content"]) < 5]
    merged = [c for c in chunks if "故" in c["content"]]

    assert tiny == [], f"碎句独立成段：{[c['content'] for c in tiny]}"
    assert merged and "抽样定理要求" in merged[0]["content"], \
        merged[0]["content"][:30] if merged else "碎句丢了"


def test_fragment_between_figures_is_merged() -> None:
    """被两张图夹住的碎片（流程图拆行）也要并走，但公式不能被并进正文。"""
    flowchart = "\n".join([
        "<!-- page:1 -->",
        "<!-- chapter:第 1 章 -->",
        "",
        "下面是密码子频率的计算流程。",
        "",
        "![图 1-1 流程](as_aaaabbbb.png)",
        "",
        "开始",
        "",
        "![图 1-2 中间](as_ccccdddd.png)",
        "",
        "$$P(A)=1$$",
        "",
        "从训练样本中统计每个密码子出现的频率，据此估计转移概率，这是参数估计的第一步。",
    ])
    chunks = documents.cut_chunks(flowchart, "doc00005")
    tiny = [c for c in chunks if c["kind"] == "text" and len(c["content"]) < 5]

    assert tiny == [], f"图之间的碎行独立成段：{[c['content'] for c in tiny]}"
    assert any("开始" in c["content"] and "计算流程" in c["content"] for c in chunks), \
        f"碎行没挂到相邻正文：{[c['content'][:20] for c in chunks if c['kind'] == 'text']}"
    assert any(c["kind"] == "formula" and "P(A)=1" in c["content"] for c in chunks), \
        "公式被并进正文了"


def test_normal_document_keeps_reasonable_chunks() -> None:
    """正常文档不该被上述修补逻辑影响：段长合理、原文不丢、序号连续。"""
    normal = "\n".join(
        line
        for i in range(10)
        for line in (
            f"<!-- page:{i + 1} -->",
            f"<!-- chapter:第 {i // 5 + 1} 章 -->",
            body(f"第 {i} 页的正文内容，讲的是某一种算法的复杂度。"),
        )
    )
    chunks = documents.cut_chunks(normal, "doc00004")
    lengths = [len(c["content"]) for c in chunks]

    assert chunks and all(documents.core.MIN_CHARS <= size <= documents.core.MAX_CHARS for size in lengths), \
        f"段长越界：{lengths}"
    assert sum(lengths) >= 10 * 100, f"原文丢了，合计只剩 {sum(lengths)} 字"
    assert [c["seq"] for c in chunks] == list(range(len(chunks))), "序号不连续"
