# -*- coding: utf-8 -*-
"""探针：现有检索 + 通用对话，能不能直接承担「章节摘要」和「自测出题」。

背景：`docs/PROJECT-PLAN.md` 把 D 域（章节摘要 / 出题 / 批改）列在 P1，
但也许不需要专门做——现有 `search_textbook` + 通用对话已经能做。这个探针
拿真书、真模型试一遍，看结论再决定要不要写 D 域。

观察三件事：
  1. 模型**调了几次**检索、每次命中哪些章节 —— 决定「一次能不能覆盖一章」；
  2. 回答里的**引用条数** —— 决定「有没有依据」还是凭参数知识编；
  3. 回答形态 —— 摘要像不像提纲、题目有没有答案和解析。

只读：文档库和向量库读生产副本（向量库复制到临时目录），checkpoint 用临时
文件，不写 `data/`。记忆不注入（`memory_enabled=False`），排除干扰。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:/agent")
DATA = ROOT / "data"

sys.path.insert(0, str(ROOT))
import documents  # noqa: E402
import retrieval  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="probe-chapter-"))
shutil.copy2(DATA / "vectors.sqlite3", tmp / "vectors.sqlite3")
retrieval.VECTOR_DB = tmp / "vectors.sqlite3"

import agent_core  # noqa: E402

agent_core.CHECKPOINT_PATH = tmp / "checkpoints.sqlite"

USER = "u-671d242584"
BOOK = "第3章 电波传播与传播预测模型"

QUESTIONS = [
    ("章节摘要", "第3章 电波传播与传播预测模型 这一章主要讲什么？给我一个提纲，覆盖它的主要小节。"),
    ("自测出题", "就第3章 电波传播与传播预测模型 出 3 道单选题，每题给选项、答案和一句解析。"),
    ("对照：普通检索", "第3章 电波传播与传播预测模型 里的路径损耗模型有哪几种？分别适用什么场景？"),
]

_calls: list[dict] = []


def backend(user_id: str, query: str, doc_titles: list[str] | None, limit: int) -> dict:
    """包一层，把每次检索的参数与命中情况记下来。"""
    result = retrieval.search(user_id, query, limit=limit, doc_titles=doc_titles)
    hits = result.get("hits") or []
    _calls.append(
        {
            "query": query,
            "doc_titles": doc_titles,
            "limit": limit,
            "n": len(hits),
            "chapters": Counter(h.get("chapter") or "(无章节)" for h in hits),
            "pages": sorted({(h.get("page_start"), h.get("page_end")) for h in hits}),
            "confident": result.get("confident"),
            "reason": result.get("reason"),
        }
    )
    return result


agent_core.set_retrieval_backend(backend)


async def ask(label: str, question: str, thread: str) -> None:
    _calls.clear()
    tools: list[str] = []
    answer: list[str] = []
    citations: list[dict] = []

    async for event in agent_core.stream_agent(
        USER, thread, question, memory_enabled=False
    ):
        kind = event["type"]
        if kind == "tool_call":
            tools.append(event["tool"])
        elif kind == "assistant_delta":
            answer.append(event["content"])
        elif kind == "citations":
            citations = event.get("items") or []

    text = "".join(answer).strip()
    print("\n" + "=" * 78)
    print(f"[{label}] {question}")
    print("-" * 78)
    print(f"工具调用 {len(tools)} 次：{tools or '（没调）'}")

    for i, c in enumerate(_calls, 1):
        chapters = "、".join(f"{k}×{v}" for k, v in c["chapters"].most_common(4))
        print(f"  #{i} query={c['query'][:38]!r} 命中{c['n']}条 "
              f"confident={c['confident']} 章节={chapters}")
        if c["reason"]:
            print(f"      reason={c['reason']}")

    print(f"\n引用 {len(citations)} 条："
          f"{[(x['code'], (x.get('chapter') or '')[:18], x.get('page_start')) for x in citations]}")

    print(f"\n回答（{len(text)} 字）：")
    print(text[:1800] + ("\n……（截断）" if len(text) > 1800 else ""))


async def main() -> None:
    doc = documents.get_document("50f634de", USER)
    print(f"书目：{doc['title']}  status={doc['status']}  "
          f"chunks={doc['chunk_count']}  pages={doc['page_count']}")

    # 可以只跑某一题：python probe-chapter-tasks.py 章节摘要
    only = sys.argv[1] if len(sys.argv) > 1 else None
    picked = [q for q in QUESTIONS if only is None or only in q[0]]

    try:
        for index, (label, question) in enumerate(picked):
            await ask(label, question, f"probe-chapter-{index}")
    finally:
        await agent_core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
