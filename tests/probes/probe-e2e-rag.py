# -*- coding: utf-8 -*-
"""端到端探针：真教材 → 真检索工具 → 真模型回答。

验证的不是「检索排得准不准」（那个在 probe-retrieval.py 里），而是
「模型会不会去查、查完会不会标引用、书里没有会不会明说」。

checkpoint 和文档库都换成临时目录，不碰 data/。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:/agent")
import documents
import retrieval

tmp = Path(tempfile.mkdtemp(prefix="probe-e2e-"))
documents.DB_PATH = tmp / "documents.sqlite3"
documents.UPLOAD_DIR = tmp / "uploads"
documents.ASSET_DIR = tmp / "assets"
documents.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
documents.ASSET_DIR.mkdir(parents=True, exist_ok=True)
retrieval.VECTOR_DB = tmp / "vectors.sqlite3"

import agent_core

agent_core.CHECKPOINT_PATH = tmp / "checkpoints.sqlite"
agent_core.set_retrieval_backend(
    lambda user_id, query, doc_titles, limit: retrieval.search(
        user_id, query, limit=limit, doc_titles=doc_titles
    )
)

QUESTIONS = [
    "第 2 章里常见的时间复杂度类型有哪些？",
    "书上的图 2-9 讲的是什么？",
    "傅里叶变换的时频对偶性怎么证？",
]


async def ask(question: str, thread: str) -> None:
    print("=" * 78)
    print("问：", question)
    tools: list[str] = []
    answer: list[str] = []
    citations: list[dict] = []
    async for event in agent_core.stream_agent("u1", thread, question):
        if event["type"] == "tool_call":
            tools.append(event["tool"])
        elif event["type"] == "assistant_delta":
            answer.append(event["content"])
        elif event["type"] == "citations":
            citations = event["items"]
    print("工具：", tools or "（没调）")
    print("回答：", "".join(answer).strip()[:600])
    print("引用：", [(item["code"], item["chapter"], item["page_start"]) for item in citations])
    await agent_core.shutdown()


def main() -> None:
    base = Path("D:/agent/.workbuddy/tmp/marker-compare/pdf/algo.pdf")
    record = documents.create_upload("u1", "Hello-Algo.pdf", base.read_bytes())
    documents.parse_document(record["doc_id"])
    print("入库：", record["doc_id"], documents.get_document(record["doc_id"], "u1")["status"])
    print("向量：", retrieval.ensure_vectors("u1"))

    async def run() -> None:
        for index, question in enumerate(QUESTIONS):
            await ask(question, f"t{index}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
