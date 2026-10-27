"""切分质量回归：空锚点段、超短碎片、超长代码块（全离线）。

线上库里同时存在三种「能检索、但检索到等于没检索」的段落：
- 130 条内容只有 `<!-- page:N -->` 的 code chunk（围栏没闭合，把后半本吞进了代码块）；
- 213 条不足 5 字的正文段（「故」「则」「即」）；
- 13 条超过 3000 字，最坏 36106 字（一条代码块把整章都装进去了）。

跑法：python tests/probes/probe-chunking.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import documents  # noqa: E402

failures: list[str] = []
ANCHOR_ONLY = re.compile(r"^(?:\s*<!--\s*(?:page|chapter):[^>]*-->\s*)+$")


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def body(text: str, size: int = 120) -> str:
    return (text * (size // max(1, len(text)) + 1))[:size]


def main() -> int:
    print("== 1. 未闭合的代码围栏不能吞掉后面几十页 ==\n")
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
    check("第 2 页正文是 text 而不是 code", bool(page2) and page2[0]["kind"] == "text", str(kinds))
    check("页码出现在正确位置", bool(page2) and page2[0]["page_start"] == 2,
          str(page2[0]["page_start"]) if page2 else "没找到")
    check("没有「只有锚点」的段落", not any(ANCHOR_ONLY.match(c["content"]) for c in chunks))
    check("代码块还在（只收了自己那三行）",
          any(c["kind"] == "code" and "def solve" in c["content"] for c in chunks), str(kinds))

    print("\n== 2. 超长代码块按行切开 ==\n")
    listing = "\n".join(f"line_{i:04d} = compute({i})  # 这一行有一点注释让它长一些" for i in range(400))
    long_markdown = "\n".join(["<!-- page:1 -->", "```python", listing, "```"])
    chunks = documents.cut_chunks(long_markdown, "doc00002")
    code_chunks = [c for c in chunks if c["kind"] == "code"]
    check("被切成多块", len(code_chunks) > 1, f"{len(code_chunks)} 块")
    check("每块都不超过上限", all(len(c["content"]) <= documents.MAX_CODE_CHARS for c in code_chunks),
          f"最长 {max(len(c['content']) for c in code_chunks)}")
    rebuilt = "".join(c["content"] if c["content"].endswith("\n") else c["content"] + "\n"
                      for c in code_chunks)
    check("拼回去内容不丢", rebuilt.count("line_") == 400, f"{rebuilt.count('line_')} 行")
    check("切点都在行首（没切半行）",
          all(c["content"].lstrip().startswith("line_") for c in code_chunks),
          str([c["content"][:12] for c in code_chunks[:3]]))

    print("\n== 3. 图后面的碎句要挂到下一段 ==\n")
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
    check("没有独立成条的碎句", tiny == [], str([c["content"] for c in tiny]))
    merged = [c for c in chunks if "故" in c["content"]]
    check("碎句并进了正文", bool(merged) and "故" in merged[0]["content"]
          and "抽样定理要求" in merged[0]["content"], str(merged[0]["content"][:30]) if merged else "没了")

    print("\n== 3b. 被图夹住的碎片也要并走（流程图拆行）==\n")
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
    check("图之间的碎行没有独立成段", tiny == [], str([c["content"] for c in tiny]))
    check("碎行挂到了相邻正文",
          any("开始" in c["content"] and "计算流程" in c["content"] for c in chunks),
          str([c["content"][:20] for c in chunks if c["kind"] == "text"]))
    check("公式没被并进正文", any(c["kind"] == "formula" and "P(A)=1" in c["content"] for c in chunks))

    print("\n== 4. 正常文档的段数不受影响 ==\n")
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
    check("段落长度都在合理区间（不碎片、不超长）",
          bool(chunks) and all(documents.MIN_CHARS <= size <= documents.MAX_CHARS for size in lengths),
          str(lengths))
    check("原文没丢", sum(lengths) >= 10 * 100, f"合计 {sum(lengths)} 字")
    check("序号连续", [c["seq"] for c in chunks] == list(range(len(chunks))))

    print()
    if failures:
        print(f"{len(failures)} 项失败：" + "、".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
