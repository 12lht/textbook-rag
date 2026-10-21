"""检索层：把一句问题变成「书上哪几段能回答」。

这是应用层模块，和 `documents.py` / `memories.py` 同一个位置：只读 SQLite，
**不 import 任何 agent 相关的东西**，因此也能被命令行、探针、离线评测直接调用。

## 这一层为什么独立存在

`documents.py` 负责「书进来了、切成了段」。这一层负责「问题来了、该看哪几段」。
分开的理由和解析/切分分开是同一条：**换排序策略不该重解析一本书**。
一本书解析一次要几十秒，而调一个打分权重只需要几毫秒——两件事焊在一起，
任何检索侧的实验都得先付一次解析的代价。
反过来也一样：调整检索不该动 `documents` 里的任何东西，删书、换切分策略
都不该穿过打分逻辑。

## 为什么不是向量检索

没有 embedding 服务：这台机器上跑不了本地向量模型，外部 API 也不提供 embedding。
在「必须能离线跑、必须零框架依赖」这两个前提下，**BM25 + 字符 n-gram** 是唯一
能立刻跑起来的选择，而且它对「术语、公式号、图号」这类**精确串**比向量更准
——这正是 B2 的验收点（拿 10 个术语/式号搜，召回 ≥ 8）。

代价要认：它不懂同义改写。「抽样定理」和「采样定理」在它眼里是两个词。
这一条现在靠模型在查之前**自己换一次说法**（多查一次）来兜，不靠这一层。

## 中文怎么切

不引入分词库（jieba 的体积和词典维护成本都超过收益）。做法是**二元字符组**：

    抽样定理 → 抽样 / 样定 / 定理   （bigram，主信号）
               + 抽 / 样 / 定 / 理    （unigram，弱信号，权重 0.3）

bigram 决定了「定理」不会命中「定义」，unigram 兜住「两个字只对一个字」的模糊
命中。纯英文/数字按整词切（`5G`、`RAN`、`3.5`），它们在中文教材里是稳定的术语。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import documents

# numpy 只用来算「一条查询 × 几千条向量」的点积：纯 Python 循环 9000×1024
# 要几秒，numpy 是十几毫秒。没装就退回 BM25（不是致命依赖）。
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

# 字符 n-gram 的权重。bigram 是主信号；unigram 只用来兜「部分命中」，
# 权重压到 0.3 是因为「的、是、在」这类字几乎每段都有，给足权重会让
# 任何一段都像是相关段落。英文/数字整词比 bigram 更可靠（不会切出半个词），
# 权重给到 1.2。
_W_BIGRAM = 1.0
_W_UNIGRAM = 0.3
_W_WORD = 1.2

# BM25 的两个惯例参数。k1 控制词频饱和（同一段里第 10 次出现不该比第 2 次
# 强 5 倍），b 控制长度惩罚（长段落天然容易命中，要压一压）。
# 不调这两个：现在的语料是教材段落，长度分布很窄（中位 200-500 字），
# 调参的收益小于「换一个切分策略」带来的差异。
_K1 = 1.5
_B = 0.75

# 命中整串时的加成（乘在 BM25 分数上，所以和语料规模无关）。
# 「图 2‑5」「式 3-1」「抽样定理」这类查询，整串出现几乎就等于命中，
# 而 BM25 只按 bigram 累加，会把「抽样」和「定理」分开的段落也算高分。
_PHRASE_BOOST = 1.35  # 整串出现在正文里
_HEADING_BOOST = 1.15  # 只出现在章节标题/书名里

# 判「书里没有」的阈值。coverage 是「问题里的信息量被这一段覆盖了多少」
# （按 idf 加权），不是原始分数——原始分数随查询长度和语料规模浮动，
# 没法拿来当固定阈值。
#
# 0.30 是拿三本书的真实问题标出来的：书里有的问题 coverage 都在 0.5 以上，
# 书里没有的问题（「今天北京天气」这种）基本停在 0.1 以下，中间有一段空档。
_MIN_COVERAGE = 0.30

# 一段正文进模型上下文时的截断长度。整段给出去没必要：答案通常落在前两句，
# 而 6 条 900 字的段落会把窗口塞满、还会把用户的问题挤出注意力。
MAX_HIT_CHARS = 700

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_ASCII_RUN = re.compile(r"[a-z0-9]+(?:[.\-_+/][a-z0-9]+)*")

# 各种横线归一成一个 ASCII 连字符。教材里的「图 2‑5」用的是 U+2011（不换行
# 连字符），用户键盘打出来的是 `-`；不归一就永远搜不到图号。
# 这不是吹毛求疵：`_match_caption` 当初就是因为这条踩过一次。
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"

# 问句里的填充词。**必须先剥掉再算覆盖率**，否则它会被当成「问题里有、书里没有」
# 的关键信息，把「对数阶出现在什么场合」这种正常提问的覆盖率压到 0.17——
# 而它明明第一条就命中了。
#
# 只收**多字词 + 纯语气助词**，不碰单字实词：「能」会被误伤「能耗」，
# 「有」会拆坏「有功功率」，「在」会拆坏「在线」。宁可漏剥也不能拆坏术语。
_FILLER = (
    "什么", "怎么", "怎么样", "如何", "为什么", "为何", "哪些", "哪个", "哪里",
    "多少", "是否", "请问", "帮我", "介绍", "说明", "讲讲", "一下", "可以",
    "这个", "那个", "我们", "你们", "它", "的", "了", "呢", "吗", "吧",
)
_FILLER_RE = re.compile("|".join(sorted(_FILLER, key=len, reverse=True)))


def strip_filler(text: str) -> str:
    return " ".join(_FILLER_RE.sub(" ", canonical(text)).split())


def canonical(text: str) -> str:
    """小写 + 全角转半角 + 横线归一 + 空白折叠。检索两端都用它。"""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    for dash in _DASHES:
        text = text.replace(dash, "-")
    return " ".join(text.split())


def flat(text: str) -> str:
    """去掉所有空白和标点的极简形态，只用于「整串是否出现」的判断。"""
    return "".join(ch for ch in canonical(text) if ch.isalnum() or ch in "-+#./")


def title_key(text: str) -> str:
    """书名归一化：只留字母、数字和汉字，去掉空格与**一切**标点。

    为什么不复用 `canonical`：它保留标点，拿它做子串判断会被排版差异挡住——
    用户说「第3章 电波传播与传播预测模型」，实际书名是
    「第3章+电波传播与传播预测模型20260407」，中间那个 '+' 与空格对不上，
    子串判断直接 False，于是「指定了书却一本都没命中」。书名匹配要的是
    「忽略排版的差异」，所以比 `flat` 更狠：连 + - 也不留。
    """
    return "".join(ch for ch in canonical(text) if ch.isalnum())


_CHAPTER_HINT = re.compile(r"第\s*([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*章")
_CN_DIGIT = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9",
}


def _normalize_chapter(raw: str) -> str:
    """把「三」「十二」「二十三」换成阿拉伯数字。书名里写的是「第3章」。"""
    if raw.isdigit():
        return str(int(raw))
    if raw in _CN_DIGIT:                           # 一 ~ 九
        return _CN_DIGIT[raw]
    if raw == "十":
        return "10"
    if raw.startswith("十"):                       # 十一 ~ 十九
        return "1" + _CN_DIGIT.get(raw[1], "0")
    if len(raw) == 2 and raw[1] == "十":           # 二十、三十
        return _CN_DIGIT.get(raw[0], "0") + "0"
    if len(raw) == 3 and raw[1] == "十":           # 二十三
        return _CN_DIGIT.get(raw[0], "0") + _CN_DIGIT.get(raw[2], "0")
    return raw


def chapter_hint(query: str) -> str | None:
    """从问题里认出「第 3 章」这种章节线索，返回归一化后的「第3章」。

    出现**两个及以上不同**章号时返回 None：用户说的是「第3章和第5章有什么
    区别」，那种提问要跨书比对，限定反而会砍掉一半。
    """
    found = {m.group(1) for m in _CHAPTER_HINT.finditer(str(query or ""))}
    if len(found) != 1:
        return None
    return f"第{_normalize_chapter(found.pop())}章"


def _scope_positions(index: "_Index", keys: list[str]) -> set[int]:
    """书名归一化后能命中哪几本，返回它们在 `index.docs` 里的下标。"""
    return {
        position
        for position, doc in enumerate(index.docs)
        if any(
            key and key in title_key(str(doc.meta.get("doc_title") or ""))
            for key in keys
        )
    }


def _longest_common(left: str, right: str, *, cap: int = 12, floor: int = 4) -> int:
    """`left` 有多长的一段**原样**出现在 `right` 里（从长到短试，命中即返回）。

    不做完整的「最长公共子串」动态规划：那要 O(n·m) 的两层循环，285 段 × 700 字
    就跑不动了。这里的查询短（十几个字），从长到短枚举它的子串、用 C 层的
    `in` 去查，绝大多数情况下试两三次就返回。
    """
    size = min(len(left), cap)
    for length in range(size, floor - 1, -1):
        for start in range(0, len(left) - length + 1):
            if left[start : start + length] in right:
                return length
    return 0


def tokenize(text: str) -> list[tuple[str, float]]:
    """→ `[(token, weight)]`。索引和查询走同一个函数，不允许两套切法。"""
    norm = canonical(text)
    tokens: list[tuple[str, float]] = []
    for run in _CJK_RUN.findall(norm):
        if len(run) == 1:
            tokens.append((f"u:{run}", _W_UNIGRAM))
            continue
        for index in range(len(run) - 1):
            tokens.append((f"b:{run[index:index + 2]}", _W_BIGRAM))
        for char in run:
            tokens.append((f"u:{char}", _W_UNIGRAM))
    for word in _ASCII_RUN.findall(norm):
        if len(word) >= 2:
            tokens.append((f"w:{word}", _W_WORD))
    return tokens


@dataclass
class _Doc:
    """一段的索引形态。原文留在 `content` 里，进上下文时才截断。"""

    meta: dict
    tf: Counter = field(default_factory=Counter)
    length: float = 0.0
    flat: str = ""


class _Index:
    """一个用户的全部段落 + 倒排表。**每次语料变化就整体重建**。

    不增量更新：一个用户撑死几十本书、几万段，重建一次不到一秒；而增量更新
    要处理「解析完一本书的 chunk 全换了」（chunk_id 带 seq，重新切分后整本都变），
    维护成本远大于收益。
    """

    def __init__(self, chunks: list[dict], signature: tuple) -> None:
        self.signature = signature
        self.docs: list[_Doc] = []
        self.postings: dict[str, list[tuple[int, float]]] = {}
        for chunk in chunks:
            self.docs.append(self._index_chunk(chunk))
        self.total_len = sum(doc.length for doc in self.docs) or 1.0
        self.avg_len = self.total_len / max(len(self.docs), 1)
        self.position_of = {
            str(doc.meta.get("chunk_id")): position for position, doc in enumerate(self.docs)
        }
        # 每本书的图段索引：{doc_id: [(position, asset_id, 题注首行)]}。给命中
        # 段落标注「本节附近有图」用——图段只有一行题注（十几二十字），概念性
        # 提问的词和题注对不上，几乎排不进候选池；把图挂到邻近正文段的返回上，
        # 由模型决定要不要展示。书重建时整个 _Index 重建，索引随之更新。
        self.figures_by_doc: dict[str, list[tuple[int, str, str]]] = {}
        for position, doc in enumerate(self.docs):
            if str(doc.meta.get("kind") or "") != "figure" or not doc.meta.get("asset_id"):
                continue
            content = str(doc.meta.get("content") or "")
            caption = content.splitlines()[0].strip() if content else ""
            self.figures_by_doc.setdefault(str(doc.meta.get("doc_id")), []).append(
                (position, str(doc.meta["asset_id"]), caption)
            )
        for position, doc in enumerate(self.docs):
            for token, count in doc.tf.items():
                self.postings.setdefault(token, []).append((position, count))
        # 向量矩阵（每行一段，行号 → 段落位置由 `vector_positions` 记）。
        # **读不到向量不算错误**：没配服务、还没算、换过模型，都退回纯 BM25。
        self.matrix: "np.ndarray | None" = None
        self.vector_positions: list[int] = []
        self.load_vectors()

    def load_vectors(self) -> None:
        # 没配向量服务时**连文件都不碰**：`_connect_vectors()` 会建库，
        # 一个纯聊天部署不该因为这条路多出一个空的 sqlite 文件。
        if np is None or not embedding_enabled() or not self.docs:
            return
        doc_ids = sorted({str(doc.meta.get("doc_id")) for doc in self.docs})
        stored = _existing_vectors(doc_ids)
        if not stored:
            return
        vectors: list = []
        positions: list[int] = []
        for chunk_id, (_dim, blob) in stored.items():
            position = self.position_of.get(chunk_id)
            if position is None:
                # 向量是按老切分算的（chunk_id 变了），跳过——它已经不指向任何一段。
                continue
            vectors.append(np.frombuffer(blob, dtype="float32"))
            positions.append(position)
        if vectors:
            self.matrix = np.vstack(vectors)
            self.vector_positions = positions

    def vector_ranked(self, query: str, *, limit: int = 40) -> list[tuple[float, int]]:
        """→ `[(余弦, 段落位置)]`，按余弦降序。"""
        if self.matrix is None or np is None:
            return []
        vector = _query_vector(query)
        if vector is None:
            return []
        similarities = self.matrix @ vector
        order = np.argsort(-similarities)[:limit]
        return [(float(similarities[index]), self.vector_positions[index]) for index in order]

    def _index_chunk(self, chunk: dict) -> _Doc:
        content = str(chunk.get("content") or "")
        doc = _Doc(meta=chunk, flat=flat(content))
        # 章节和书名也算这一段的「可检索文字」，但只给一部分权重——它们是
        # 上下文，不是这段话在说什么。给满权重的话，同一章的每一段都会因为
        # 章节名而命中，排序就失去区分度。
        for source, scale in (
            (content, 1.0),
            (str(chunk.get("chapter") or ""), 0.5),
            (str(chunk.get("doc_title") or ""), 0.3),
        ):
            if not source:
                continue
            for token, weight in tokenize(source):
                doc.tf[token] += weight * scale
                doc.length += weight * scale
        return doc

    def idf(self, token: str) -> float:
        """逆文档频率。**语料里根本没有的词给最高分，而不是 0**。

        这一条决定「书里没有」判不判得出来。如果把没出现过的词记成 idf=0，
        它就既不进分子也不进分母——「Python 的 asyncio 怎么用」里 `asyncio`
        不存在，但 `python` 存在，覆盖率会被算成 70%，于是判成「书里有」。
        一个词在整本书里一次都没出现，恰恰是「这段回答不了这个问题」最强的证据。
        """
        n = len(self.docs)
        df = len(self.postings.get(token, ()))
        if not df:
            return math.log(1 + (n + 0.5) / 0.5)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str) -> list[tuple[float, float, int]]:
        """→ `[(score, coverage, doc_index)]`，按分数降序。"""
        query_tokens = Counter()
        for token, weight in tokenize(query):
            query_tokens[token] = max(query_tokens[token], weight)
        if not query_tokens:
            return []

        idf = {token: self.idf(token) for token in query_tokens}
        total_idf = sum(idf.values()) or 1.0

        scores: dict[int, float] = {}
        matched: dict[int, float] = {}
        for token, weight in query_tokens.items():
            weight_idf = idf[token]
            for position, count in self.postings.get(token, ()):
                length = self.docs[position].length or 1.0
                norm = _K1 * (1 - _B + _B * length / self.avg_len)
                scores[position] = scores.get(position, 0.0) + weight_idf * (
                    count * (_K1 + 1) / (count + norm)
                )
                matched[position] = matched.get(position, 0.0) + weight_idf * weight

        query_flat = flat(query)
        results: list[tuple[float, float, int]] = []
        for position, raw in scores.items():
            doc = self.docs[position]
            # 加成看的是「问题里最长的一段话原样出现在这一段里」，不是整句。
            # 早先拿**整句**去比：`图 2-10 讲了什么` 永远匹配不上，因为正文里
            # 只有「图 2‑10 常数阶……」——于是精确图号查询反而不如模糊词命中。
            # 用户问句里必然夹着「讲了什么」这类废话，只能按最长公共子串算。
            common = _longest_common(query_flat, doc.flat)
            if common >= 4:
                raw *= 1 + 0.06 * common
            else:
                heading = flat(
                    f"{doc.meta.get('chapter') or ''} {doc.meta.get('doc_title') or ''}"
                )
                if _longest_common(query_flat, heading) >= 4:
                    raw *= _HEADING_BOOST
            results.append((raw, matched[position] / total_idf, position))
        results.sort(key=lambda item: item[0], reverse=True)
        return results


# --------------------------------------------------------------------------
# 向量：换个说法也能搜到
# --------------------------------------------------------------------------
#
# BM25 只数字面重合。「算法跑得快不快」在 Hello-Algo 上只拿到 0.07 的覆盖率，
# 因为它切出来的 `法跑/跑得/得快/快不/不快` 这五个片段整本书里一次都没出现过。
# 向量补的就是这个洞：把段落压成一个点，意思近的点靠得近，**不需要共享任何字**。
#
# 但它不能替代 BM25：`图 2-10`、`式 3-1`、`SNR` 这类精确串，字面匹配比向量准得多
# （向量会把「图 2-10」和「图 2-11」判成几乎一样）。所以两边各出一份排名，用
# RRF 融合——只比**名次**，不比分数，因为两者的分数根本不同量纲（BM25 是几十的
# 量级，余弦是 0~1）。

# 向量存哪：**独立的一个 SQLite 文件**，不进 `documents.sqlite3`。
#
# 理由是「换排序策略不该重解析一本书」的同一条：向量是检索层的派生物，
# 换模型、换维度、算错重算，都只该动这个文件。代价是删书时要显式清理
# （`forget_document`，由 service 在删书成功后调用）。
VECTOR_DB = Path(__file__).with_name("data") / "vectors.sqlite3"

# 一批送多少条。硅基流动/智谱都接受数组输入，一次几十条能省掉大量往返；
# 太大的批次会把单次请求拖到超时，得不偿失。
EMBEDDING_BATCH = 32

# 融合时向量那边取前多少名。取 40 是因为最终只回 6 条：再往后的名次
# 几乎不可能翻上来，却会让每次查询多算几十条。
_VECTOR_DEPTH = 40

# 字面命中 / 字面很弱时，BM25 各占多少权重。
# 这两个数是拿 `probe-judge.py`（让模型逐条判「能不能回答」）在 16 条换说法
# 提问上对比出来的，不是从论文抄的。
_W_BM25_WHEN_LITERAL = 0.75
_W_BM25_WHEN_VAGUE = 0.50


# 余弦低于这个值就不参与融合。bge-m3 的相似度整体偏高（不相关也常有 0.3~0.4），
# 只有明显高于底噪的才当作「语义上真的相关」。
_VEC_MIN_COS = 0.45

# 只有向量命中、字面完全不重合时，靠它来判「这段确实回答了问题」。
# 阈值是从真书上量出来的，见 `probe-retrieval.py` 的对照表。
_VEC_CONFIDENT_COS = 0.58

# —— rerank（2026-10-21 接入，复合分权重平移自 WeKnora 公开默认参数）——
#
# 管线：加权融合 → 取融合分前 _RERANK_POOL 名候选 → rerank 模型逐对打分 →
# 复合分 = 模型分*0.6 + 融合归一分*0.3 + 来源权重*0.1（本项目来源只有教材，恒 1.0，
# 那项是常数）→ 取前 limit。对照组数据见 probe-rerank.py / probe-weknora.py：
# 有用段 26→28/29，且 rerank 分数分离度（0.87 vs 0.03）远好于双塔 cosine，
# 可以当「书里有没有」的置信信号。
#
# **没配 key / 调用失败一律静默降级回融合排序**——rerank 是锦上添花，不是依赖；
# RERANK_ENABLED=0 可整体关掉（评测/调试用）。
_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
_RERANK_POOL = 50
_RERANK_TIMEOUT = 30
_RERANK_W_MODEL = 0.6
_RERANK_W_RETRIEVAL = 0.3

# 短块邻居扩展：检索粒度碎（TARGET_CHARS=250）、喂模型粒度厚。
# 250 字的碎块自含性不够，扩同书后邻到约 850 字再给模型——WeKnora「检索碎、
# 合并厚」的同款设计。只往后扩、不动 chunk_id：引用锚点还是命中那一段。
_NEIGHBOR_MIN = 350
_NEIGHBOR_TARGET = 850


def _embedding_api_key() -> str:
    """读环境变量。**延迟到调用时读**——探针可能在 import 之后才 load_dotenv。"""
    return (os.getenv("EMBEDDING_API_KEY") or os.getenv("SILICON_API_KEY") or "").strip()


def _embedding_base_url() -> str:
    return (os.getenv("EMBEDDING_BASE_URL") or "https://api.siliconflow.cn/v1").rstrip("/")


def _embedding_model() -> str:
    return (os.getenv("EMBEDDING_MODEL") or "BAAI/bge-m3").strip()


def embedding_enabled() -> bool:
    return bool(_embedding_api_key()) and np is not None


def _connect_vectors() -> sqlite3.Connection:
    VECTOR_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(VECTOR_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vectors ("
        " chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, model TEXT NOT NULL,"
        " dim INTEGER NOT NULL, vec BLOB NOT NULL, created_at TEXT NOT NULL)"
    )
    return conn


def _embed(texts: list[str]) -> list[list[float]]:
    """调 OpenAI 兼容的 embeddings 接口。**失败就抛**，由调用方决定降级。"""
    key = _embedding_api_key()
    if not key:
        raise RuntimeError("没有配置向量服务的 API Key。")
    payload = json.dumps(
        {
            "model": _embedding_model(),
            "input": texts,
            "encoding_format": "float",
        }
    ).encode()
    url = f"{_embedding_base_url()}/embeddings"
    last: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.load(response)
            # 返回顺序与输入一一对应（`index` 字段兜底，别假定数组顺序）。
            data = sorted(body["data"], key=lambda item: item.get("index", 0))
            return [item["embedding"] for item in data]
        except urllib.error.HTTPError as exc:
            last = exc
            # 429/5xx 值得重试（免费档限流是常态）；4xx 其他错误重试也没用。
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"向量服务连续失败：{last}")


def _normalize(vector: list[float]) -> "np.ndarray":
    """单位化。存的时候就除好长度，查询时余弦退化成一次点积。"""
    array = np.asarray(vector, dtype="float32")
    norm = float(np.linalg.norm(array)) or 1.0
    return array / norm


def _vector_text(chunk: dict) -> str:
    """送进向量模型的那段文字。

    **带上章节标题**：`- 教材：《移动通信》` 这种孤立的一行，光看内容是判断不出
    它在讲什么的；加上「3.2 抽样定理」之后，它才有机会被语义搜到。
    截断到 1200 字：bge-m3 能吃 8K token，但一条 chunk 极少超过这个长度，
    真超了也是代码块，语义信息集中在前半段。
    """
    content = str(chunk.get("content") or "")[:1200]
    chapter = str(chunk.get("chapter") or "").strip()
    return f"{chapter}\n{content}" if chapter else content


def _existing_vectors(doc_ids: list[str]) -> dict[str, tuple[int, bytes]]:
    model = _embedding_model()
    placeholders = ",".join("?" * len(doc_ids))
    conn = _connect_vectors()
    try:
        rows = conn.execute(
            f"SELECT chunk_id, dim, vec FROM vectors WHERE model = ?"
            f" AND doc_id IN ({placeholders})",
            (model, *doc_ids),
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: (row[1], row[2]) for row in rows}


def ensure_vectors(user_id: str, *, limit_batches: int | None = None) -> dict:
    """把这个用户**缺的**向量补齐（阻塞，调用方要丢后台）。

    按 chunk 增量补：已经算过的跳过，所以重复调用很便宜。新增一本书只算那一本。
    **不能放在提问路径上**：一本 700 页的书有几千段，一次补完要一两分钟。
    """
    if not embedding_enabled():
        return {"embedded": 0, "total": 0, "reason": "未配置向量服务"}

    chunks = documents.chunks_of_user(user_id)
    if not chunks:
        return {"embedded": 0, "total": 0, "reason": "没有可索引的教材"}

    doc_ids = sorted({str(chunk["doc_id"]) for chunk in chunks})
    have = _existing_vectors(doc_ids)
    missing = [chunk for chunk in chunks if str(chunk["chunk_id"]) not in have]
    if not missing:
        return {"embedded": 0, "total": len(chunks)}

    dim = next((item[0] for item in have.values()), 0)
    done = 0
    try:
        for start in range(0, len(missing), EMBEDDING_BATCH):
            if limit_batches is not None and start // EMBEDDING_BATCH >= limit_batches:
                break
            batch = missing[start : start + EMBEDDING_BATCH]
            vectors = _embed([_vector_text(chunk) for chunk in batch])
            rows = []
            for chunk, vector in zip(batch, vectors):
                array = _normalize(vector)
                dim = dim or len(array)
                rows.append(
                    (
                        str(chunk["chunk_id"]),
                        str(chunk["doc_id"]),
                        _embedding_model(),
                        len(array),
                        array.tobytes(),
                        datetime.now().isoformat(timespec="seconds"),
                    )
                )
            conn = _connect_vectors()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO vectors"
                    " (chunk_id, doc_id, model, dim, vec, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
            done += len(batch)
    except Exception as exc:  # noqa: BLE001 — 补到一半失败也要留下已完成的
        clear_cache(user_id)
        return {"embedded": done, "total": len(chunks), "error": str(exc)[:200]}

    # 索引里可能缓存着「没有向量」的旧矩阵，清掉让它下次重读。
    clear_cache(user_id)
    return {"embedded": done, "total": len(chunks)}


def forget_document(doc_id: str) -> None:
    """删书时清掉它的向量。不清理的话，几本删过的书会留下几十 MB 的死数据。"""
    if not VECTOR_DB.exists():
        return
    conn = _connect_vectors()
    try:
        conn.execute("DELETE FROM vectors WHERE doc_id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()


_vector_cache_lock = threading.Lock()
_query_vectors: dict[str, "np.ndarray"] = {}


def _query_vector(query: str) -> "np.ndarray | None":
    """查询向量。**按文本缓存**：同一轮里模型常把同一个问题查两次，
    第二次不该再花一次网络往返和一分钱。
    """
    if not embedding_enabled():
        return None
    with _vector_cache_lock:
        cached = _query_vectors.get(query)
    if cached is not None:
        return cached
    try:
        vector = _normalize(_embed([query])[0])
    except Exception:  # noqa: BLE001 — 向量挂了就退回纯 BM25，不许让检索失败
        return None
    with _vector_cache_lock:
        if len(_query_vectors) > 200:
            _query_vectors.clear()
        _query_vectors[query] = vector
    return vector


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, _Index] = {}


def _signature(user_id: str) -> tuple:
    """语料指纹：书、段数、更新时间。任何一个变了就重建索引。

    不看 `status` 之外的字段：解析时间戳 `updated_at` 已经在里面了，
    重新解析一本书必然改它。
    """
    documents_list = documents.list_documents(user_id)
    return tuple(
        sorted(
            (
                item["doc_id"],
                item["status"],
                item.get("chunk_count"),
                item.get("updated_at"),
            )
            for item in documents_list
            if item["status"] == documents.STATUS_READY
        )
    )


def get_index(user_id: str) -> _Index:
    signature = _signature(user_id)
    with _lock:
        cached = _cache.get(user_id)
        if cached is not None and cached.signature == signature:
            return cached
    index = _Index(documents.chunks_of_user(user_id), signature)
    with _lock:
        _cache[user_id] = index
    return index


def clear_cache(user_id: str | None = None) -> None:
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


# --------------------------------------------------------------------------
# 对外：检索
# --------------------------------------------------------------------------

def _rerank_scores(query: str, texts: list[str]) -> list[float] | None:
    """调 rerank 模型给「问题 × 段落」逐对打分。**失败返回 None，绝不抛**。

    调用方拿 None 就走融合排序降级。rerank 是外部 API，挂掉不能拖死检索。
    """
    key = _embedding_api_key()
    if not key or os.getenv("RERANK_ENABLED", "1") == "0" or not texts:
        return None
    payload = json.dumps(
        {"model": _RERANK_MODEL, "query": query, "documents": texts, "top_n": len(texts)}
    ).encode()
    url = f"{_embedding_base_url()}/rerank"
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_RERANK_TIMEOUT) as response:
                body = json.load(response)
            scores = [0.0] * len(texts)
            for item in body.get("results", []):
                idx = int(item.get("index", -1))
                if 0 <= idx < len(texts):
                    scores[idx] = float(item.get("relevance_score", 0.0))
            return scores
        except Exception:  # noqa: BLE001 — 任何失败都降级，rerank 没有「必须成功」的理由
            if attempt == 1:
                return None
            time.sleep(1.0)


def _expand_neighbors(docs: list["_Doc"], position: int, base: str) -> str:
    """短块邻居扩展：命中段太短（< `_NEIGHBOR_MIN`）就往后拼同书相邻段。

    邻居只是给模型的上下文，**不动 chunk_id / 页码**——引用锚点还是命中那一段。
    chunk_id 形如 `{doc_id}-{seq:04d}`，seq 加一就是物理上的下一段。
    """
    if len(base) >= _NEIGHBOR_MIN:
        return base
    doc_id = str(docs[position].meta.get("doc_id") or "")
    try:
        seq = int(str(docs[position].meta.get("chunk_id")).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return base
    positions = {
        int(str(d.meta.get("chunk_id")).rsplit("-", 1)[1]): p
        for p, d in enumerate(docs)
        if str(d.meta.get("doc_id") or "") == doc_id
    }
    out = base
    nxt = seq + 1
    while len(out) < _NEIGHBOR_TARGET:
        nxt_pos = positions.get(nxt)
        if nxt_pos is None:
            break
        piece = str(docs[nxt_pos].meta.get("content") or "")
        # 允许 120 字的尾部溢出（别把最后一段切得只剩几个字），但不无限涨
        remaining = _NEIGHBOR_TARGET + 120 - len(out)
        if remaining <= 0:
            break
        out += "\n" + piece[:remaining]
        nxt += 1
    return out


def search(
    user_id: str,
    query: str,
    *,
    limit: int = 6,
    doc_titles: list[str] | None = None,
) -> dict:
    """查这个人的教材，返回最相关的几段。

    `doc_titles` 用于「指定书名提问」（B6）：按书名做**归一化子串**匹配——忽略
    空格与标点，只比字母数字和汉字（`title_key`），所以「第3章 电波传播」能命中
    「第3章+电波传播与传播预测模型20260407」。不要求把书名打全（「移动通信」要能
    命中「第1章 移动通信概述」）。指定了但一本都没命中时返回空结果 + 现有书名
    列表，**不退回全库**——那会让「问 A 书，答 B 书」看起来像检索成功。
    """
    query = str(query or "").strip()
    if not query:
        return {"hits": [], "confident": False, "reason": "问题为空。"}

    # 剥掉问句填充词。**索引侧不剥**——正文里这些词本来就是内容的一部分，
    # 剥了只会让「什么」这类词在正文里留下的 bigram 对不上。
    query = strip_filler(query)
    if not query:
        return {"hits": [], "confident": False, "reason": "问题里没有可检索的词。"}

    index = get_index(user_id)
    if not index.docs:
        return {"hits": [], "confident": False, "reason": "这个用户还没有可检索的教材。"}

    scored = index.score(query)
    allowed: set[int] | None = None

    # 「第 3 章」这类线索自动限定到那本书。
    #
    # 实测（`probe-chapter-tasks.py`）：用户问「第3章 电波传播… 这一章讲什么」，
    # 模型连着 9 次检索都没填 `doc_title`——工具描述里写明要填它也不填。结果是
    # 第 5 章的 MIMO 内容混进第 3 章的答案，引用页码跑到 160，而那本书只有 112 页。
    # 模型靠不住，就让代码用这条线索：它明明写在 query 里。
    #
    # 只在**恰好一个**章号时生效（见 `chapter_hint`），且必须真有书名能对上，
    # 否则不动，交给原来的全库检索。
    if not doc_titles:
        hint = chapter_hint(query)
        if hint and any(
            title_key(hint) in title_key(str(doc.meta.get("doc_title") or ""))
            for doc in index.docs
        ):
            doc_titles = [hint]

    if doc_titles:
        wanted = [title_key(title) for title in doc_titles if str(title or "").strip()]
        allowed = _scope_positions(index, wanted)
        if not allowed:
            # 模型很爱把书名和章号拼成一句（「移动通信 第3章」「《移动通信》第3章」），
            # 整串当子串去比必然落空——书名是「第3章+电波传播与传播预测模型…」，
            # 里面根本没有「移动通信」四个字。退一步：从它填的话里、以及 query 里
            # 各抽一次章号，用章号去匹配。这一步实测能救回「0 条命中导致模型
            # 直接拒答」那种最坏结果。
            for source in [*doc_titles, query]:
                hint = chapter_hint(str(source))
                if not hint:
                    continue
                hit_positions = _scope_positions(index, [title_key(hint)])
                if hit_positions:
                    doc_titles = [hint]
                    allowed = hit_positions
                    break
        if not allowed:
            titles = "、".join(str(title) for title in doc_titles)
            # 把**全部**书名报回去。只给前 12 本会出事：实测模型填错书名后拿到
            # 一份以「1_」「10_」开头的书目，就断定「书库里没有移动通信」并拒答。
            names = sorted({str(doc.meta.get("doc_title") or "") for doc in index.docs})
            return {
                "hits": [],
                "confident": False,
                "reason": f"没有找到书名包含「{titles}」的教材。现有的书有：{'、'.join(names)}",
            }

    # 两路排名 → 一份结果。
    #
    # **不用 RRF**（试过，见 probe 的对照数据）：RRF 只看名次，会把 BM25 的强命中
    # 压平——「什么是提示工程」BM25 排第 2，融合后掉到第 5，因为向量那边起来的
    # 几条把名次挤掉了。而两路的分数本来就不是一回事，硬融只能互相伤害。
    #
    # 改成「按字面可信度分配权重」：
    #   - 字面覆盖率够高（问题里的词书里都有），说明这是**术语型提问**，
    #     BM25 更准（它不会被「图 2-10 / 图 2-11」这种近义串骗到）→ 0.75 : 0.25
    #   - 字面几乎不命中，说明是**口语/同义改写**，只能靠语义 → 0.35 : 0.65
    # 这个判据用的还是 coverage，和「书里有没有」用的是同一个量，不引入新阈值。
    max_bm25 = max((item[0] for item in scored), default=1.0) or 1.0
    top_coverage_raw = scored[0][1] if scored else 0.0
    # 两个权重是**可调参数**（离线评测时按数据调，不是拍脑袋定的）。
    # 直觉：字面命中的时候 BM25 的第一条通常是对的，让向量去填空位就行；
    # 字面几乎不命中时 BM25 在瞎猜，多给向量一些权重。
    # 但**不能给够到让向量随便挤掉第一条**——实测 BM25 第一条在「换说法」的
    # 提问上仍然比向量的第一条更准（7/16 对 5/16），挤掉是净亏。
    if top_coverage_raw >= _MIN_COVERAGE:
        weight_bm25 = _W_BM25_WHEN_LITERAL
    else:
        weight_bm25 = _W_BM25_WHEN_VAGUE
    weight_vector = 1.0 - weight_bm25

    combined: dict[int, float] = {}
    coverage_of: dict[int, float] = {}
    cosine_of: dict[int, float] = {}
    for score, coverage, position in scored:
        if allowed is not None and position not in allowed:
            continue
        combined[position] = weight_bm25 * (score / max_bm25)
        coverage_of[position] = coverage
    for similarity, position in index.vector_ranked(query, limit=_VECTOR_DEPTH):
        if similarity < _VEC_MIN_COS:
            continue
        if allowed is not None and position not in allowed:
            continue
        combined[position] = combined.get(position, 0.0) + weight_vector * similarity
        cosine_of[position] = similarity

    order = sorted(combined, key=lambda position: combined[position], reverse=True)

    # rerank 候选池 = **BM25 前 50 ∪ 向量前 50 的并集**，不是融合分前 50。
    # 融合分会把「单路独有」的候选排到池外——纯向量才能挖出来的换说法段、
    # BM25 才能命中的精确串——rerank 就没机会捞了（实测 24→28+ 的差距就在这）。
    cand_pos: list[int] = []
    seen: set[int] = set()
    for pos in order[:_RERANK_POOL]:
        if pos not in seen:
            seen.add(pos)
            cand_pos.append(pos)
    for sim, pos in index.vector_ranked(query, limit=_RERANK_POOL):
        if sim < _VEC_MIN_COS:
            continue
        if pos not in seen:
            seen.add(pos)
            cand_pos.append(pos)

    # 复合分（WeKnora 同款 0.6/0.3/0.1）：模型分主导排序，融合归一分兜底
    # （融合信号不丢），来源项在本项目是常数 0.1。失败/没配 key 时 rr=None，
    # 这里天然回退成纯融合排序——降级路径不用单独写。
    rr = _rerank_scores(
        query,
        [str(index.docs[p].meta.get("content") or "")[:1200] for p in cand_pos],
    )
    if rr is not None:
        max_c = max((combined.get(p, 0.0) for p in cand_pos), default=1.0) or 1.0
        composite = {
            pos: _RERANK_W_MODEL * rr[i]
            + _RERANK_W_RETRIEVAL * (combined.get(pos, 0.0) / max_c)
            + 0.1
            for i, pos in enumerate(cand_pos)
        }
        order = sorted(composite, key=composite.get, reverse=True)
    order = order[: max(1, limit)]

    result = _build_hits(index, order, combined, coverage_of, cosine_of)
    # 这次**实际生效**的书名限定（模型填的，或从「第3章」自动认出来的）。
    # 调用方把它记进本轮上下文，后面模型把 query 改写成「3.6 移动信道…」时
    # 就不会丢掉限定、把别本书的内容搜进来。
    result["scoped"] = doc_titles[0] if doc_titles else None
    return result


def _nearby_figures(index: "_Index", meta: dict, max_count: int = 2) -> list[dict]:
    """找一段正文同页（±1 页）的插图，返回 [{asset_id, caption}]。

    「邻近」按页码判：书上的图几乎总在讲它的那段话的同一页（±1 页吸收
    跨页排版）。找不到就返回空列表——没图就明说，不让模型猜。
    """
    doc_id = str(meta.get("doc_id") or "")
    candidates = index.figures_by_doc.get(doc_id) or []
    if not candidates:
        return []
    try:
        page_start = int(meta.get("page_start") or 0)
        page_end = int(meta.get("page_end") or page_start)
    except (TypeError, ValueError):
        return []
    if not page_start:
        return []
    pages = set(range(page_start - 1, page_end + 2))
    nearby = []
    for position, asset_id, caption in candidates:
        figure_page = index.docs[position].meta.get("page_start")
        try:
            if figure_page is None or int(figure_page) not in pages:
                continue
        except (TypeError, ValueError):
            continue
        nearby.append({"asset_id": asset_id, "caption": caption})
        if len(nearby) >= max_count:
            break
    return nearby


def _build_hits(index: "_Index", order: list[int], combined: dict, coverage_of: dict, cosine_of: dict) -> list[dict]:
    hits: list[dict] = []
    for position in order:
        meta = index.docs[position].meta
        content = str(meta.get("content") or "")
        if len(content) > MAX_HIT_CHARS:
            content = content[:MAX_HIT_CHARS].rstrip() + "……"
        content = _expand_neighbors(index.docs, position, content)
        hit = {
            "chunk_id": meta.get("chunk_id"),
            "doc_id": meta.get("doc_id"),
            "doc_title": meta.get("doc_title"),
            "chapter": meta.get("chapter"),
            "page_start": meta.get("page_start"),
            "page_end": meta.get("page_end"),
            "kind": meta.get("kind"),
            "asset_id": meta.get("asset_id"),
            "content": content,
            "coverage": round(float(coverage_of.get(position, 0.0)), 3),
            "cosine": round(float(cosine_of.get(position, 0.0)), 3),
            "score": round(float(combined[position]), 3),
        }
        # 图段自己就是图，不用再标邻近；正文段标注同页的插图（最多 2 张），
        # 让模型看到「这段话旁边有图」，决定要不要在回答里展示。
        if str(meta.get("kind") or "") != "figure":
            hit["nearby_figures"] = _nearby_figures(index, meta)
        hits.append(hit)

    # 判「书里有没有」看两条：字面覆盖率（BM25）够高，**或者**语义相似度够高。
    # 只看字面会误杀同义改写（「算法跑得快不快」覆盖率 0.07 但语义命中）；
    # 只看语义会放过「书里有 Python、但没有 asyncio」这种半对的问题。
    top_coverage = hits[0]["coverage"] if hits else 0.0
    top_cosine = hits[0]["cosine"] if hits else 0.0
    confident = bool(hits) and (
        top_coverage >= _MIN_COVERAGE or top_cosine >= _VEC_CONFIDENT_COS
    )
    return {
        "hits": hits,
        "confident": confident,
        # 不 confident 时也要把 hits 带出去：模型可以据此说「书里只在某某处
        # 提过一句」，比直接说「没有」更有用。但 `confident=False` 是一个
        # 明确的信号，提示词要求它**不许**把弱命中包装成确定答案。
        "reason": "" if confident else "教材里没有明显对得上的段落。",
    }
