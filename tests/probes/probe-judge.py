"""让模型当裁判：几组融合权重，逐条判「这段能不能回答这个问题」。

判据为什么不能用「标准答案 = 术语提问的第一条」：
  「递归」搜出来的第一条是「尾递归」那段，而「函数在自己里面又调用自己，什么时候
  会栈溢出」的答案是另一段（「过深的递归可能导致栈溢出错误」）。术语提问的第一条
  只是**第一个提到这个词的地方**，不是这个问题的答案。用它当标准答案，评出来的是
  「能不能猜中我随便指的哪一段」。

所以：两种检索各取**前 6 条**（这是模型真正会读到的条数），混在一起编号交给
模型逐条打 0/1。这样评的是「给你的这堆材料里有没有能用的」，也正是这个系统的
真实形态——模型自己从 6 条里挑。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:/agent")
from dotenv import load_dotenv

load_dotenv(r"D:/agent/.env")

import documents
import retrieval
import agent_core

CORPUS = Path(r"D:/agent/.workbuddy/tmp/ab-corpus")
LIMIT = 6

QUERIES = [
    "函数在自己里面又调用自己，什么时候会栈溢出",
    "用循环代替一层层往下调用，写法上有什么不一样",
    "输入规模翻倍之后，跑的时间会怎么变",
    "程序会吃掉多少内存",
    "相邻两个比大小、大的往后挪，这种排序叫什么",
    "排好序的数组里，每次砍掉一半来定位",
    "用键值对存东西、查找只要常数时间的那种结构",
    "每一步都挑当前看起来最好的那个",
    "这门课想让我学会什么",
    "期末之外的分数怎么算",
    "信号从这头传到那头要多久",
    "基站为什么要排成一片一片的格子",
    "让模型先把推理步骤写出来再给答案",
    "把要求写清楚，让模型照着做",
    "模型把一句话切成一小块一小块再处理",
    "把自己准备的资料喂给模型再让它回答",
]

CONFIGS = [
    ("纯 BM25", (None, None)),
    ("BM25+向量", (0.75, 0.50)),
]


def build_judge():
    """裁判模型。**和默认配置有三处不同**，每一处都是为了让它更像裁判：

    1. **温度 0**：判 0/1 不是创作，不该有随机性。原来用默认 0.5，同一套配置
       两次跑出 23 和 24——那不是检索的差别，是裁判在抖。
    2. **关掉推理**：deepseek-flash 是推理模型，默认会先吐一段思维链再给答案，
       那部分同样计费、同样费时（64 次调用跑了 9 分钟）。判 0/1 不需要思考。
    3. **可以换成别的家**：`JUDGE_PROVIDER=glm` 就用智谱来判。
       回答用的是 DeepSeek，让同一个模型给自己的检索结果打分有自偏好的嫌疑，
       换一家判一次是对照组（智谱的 chat 额度还在，只是 embedding 没额度）。
    """
    provider = os.getenv("JUDGE_PROVIDER", "deepseek").lower()
    if provider == "glm":
        return agent_core.build_glm_model(temperature=0.0, max_tokens=3000)
    return agent_core.build_model(
        temperature=0.0, max_tokens=3000, disable_reasoning=True
    )

JUDGE = """下面是教材里的若干段原文，编号 A 开始。
问题：{query}

请逐段判断：这一段能不能回答上面这个问题？
1 = 能（直接讲了这个事，或明显就是在解释这件事）；
0 = 不能（只是提到了相关的字，或者根本是别的话题）。

只输出 {count} 个数字，用逗号分隔，不要任何其他文字。例：1,0,0,1,0,0

{chunks}"""


def judge(model, query: str, chunks: list[str]) -> list[int]:
    body = "\n\n".join(f"{chr(65 + index)}. {text}" for index, text in enumerate(chunks))
    prompt = JUDGE.format(query=query, chunks=body, count=len(chunks))
    delay = 10
    for attempt in range(4):
        try:
            reply = model.invoke(prompt)
            break
        except Exception as exc:  # noqa: BLE001 — 429/超时都值得再试
            if attempt == 3:
                raise
            print(f"  (裁判调用失败 {attempt + 1}/3，{delay}s 后重试：{type(exc).__name__})")
            time.sleep(delay)
            delay *= 2
    content = getattr(reply, "content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    digits = [char for char in str(content) if char in "01"]
    while len(digits) < len(chunks):
        digits.append(0)
    return [int(char) for char in digits[: len(chunks)]]


def run(model, index, label: str, weights: tuple) -> dict:
    matrix = index.matrix
    if weights[0] is None:
        index.matrix = None
    else:
        retrieval._W_BM25_WHEN_LITERAL = weights[0]
        retrieval._W_BM25_WHEN_VAGUE = weights[1]
        index.matrix = matrix

    top1 = useful = total = 0
    for query in QUERIES:
        hits = retrieval.search("u1", query, limit=LIMIT)["hits"]
        scores = judge(model, query, [hit["content"] for hit in hits])
        top1 += scores[0] if scores else 0
        useful += sum(scores)
        total += 1 if any(scores) else 0
    print(
        f"{label:<14} 第一条能答 {top1}/{len(QUERIES)}   "
        f"六条里有用段的条数 {useful}   "
        f"至少有一条能答的题 {total}/{len(QUERIES)}"
    )
    index.matrix = matrix
    return {"top1": top1, "useful": useful, "answered": total}


def main() -> None:
    documents.DB_PATH = CORPUS / "documents.sqlite3"
    documents.UPLOAD_DIR = CORPUS / "uploads"
    documents.ASSET_DIR = CORPUS / "assets"
    retrieval.VECTOR_DB = CORPUS / "vectors.sqlite3"

    index = retrieval.get_index("u1")
    model = build_judge()
    print("裁判：", os.getenv("JUDGE_PROVIDER", "deepseek"), "（温度 0）\n")
    for label, weights in CONFIGS:
        run(model, index, label, weights)


if __name__ == "__main__":
    main()
