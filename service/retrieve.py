"""混合检索：dense(cosine) + sparse(BM25) → 加权融合 → 去重 → 可选精排。

设计取舍（见 research/13、14）：
- user_id 隔离 ⇒ 每次只对该用户的记忆做精确打分；热数据与 query-independent 索引有界缓存
- Search 只部分排序到 top_k 与去重/精排所需的 overfetch，避免全量排序
- 精排是唯一不受模型限制的环节，但只对短名单做（延迟预算）
- 并列必须全序 tie-break（score desc, created_at asc, id asc）⇒ 复现确定
"""
from __future__ import annotations

import heapq
import json
import calendar
import functools
import math
import os
import re
import threading
import time
import urllib.request
from array import array
from collections import Counter, OrderedDict

import dotenv

dotenv.load()
from embed import norm, unpack  # noqa: E402
import http_pool  # noqa: E402

try:  # numpy 是可选的加速路径；没有也完全能跑
    import numpy as _np
except Exception:  # pragma: no cover - 取决于运行环境
    _np = None

# math.sumprod (3.12+) is a C-level dot product; zip+generator is ~4x slower
_DOT = getattr(math, "sumprod", None) if _np is None else None

# ---------- 融合配置 ----------
# FUSION=linear（默认）：α·cos + β·bm25_norm，保留幅度信息，score 可当相关性阈值用
# FUSION=rrf            ：旧行为（只取名次），保留用于 A/B 对照
FUSION = os.environ.get("FUSION", "linear").strip().lower()
# 默认值来自 tools/tune.py 的实测（149 条标注 query，见 README §六）：
#   α=0.95/β=0.05 是「总分最高且没有任何 query 类型掉超过 0.02」的唯一候选点
DENSE_WEIGHT = float(os.environ.get("DENSE_WEIGHT", "0.95"))   # α
SPARSE_WEIGHT = float(os.environ.get("SPARSE_WEIGHT", "0.05"))  # β
RRF_K = int(os.environ.get("RRF_K", "60"))
# 稀疏侧校准：max=除以最大值（会饱和）；sat=饱和函数 s/(s+k·mean)（弱匹配保持弱）
SPARSE_NORM = os.environ.get("SPARSE_NORM", "sat").strip().lower()
# 只让「稀有词」（df/N ≤ 该值）参与 BM25：专有名词、证件号、型号等，避免「住/城市」这类高频字掀翻 dense
BM25_RARE_DF = float(os.environ.get("BM25_RARE_DF", "0.15"))
DEDUP_COS = float(os.environ.get("DEDUP_COS", "0.97"))  # 近重复阈值
# 多样性重排（MMR-lite）：多线索题（FR 列全、CD 对比、KR 更新链）要的是「覆盖不同事实」，
# 而同话题的多条记忆彼此余弦很高，会把 top-10 占满。λ 越大越偏向去重。
# 0 = 关闭（默认）；实测见 README §十七。
DIVERSITY = float(os.environ.get("DIVERSITY", "0"))
DIVERSITY_POOL = int(os.environ.get("DIVERSITY_POOL", "50"))
DEDUP_POOL = int(os.environ.get("DEDUP_POOL", "200"))   # 只对头部候选做去重（控制 O(n²)）
# 时态一致性规则：off / tense。查询里的时间词 + 记忆正文里的时间词做确定性微调，不依赖 LLM。
# 只调「近分平局」：权重远小于融合分差，纯 dense 的强赢家不会被掀翻。
TEMPORAL_RULE = os.environ.get("TEMPORAL_RULE", "tense").strip().lower()
TEMPORAL_WEIGHT = float(os.environ.get("TEMPORAL_WEIGHT", "0.06"))
_PRESENT_CUES = ("现在", "目前", "当前", "如今", "现阶段", "最新",
                 "currently", "now", "nowadays", "latest")
_PAST_CUES = ("以前", "之前", "过去", "原来", "曾经", "去年", "上次", "上一份", "前一份",
              "used to", "previously", "former", "last year")
_PAST_MARKERS = ("去年", "前年", "以前", "之前", "曾经", "原先", "原来", "早年", "当时",
                 "上一份", "前一份", "离职", "旧地址", "旧工位",
                 "used to", "previously", "formerly", "former", "last year", "in the past")
# 日期匹配：查询里写了具体日期（"Which image shown in 2024-06-17" / "the conversation of
# July 21"）时，把 created_at 落在同一天的记忆抬上来。只加分、不扣分——日期常是问题的
# 附注而不是过滤器（"on 2024-06-17, what did I say about X" 里答案未必那天写入）。
DATE_WEIGHT = float(os.environ.get("DATE_WEIGHT", "0"))   # 0 = 关闭
# 更新类问题（"latest / most recent / 现在 / 最新"）额外按写入时间抬新的：
# 时态规则只读正文措辞，读不到「后写的更权威」。同样只做小幅加成。
RECENCY_RULE = os.environ.get("RECENCY_RULE", "off").strip().lower()   # off | update
RECENCY_WEIGHT = float(os.environ.get("RECENCY_WEIGHT", "0.05"))
_UPDATE_CUES = ("latest", "most recent", "newest", "current", "currently", "now",
                "up to date", "as of now", "最新", "现在", "目前", "当前",
                "如今", "现阶段", "现在的工作", "现在住")
# 一条记忆常同时陈述旧事实与新事实（「去年在慕尼黑，现在住柏林」）：这种要中立，不能扣分
_PRESENT_MARKERS = ("现在", "目前", "当前", "如今", "现阶段", "今年",
                    "currently", "now ", "nowadays")

RERANK_MODE = os.environ.get("RERANK_MODE", "none").lower()
RERANK_KIND = os.environ.get("RERANK_KIND", "tei").lower()   # tei | dashscope
RERANK_URL = os.environ.get("RERANK_URL", "").rstrip("/")
# 默认 qwen3-rerank：gte-rerank-v2 官方标注 2026-05-30 下线，且实测分值是双峰的
# （24 条 query × 3 次：最大偏差 1.3e-3、48 次里 4 次顺序翻转）；qwen3-rerank 同样
# 条件下偏差 6.4e-8、0 次翻转，a/b 两半 MRR 与 gte 持平（0.791/0.798 vs 0.781/0.808）。
RERANK_MODEL = os.environ.get("RERANK_MODEL", "qwen3-rerank")
RERANK_TOP = int(os.environ.get("RERANK_TOP", "10"))   # 精排候选数：实测 10 与 30 质量相同，延迟 ÷3
# 精排正常 0.4 s：给 5 s 已经足够宽容，再长只是把尾部延迟留给 Search
RERANK_TIMEOUT = float(os.environ.get("RERANK_TIMEOUT", "5"))  # 单次精排超时（秒）
RERANK_API_KEY = os.environ.get("RERANK_API_KEY", "")
RERANK_FAILS = int(os.environ.get("RERANK_FAILS", "3"))      # 连续失败几次后熔断
RERANK_COOLDOWN = float(os.environ.get("RERANK_COOLDOWN", "60"))  # 熔断冷却秒数
RERANK_RETRIES = int(os.environ.get("RERANK_RETRIES", "2"))     # 单次精排的瞬时故障重试次数
# 精排侧并发：0.4 s/call × 16 ≈ 40 req/s，远高于实测总吞吐天花板（约 28/s），
# 再大开只会和 embedding 抢上游配额、换来 429。
CONCURRENCY_RERANK = int(os.environ.get("RERANK_POOL", "16"))   # 精排侧长连接上限
# 上游精排分**不是逐位可复现**的，而且是双峰的：同一 (query, documents) 反复调用，
# gte-rerank-v2 给出两套精确值（实测 0.104684 / 0.104543，差 1.4e-4），显然是两台
# 后端各算一遍。因此「量化」治不了——任何网格都会在边界上翻转。两手：
#   · RERANK_TOL：分差在此以内一律视为并列，改用融合分/时间/ID 决定顺序，于是两个
#     变体都落进同一个并列区，顺序与分数都不再随选中哪台后端而变；
#   · 响应里的 score 改为按**名次**映射回融合分区间（与上游分值大小无关），
#     这样字节只依赖最终顺序。
RERANK_TOL = float(os.environ.get("RERANK_TOL", "0.0003"))

# 精排健康度：旁路降级会改变排序，必须能在 /version 里看到发生了什么
_RERANK_STATS = {"calls": 0, "fails": 0, "degraded": 0, "cooldown_skips": 0}

# 熔断状态：精排是旁路，宕机时必须秒级降级，不能每个 query 都等 timeout
_rerank_fails = 0
_rerank_cooldown_until = 0.0


# ---------- 分词：英文按词，中文按字符 bigram ----------
_W = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
CJK_TOKENS = os.environ.get("CJK_TOKENS", "bigram_unigram").strip().lower()


def tokens(text: str) -> list[str]:
    """英文按词；中文按「连续汉字片段」切，可选单字 + bigram。

    CJK_TOKENS=bigram_unigram（默认）| bigram | unigram
    只切 bigram 时「下周几开会」与「会议安排在周五」零重叠 ⇒ BM25 全零，
    退化成按文档顺序打分；单字回退补上「会」这类共享字，零依赖且够用。
    """
    t = text.lower()
    out = _W.findall(t)
    for run in _CJK_RUN.findall(t):
        if CJK_TOKENS in ("unigram", "bigram_unigram"):
            out.extend(run)
        if CJK_TOKENS in ("bigram", "bigram_unigram"):
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


# ---------- BM25 ----------
_TOK_CACHE: dict[tuple[int, int], list[str]] = {}
_TOK_LOCK = threading.Lock()
_TOK_MAX = 100_000
_SEARCH_INDEX_MIN_DOCS = 32
_SEARCH_INDEX_MAX_ENTRIES = 16
_SEARCH_INDEX_MAX_BYTES = max(1, int(float(os.environ.get("RANK_INDEX_MAX_MB", "64")) * 1024 * 1024))
_SEARCH_INDEX: OrderedDict[object, "_SearchIndex"] = OrderedDict()
_SEARCH_INDEX_BYTES = 0
_SEARCH_INDEX_LOCK = threading.Lock()


def doc_tokens(d: dict) -> list[str]:
    """Cache tokens by text content, not by memory id.

    Memory bodies are immutable, so content-keyed entries stay valid; keying on
    `id` would go stale if an id is ever reused with different text (the tests
    do exactly that, and a rebuild could otherwise disagree with the fallback).
    """
    text = d["text"]
    key = (len(text), hash(text))
    with _TOK_LOCK:
        got = _TOK_CACHE.get(key)
    if got is not None:
        return got
    out = tokens(text)
    with _TOK_LOCK:
        if len(_TOK_CACHE) >= _TOK_MAX:
            _TOK_CACHE.clear()
        _TOK_CACHE[key] = out
    return out


def _docs_signature(docs: list[dict]) -> tuple:
    if not docs:
        return (0, CJK_TOKENS)
    return (len(docs), docs[0].get("id"), docs[0].get("seq"),
            docs[-1].get("id"), docs[-1].get("seq"), CJK_TOKENS)


class _SearchIndex:
    def __init__(self, signature, postings, doc_lengths, avgdl, matrix, dim,
                 estimated_bytes):
        self.signature = signature
        self.postings = postings
        self.doc_lengths = doc_lengths
        self.avgdl = avgdl
        self.matrix = matrix      # numpy fast path (None without numpy)
        self.dim = dim            # vector width shared by matrix / rows
        self.rows = None          # decoded rows for the pure-Python fast path
        self.estimated_bytes = estimated_bytes
        # anchors for append-only growth (Add never rewrites an existing memory)
        self.count = len(doc_lengths) if doc_lengths is not None else signature[0]
        self.total_terms = 0
        self.posting_bytes = 0
        self.first_key = None
        self.last_key = None


def _uniform_dim(docs: list[dict]) -> int:
    """Vector width when float32 blobs are uniform and decodable, else 0."""
    if not docs:
        return 0
    first = docs[0]["vec"]
    if not first or len(first) % 4:
        return 0
    if any(len(d["vec"]) != len(first) for d in docs):
        return 0
    return len(first) // 4


def _matrix_size(docs: list[dict]) -> int:
    """Bytes of the numpy matrix, or of decoded rows when numpy is unavailable."""
    dim = _uniform_dim(docs)
    if not dim:
        return 0
    if _np is not None and len(docs) >= _SEARCH_INDEX_MIN_DOCS:
        return len(docs) * dim * 4
    if _np is None and _DOT is not None:
        return len(docs) * dim * 4
    return 0


def _estimate_search_index_bytes(docs: list[dict]) -> int:
    # 256 B/doc bookkeeping + 96 B row overhead, matching _build_search_index.
    return _matrix_size(docs) + len(docs) * 352


def _doc_key(d: dict) -> tuple:
    return (d.get("id"), d.get("seq"))


def _append_postings(postings, doc_lengths, docs, start: int, budget: int):
    """Append docs[start:] to postings/doc_lengths; return (bytes_used, total_terms) or None."""
    used = 0
    total_terms = 0
    posting_bytes = 2 * array("I").itemsize + 2  # include array growth slack
    for i in range(start, len(docs)):
        tf = Counter(doc_tokens(docs[i]))
        doc_length = sum(tf.values()) or 1
        doc_lengths.append(doc_length)
        total_terms += doc_length
        for term, count in tf.items():
            posting = postings.get(term)
            extra = posting_bytes + (260 + len(term) * 4 if posting is None else 0)
            if used + extra > budget:
                return None
            if posting is None:
                posting = (array("I"), array("I"))
                postings[term] = posting
            posting[0].append(i)
            posting[1].append(count)
            used += extra
    return used, total_terms


def _build_search_index(docs: list[dict]) -> _SearchIndex | None:
    signature = _docs_signature(docs)
    dim = _uniform_dim(docs)
    matrix = None
    if dim and len(docs) >= _SEARCH_INDEX_MIN_DOCS and _np is not None:
        blobs = [d["vec"] for d in docs]
        try:
            matrix = _np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), dim)
        except Exception:  # malformed vectors remain on the exact fallback path
            matrix = None

    # Without numpy, keep decoded float32 rows so dense scoring avoids unpack+zip per query.
    rows = None
    if matrix is None and dim and _DOT is not None:
        try:
            rows = [array("f", unpack(d["vec"])) for d in docs]
        except Exception:  # pragma: no cover - defensive; blobs were already width-checked
            rows = None

    vector_bytes = 0
    if matrix is not None:
        vector_bytes = matrix.nbytes
    elif rows is not None:
        vector_bytes = len(rows) * dim * 4 + len(rows) * 96

    base_bytes = len(docs) * 256 + vector_bytes
    if base_bytes > _SEARCH_INDEX_MAX_BYTES:
        return None

    postings: dict[str, tuple[array, array]] = {}
    doc_lengths = array("I")
    appended = _append_postings(postings, doc_lengths, docs, 0,
                                _SEARCH_INDEX_MAX_BYTES - base_bytes)
    if appended is None:
        # Keep the vector matrix/rows even when sparse postings exceed their budget.
        partial = _SearchIndex(signature, None, None, 1.0, matrix, dim, base_bytes)
        partial.rows = rows
        partial.first_key = _doc_key(docs[0])
        partial.last_key = _doc_key(docs[-1])
        partial.count = len(docs)
        return partial
    posting_bytes, total_terms = appended

    avgdl = total_terms / len(doc_lengths) if doc_lengths else 1.0
    index = _SearchIndex(signature, postings, doc_lengths, avgdl, matrix, dim,
                         base_bytes + posting_bytes)
    index.rows = rows
    index.total_terms = total_terms
    index.posting_bytes = posting_bytes
    index.first_key = _doc_key(docs[0])
    index.last_key = _doc_key(docs[-1])
    index.count = len(docs)
    return index


def _extend_search_index(index: _SearchIndex, docs: list[dict]) -> _SearchIndex | None:
    """Grow an index for an Add that only appended memories.

    Add never rewrites existing rows, so an old index is reusable whenever its
    document list is still a prefix of the new one. Copy-on-write keeps readers
    of the old index valid while the extension is built.
    """
    start = index.count
    if index.first_key is None or start <= 0 or start >= len(docs):
        return None
    if _doc_key(docs[0]) != index.first_key or _doc_key(docs[start - 1]) != index.last_key:
        return None
    if index.signature[-1] != CJK_TOKENS:  # tokenizer changed ⇒ the old postings are invalid
        return None

    dim = _uniform_dim(docs[start:])
    matrix, rows, vector_bytes = None, None, 0
    if index.matrix is not None:
        if not dim or dim != index.dim or _np is None:
            return None
        try:
            extra = _np.frombuffer(b"".join(d["vec"] for d in docs[start:]),
                                   dtype="<f4").reshape(len(docs) - start, dim)
            matrix = _np.vstack([index.matrix, extra])
        except Exception:
            return None
        vector_bytes = matrix.nbytes
    elif index.rows is not None:
        if not dim or dim != index.dim or _DOT is None:
            return None
        try:
            rows = list(index.rows) + [array("f", unpack(d["vec"])) for d in docs[start:]]
        except Exception:  # pragma: no cover - defensive
            return None
        vector_bytes = len(rows) * dim * 4 + len(rows) * 96

    base_bytes = len(docs) * 256 + vector_bytes
    if base_bytes > _SEARCH_INDEX_MAX_BYTES:
        return None

    postings, doc_lengths, posting_bytes, total_terms = None, None, 0, index.total_terms
    if index.postings is not None:
        # Only touched terms need copying; untouched posting lists stay shared.
        postings = dict(index.postings)
        doc_lengths = array("I", index.doc_lengths)
        touched = set()
        for doc in docs[start:]:
            touched.update(Counter(doc_tokens(doc)))
        for term in touched:
            old = postings.get(term)
            postings[term] = ((array("I", old[0]), array("I", old[1]))
                              if old is not None else (array("I"), array("I")))
        appended = _append_postings(postings, doc_lengths, docs, start,
                                    _SEARCH_INDEX_MAX_BYTES - base_bytes - index.posting_bytes)
        if appended is None:
            return None
        posting_bytes, extra_terms = appended
        posting_bytes += index.posting_bytes
        total_terms += extra_terms

    avgdl = total_terms / len(docs) if total_terms else 1.0
    extended = _SearchIndex(_docs_signature(docs), postings, doc_lengths, avgdl, matrix,
                            dim or index.dim, base_bytes + posting_bytes)
    extended.rows = rows
    extended.total_terms = total_terms
    extended.posting_bytes = posting_bytes
    extended.first_key = index.first_key
    extended.last_key = _doc_key(docs[-1])
    extended.count = len(docs)
    return extended


def _get_search_index(cache_key, docs: list[dict], stats: dict | None = None):
    global _SEARCH_INDEX_BYTES
    if cache_key is None or len(docs) < _SEARCH_INDEX_MIN_DOCS:
        if stats is not None:
            stats["index_cache_hit"] = False
        return None

    signature = _docs_signature(docs)
    cached = None
    with _SEARCH_INDEX_LOCK:
        cached = _SEARCH_INDEX.get(cache_key)
        if cached is not None and cached.signature == signature:
            _SEARCH_INDEX.move_to_end(cache_key)
            if stats is not None:
                stats["index_cache_hit"] = True
            return cached
    if cached is not None:
        extended = _extend_search_index(cached, docs)
        if extended is not None and extended.estimated_bytes <= _SEARCH_INDEX_MAX_BYTES:
            with _SEARCH_INDEX_LOCK:
                current = _SEARCH_INDEX.get(cache_key)
                if current is cached:
                    _SEARCH_INDEX_BYTES += extended.estimated_bytes - cached.estimated_bytes
                    _SEARCH_INDEX[cache_key] = extended
                    _SEARCH_INDEX.move_to_end(cache_key)
                    if stats is not None:
                        stats["index_cache_hit"] = False
                        stats["index_extended"] = True
                    return extended
                if current is not None and current.signature == signature:
                    return current
        with _SEARCH_INDEX_LOCK:
            if _SEARCH_INDEX.get(cache_key) is cached:
                _SEARCH_INDEX_BYTES -= cached.estimated_bytes
                del _SEARCH_INDEX[cache_key]

    if _estimate_search_index_bytes(docs) > _SEARCH_INDEX_MAX_BYTES:
        if stats is not None:
            stats["index_cache_hit"] = False
        return None

    index = _build_search_index(docs)
    if stats is not None:
        stats["index_cache_hit"] = False
    if index is None or index.estimated_bytes > _SEARCH_INDEX_MAX_BYTES:
        return None

    with _SEARCH_INDEX_LOCK:
        current = _SEARCH_INDEX.get(cache_key)
        if current is not None and current.signature == signature:
            _SEARCH_INDEX.move_to_end(cache_key)
            return current
        if current is not None:
            _SEARCH_INDEX_BYTES -= current.estimated_bytes
            del _SEARCH_INDEX[cache_key]
        while _SEARCH_INDEX and (
            len(_SEARCH_INDEX) >= _SEARCH_INDEX_MAX_ENTRIES
            or _SEARCH_INDEX_BYTES + index.estimated_bytes > _SEARCH_INDEX_MAX_BYTES
        ):
            _, evicted = _SEARCH_INDEX.popitem(last=False)
            _SEARCH_INDEX_BYTES -= evicted.estimated_bytes
        _SEARCH_INDEX[cache_key] = index
        _SEARCH_INDEX_BYTES += index.estimated_bytes
    return index


def bm25_scores(query: str, docs: list[dict], index=None) -> list[float]:
    if not docs:
        return []
    q = tokens(query)
    if not q:
        return [0.0] * len(docs)
    n = len(docs)
    k1, b = 1.5, 0.75

    if index is not None and index.postings is not None:
        postings = index.postings
        rare = []
        if BM25_RARE_DF > 0:
            rare = [t for t in q
                    if 0 < len(postings.get(t, ((), ()))[0]) / n <= BM25_RARE_DF]
            q = rare or q
        idfs = {}
        for term in q:
            posting = postings.get(term)
            if posting:
                df = len(posting[0])
                idfs[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        out = [0.0] * n
        for term in q:
            posting = postings.get(term)
            if not posting:
                continue
            idf = idfs[term]
            for i, count in zip(posting[0], posting[1]):
                dl = index.doc_lengths[i]
                out[i] += idf * (count * (k1 + 1)) / (
                    count + k1 * (1 - b + b * dl / index.avgdl)
                )
        return out

    doc_tf = [Counter(doc_tokens(d)) for d in docs]
    dfs = Counter()
    for tf in doc_tf:
        dfs.update(tf.keys())
    avgdl = sum(sum(tf.values()) for tf in doc_tf) / n or 1.0
    if BM25_RARE_DF > 0:
        rare = [t for t in q if 0 < dfs[t] / n <= BM25_RARE_DF]
        q = rare or q
    out = []
    for tf in doc_tf:
        dl = sum(tf.values()) or 1
        score = 0.0
        for term in q:
            count = tf.get(term, 0)
            if not count:
                continue
            idf = math.log(1 + (n - dfs[term] + 0.5) / (dfs[term] + 0.5))
            score += idf * (count * (k1 + 1)) / (count + k1 * (1 - b + b * dl / avgdl))
        out.append(score)
    return out


def cosine(qv: list[float], dv: list[float]) -> float:
    if len(qv) != len(dv):
        return 0.0
    if _DOT is not None:
        return _DOT(qv, dv)
    return sum(a * b for a, b in zip(qv, dv))


# ---------- 融合 ----------
def _rank(scores: list[float]) -> list[int]:
    idx = list(range(len(scores)))
    idx.sort(key=lambda i: (-scores[i], i))  # 全序
    return idx


# 所有 dense 路径统一「float32 存储 × float64 累加」：
# 旧实现 numpy 走 float32 matmul、纯 Python 走 float64，二者差 ~1e-7，
# 同一 query 在索引缓存命中/淘汰时 score 会变、并列时顺序也可能变 ⇒ 破坏字节级复现。
# einsum(dtype=float64) 与 math.sumprod 的差在 1e-16 量级，且速度与 float32 matmul 相当。
_SCORE_DIGITS = 12  # 再裁掉累加顺序带来的最后几位噪声


def _np_dense(mat, qvec) -> list[float]:
    q = _np.asarray(qvec, dtype=_np.float64)
    out = _np.einsum("ij,j->i", mat, q, dtype=_np.float64, casting="unsafe")
    return _np.round(out, _SCORE_DIGITS).tolist()


def dense_scores(qvec: list[float], docs: list[dict], index=None) -> list[float]:
    dim = len(qvec)
    if index is not None and index.matrix is not None and index.matrix.shape[1] == dim:
        try:
            return _np_dense(index.matrix, qvec)
        except Exception:  # fall through to the exact compatibility path
            pass
    if _np is not None and len(docs) >= 128:
        try:
            blobs = [d["vec"] for d in docs]
            if all(len(b) == dim * 4 for b in blobs):
                mat = _np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), dim)
                return _np_dense(mat, qvec)
        except Exception:  # 回退到纯 Python，检索绝不因加速失败而中断
            pass
    if index is not None and index.rows is not None and index.dim == dim:
        try:
            q = [float(x) for x in qvec]
            return [round(_DOT(q, row), _SCORE_DIGITS) for row in index.rows]
        except Exception:  # pragma: no cover - defensive; decoded rows are width-checked
            pass
    return [round(cosine(qvec, unpack(d["vec"])), _SCORE_DIGITS) for d in docs]


def _order(scores: list[float], docs: list[dict], limit: int | None = None) -> list[int]:
    """全序 tie-break：score desc → created_at desc → id asc（契约要求复现确定）。

    created_at 取降序：平台 Answer 侧遇到冲突时偏「更新的记忆」，而平局最常出现在
    「同一事实重述两次」（旧值在后来的会话里被再确认），升序会把过期拷贝排在前面。
    """
    def _key(i: int) -> tuple[str, str]:
        return (docs[i].get("id", ""), docs[i].get("created_at", ""))

    def _sorted(indices: list[int]) -> list[int]:
        # 稳定排序链：先 id 升序，再 created_at 降序，最后 score 降序
        indices.sort(key=lambda i: _key(i)[0])
        indices.sort(key=lambda i: _key(i)[1], reverse=True)
        indices.sort(key=lambda i: -scores[i])
        return indices

    n = len(scores)
    if limit is not None and 0 <= limit < n:
        if limit == 0:
            return []
        # Head selection by score only is cheap; ties at the cutoff still need the
        # full tie-break, so every item scoring >= cutoff becomes a candidate.
        head = heapq.nsmallest(limit, range(n), key=lambda i: -scores[i])
        cutoff = scores[head[-1]]
        chosen = set(head)
        candidates = head + [i for i, s in enumerate(scores) if s == cutoff and i not in chosen]
        return _sorted(candidates)[:limit]
    return _sorted(list(range(n)))


def bm25_norm(sparse: list[float], mode: str | None = None) -> list[float] | None:
    """把 BM25 压到 0–1，并让「零命中」这件事可表达。

    返回 None 表示本 query 对所有文档都零命中（无词面证据）：
    此时稀疏侧整体弃权，只由 dense 决定顺序 —— 这修掉了旧实现里
    `_rank()` 在全零时按插入顺序打分、最早写入的分块白拿名次分的缺陷。
    """
    mode = (mode or SPARSE_NORM or "sat").lower()
    mx = 0.0
    pos_sum = 0.0
    pos_n = 0
    for s in sparse:
        if s > mx:
            mx = s
        if s > 0.0:
            pos_sum += s
            pos_n += 1
    if mx <= 0.0:
        return None
    if mode == "max":
        return [s / mx if s > 0.0 else 0.0 for s in sparse]
    # sat：s/(s+k)，k 取正分均值 ⇒ 弱匹配停在低区间，不会与强 dense 争夺名额
    k = (pos_sum / pos_n) if pos_n else 1.0
    return [s / (s + k) if s > 0.0 else 0.0 for s in sparse]


def fuse_linear(dense: list[float], sparse: list[float]) -> list[float]:
    """α·cos⁺ + β·bm25_norm，按有效权重归一 ⇒ score ∈ [0,1] 且可做阈值。"""
    bn = bm25_norm(sparse)
    a = max(0.0, DENSE_WEIGHT)
    b = max(0.0, SPARSE_WEIGHT) if bn is not None else 0.0
    if a + b <= 0.0:
        a, b = 0.5, 0.5 if bn is not None else 0.0
    total = a + b or 1.0
    inv = 1.0 / total
    if bn is None:
        return [(a * dv * inv if dv > 0.0 else 0.0) if a else 0.0 for dv in dense]
    out = []
    for dv, sv in zip(dense, bn):
        s = a * dv if dv > 0.0 else 0.0
        out.append((s + b * sv) * inv)
    return out


def fuse_rrf(dense: list[float], sparse: list[float]) -> list[float]:
    """旧行为：纯名次融合（保留用于 A/B 对照，不参与默认路径）。"""
    rd = _rank(dense)
    rrf = [0.0] * len(dense)
    for pos, i in enumerate(rd):
        rrf[i] += DENSE_WEIGHT / (RRF_K + pos + 1)
    if bm25_norm(sparse) is not None:  # 零命中时稀疏侧同样不该投票
        for pos, i in enumerate(_rank(sparse)):
            rrf[i] += SPARSE_WEIGHT / (RRF_K + pos + 1)
    return rrf


def fused_scores(qvec: list[float], query_text: str, docs: list[dict],
                 index=None, stats: dict | None = None) -> list[float]:
    """融合分 + 时态微调。`hybrid()` 与线上 `run()` 共用这一份，避免评测与服务两套逻辑。"""
    dense = dense_scores(qvec, docs, index)
    sparse = bm25_scores(query_text, docs, index)
    fused = fuse_rrf(dense, sparse) if FUSION == "rrf" else fuse_linear(dense, sparse)
    if TEMPORAL_RULE == "tense":
        tense = query_tense(query_text)
        bias = tense_bias(docs, tense)
        if bias is not None:
            fused = [min(1.0, max(0.0, s + b)) for s, b in zip(fused, bias)]
            if stats is not None:
                stats["temporal"] = tense
    if DATE_WEIGHT > 0:                      # 问题里写了具体日期 → 那天写入的抬上来
        bias = date_bias(docs, query_text)
        if bias is not None:
            fused = [min(1.0, max(0.0, s + b)) for s, b in zip(fused, bias)]
            if stats is not None:
                stats["date_matched"] = sum(1 for b in bias if b > 0)
    if RECENCY_RULE == "update":             # 更新类问题 → 后写的更权威
        bias = recency_bias(docs, query_text)
        if bias is not None:
            fused = [min(1.0, max(0.0, s + b)) for s, b in zip(fused, bias)]
            if stats is not None:
                stats["recency"] = True
    return [round(s, _SCORE_DIGITS) for s in fused]


def hybrid(qvec: list[float], query_text: str, docs: list[dict],
           limit: int | None = None, cache_key=None,
           stats: dict | None = None) -> list[tuple[int, float]]:
    index = _get_search_index(cache_key, docs, stats)
    fused = fused_scores(qvec, query_text, docs, index, stats)
    return [(i, fused[i]) for i in _order(fused, docs, limit)]


# ---------- 时态一致性：查询问「现在」就压旧事实，问「以前」就抬旧事实 ----------
# 英文线索必须按词边界匹配：子串匹配会把 know/snowing→now、performer→former、
# acknowledge→now 误判（Mem-Gallery 1,711 条英文问题里大量误触发）。中文无词边界，仍按子串。
def _cue_matcher(cues: tuple[str, ...]):
    ascii_cues = [c.strip() for c in cues if c.strip().isascii()]
    cjk = tuple(c for c in cues if not c.strip().isascii())
    rx = (re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(c) for c in ascii_cues)
                     + r")(?![a-z0-9])")
          if ascii_cues else None)

    def match(low: str) -> bool:
        return any(c in low for c in cjk) or (rx is not None and rx.search(low) is not None)
    return match


_has_present_cue = _cue_matcher(_PRESENT_CUES)
_has_past_cue = _cue_matcher(_PAST_CUES)
_has_past_marker = _cue_matcher(_PAST_MARKERS)
_has_present_marker = _cue_matcher(_PRESENT_MARKERS)


def query_tense(text: str) -> str | None:
    """返回 "present" / "past" / None。同时含两个时态时视为不确定。"""
    low = text.lower()
    present = _has_present_cue(low)
    past = _has_past_cue(low)
    if present == past:
        return None
    return "present" if present else "past"


_TENSE_CACHE: dict[tuple[int, int], str] = {}
_TENSE_LOCK = threading.Lock()


def doc_tense_state(d: dict) -> str:
    """记忆正文只陈述旧事实时为 "past"；新旧混合或纯现时陈述时为 "neutral"。

    按正文缓存（正文不可变）。混合陈述必须中立：它往往正是含答案的那一条。
    """
    text = d["text"]
    key = (len(text), hash(text))
    with _TENSE_LOCK:
        got = _TENSE_CACHE.get(key)
    if got is not None:
        return got
    low = text.lower()
    has_past = _has_past_marker(low)
    has_present = _has_present_marker(low)
    state = "past" if has_past and not has_present else "neutral"
    with _TENSE_LOCK:
        if len(_TENSE_CACHE) >= _TOK_MAX:
            _TENSE_CACHE.clear()
        _TENSE_CACHE[key] = state
    return state


def tense_bias(docs: list[dict], tense: str | None,
               weight: float | None = None) -> list[float] | None:
    """现时提问 ⇒ 带旧时标记的记忆扣分；过去时提问 ⇒ 加分。

    只看正文措辞，不看写入时间：同一会话里「去年住在慕尼黑」可能几分钟前才写，
    时间戳反映的是写入顺序而不是事实有效期。没有命中任何旧时标记时整体不启用。
    """
    if tense not in ("present", "past") or len(docs) < 2:
        return None
    w = TEMPORAL_WEIGHT if weight is None else weight
    if w <= 0:
        return None
    past = [doc_tense_state(d) == "past" for d in docs]
    if not any(past):
        return None
    if tense == "present":
        return [-w if f else 0.0 for f in past]
    return [w if f else 0.0 for f in past]


# ---------- 日期匹配 / 更新类新鲜度 ----------
_MONTHS = {m: i for i, m in enumerate(
    ("january february march april may june july august september october november december"
     ).split(), 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MON = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_DATE_RXS = (
    re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})/(\d{1,2})/(\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日"),
)
_MONTH_DAY_RX = re.compile(r"(?<![a-z])(" + _MON + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
                           r"(?:,?\s*(\d{4}))?(?![\d])", re.I)
_DAY_MONTH_RX = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MON + r")\.?"
                           r"(?:,?\s*(\d{4}))?", re.I)
_MONTH_YEAR_RX = re.compile(r"(?<![a-z])(" + _MON + r")\.?,?\s+(\d{4})(?!\d)", re.I)


def query_dates(text: str) -> tuple[set[tuple[int, int, int]], set[tuple[int, int]]]:
    """抽出查询里的显式日期。返回 (精确到日, 精确到月)。

    只认「年-月-日」和「月 日 [年]」两种：只给「月 日」时按 (0, m, d) 处理，
    与任何年份的该天匹配——问题里常有 "on June 17" 这种省略年份的写法。
    """
    if not text:
        return set(), set()
    days: set[tuple[int, int, int]] = set()
    months: set[tuple[int, int]] = set()
    for rx in _DATE_RXS:
        for y, m, d in rx.findall(text):
            if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
                days.add((int(y), int(m), int(d)))
    for mon, d, y in _MONTH_DAY_RX.findall(text):
        m = _MONTHS.get(mon.lower())
        if m and 1 <= int(d) <= 31:
            days.add((int(y) if y else 0, m, int(d)))
    for d, mon, y in _DAY_MONTH_RX.findall(text):
        m = _MONTHS.get(mon.lower())
        if m and 1 <= int(d) <= 31:
            days.add((int(y) if y else 0, m, int(d)))
    for mon, y in _MONTH_YEAR_RX.findall(text):
        m = _MONTHS.get(mon.lower())
        if m:
            months.add((int(y), m))
    return days, months


def _doc_date(created_at: str) -> tuple[int, int, int]:
    try:
        return int(created_at[0:4]), int(created_at[5:7]), int(created_at[8:10])
    except Exception:  # noqa: BLE001 — 时间戳形状异常时不参与匹配
        return (0, 0, 0)


def date_bias(docs: list[dict], query_text: str, weight: float | None = None) -> list[float] | None:
    """命中所问日期的记忆加分（同月给 1/3）。只加分，不惩罚。"""
    w = DATE_WEIGHT if weight is None else weight
    if w <= 0 or len(docs) < 2:
        return None
    days, months = query_dates(query_text)
    if not days and not months:
        return None
    bias = []
    hit = False
    for d in docs:
        y, m, dd = _doc_date(d.get("created_at", ""))
        add = 0.0
        for qy, qm, qd in days:
            if qm == m and qd == dd and (qy == 0 or qy == y):
                add = w
                break
        if add == 0.0 and (y, m) in months:
            add = w / 3.0
        bias.append(add)
        hit = hit or add > 0.0
    return bias if hit else None


_has_update_cue = _cue_matcher(_UPDATE_CUES)


def recency_bias(docs: list[dict], query_text: str, weight: float | None = None) -> list[float] | None:
    """更新类问题：按写入时间线性抬新压旧（最老 −w、最新 +w）。"""
    w = RECENCY_WEIGHT if weight is None else weight
    if RECENCY_RULE != "update" or w <= 0 or len(docs) < 3:
        return None
    if not _has_update_cue((query_text or "").lower()):
        return None
    stamps = []
    for d in docs:
        try:
            stamps.append(calendar.timegm(time.strptime(d["created_at"][:19], "%Y-%m-%dT%H:%M:%S")))
        except Exception:  # noqa: BLE001
            stamps.append(0)
    lo, hi = min(stamps), max(stamps)
    if hi <= lo:
        return None
    span = float(hi - lo)
    out = []
    for t in stamps:
        out.append(w * (2.0 * (t - lo) / span - 1.0))
    return out


# ---------- 近重复去重（同一事实的多副本只留一条）----------
def _norm_text(s: str) -> str:
    return "".join(s.lower().split())[:400]


def dedup(ranked: list[tuple[int, float]], docs: list[dict], index=None,
          want: int | None = None) -> list[tuple[int, float]]:
    """头部候选去重：先比正文指纹，再比向量；尾部原样保留。

    只对前 DEDUP_POOL 条做向量比较，避免 O(n²) 把延迟拖垮；
    精排只处理 RERANK_TOP 条，因此头部去重已覆盖实际收益区间。
    向量优先复用检索索引里已解码的矩阵/行，避免每次 Search 重新 unpack。
    `want` 给出时只贪心到够用为止（Search 只要前 top_k 条，不值得把池子比完）。
    """
    pool, tail = ranked[:DEDUP_POOL], ranked[DEDUP_POOL:]
    if not pool:
        return ranked
    pool_rows = [i for i, _ in pool]
    matrix = None
    rows = None
    if index is not None and pool_rows:
        n_rows = max(pool_rows) + 1
        if index.matrix is not None and n_rows <= index.matrix.shape[0]:
            matrix = index.matrix
        elif index.rows is not None and n_rows <= len(index.rows):
            rows = index.rows
    vecs = None if (matrix is not None or rows is not None) else [
        unpack(docs[i]["vec"]) for i, _ in pool]
    gram = None
    if matrix is not None:
        try:
            pool_matrix = matrix[pool_rows].astype(_np.float64)  # 与纯 Python 路径同精度
            gram = pool_matrix @ pool_matrix.T
        except Exception:  # pragma: no cover - defensive; fall back to decoded rows
            gram = None

    seen: set[str] = set()
    out: list[tuple[int, float]] = []
    kept: list[int] = []
    sigs = [docs[i].get("img_sig") or "" for i, _ in pool]
    for pos, (i, sc) in enumerate(pool):
        # 图片不同的记忆不是重复：两张不同的图即使 caption/正文相同（甚至都是
        # "(image only)"），也必须各自保留，否则图集式检索（Mem-Gallery）会丢证据。
        key = _norm_text(docs[i]["text"]) + "\x00" + sigs[pos]
        if key in seen:
            continue
        window = [j for j in kept[-40:] if sigs[j] == sigs[pos]]
        if gram is not None:
            if window and float(gram[pos][window].max()) > DEDUP_COS:
                continue
        elif matrix is not None:
            if window:
                sims = (matrix[[pool_rows[j] for j in window]].astype(_np.float64)
                        @ matrix[pool_rows[pos]].astype(_np.float64))
                if float(sims.max()) > DEDUP_COS:
                    continue
        elif rows is not None:
            if window:
                row = rows[pool_rows[pos]]
                if any(cosine(row, rows[pool_rows[j]]) > DEDUP_COS for j in window):
                    continue
        elif window:
            v = vecs[pos]
            if any(cosine(v, vecs[j]) > DEDUP_COS for j in window):
                continue
        seen.add(key)
        kept.append(pos)
        out.append((i, sc))
        if want is not None and len(out) >= want:
            return out
    return out + tail


# ---------- 可选精排（唯一不受模型限制的环节）----------
def _rebased(head: list[tuple[int, float]], scored: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """把精排分单调映射回「融合分区间」，避免两种分的量纲混在一起。

    精排分（sigmoid 后的 cross-encoder 分）与融合分不可比：直接拼接会出现
    「尾部融合分 > 头部精排分」的倒挂，`score` 也就不再单调可解释。
    这里把头部按**名次**等距压到 [min(head), max(head)]（不看上游分值大小）：
      · 头部内部顺序 = 精排顺序（保序）
      · 头部任何一条都 ≥ 尾部首条（融合分递减）
      · 仍在 [0,1] 内，可当阈值用且逐位可复现
    """
    if not scored:
        return head
    fused = [s for _, s in head]
    hi, lo = max(fused), min(fused)
    n = len(scored)
    if n == 1:
        return [(scored[0][0], hi)]
    # 按名次等距映射，不看上游分值大小：上游同一请求会在两套精确值之间跳
    # （见 RERANK_TOL 注释），按分值映射会把那份抖动原样搬进响应。
    step = (hi - lo) / (n - 1)
    return [(i, hi - k * step) for k, (i, _) in enumerate(scored)]


def rerank(query_text: str, ranked: list[tuple[int, float]], docs: list[dict]) -> list[tuple[int, float]]:
    global _rerank_fails, _rerank_cooldown_until
    if RERANK_MODE != "http" or not RERANK_URL or RERANK_TOP <= 0:
        return ranked
    if time.time() < _rerank_cooldown_until:
        _RERANK_STATS["cooldown_skips"] += 1
        return ranked  # 熔断中：旁路失效不影响 Search
    _RERANK_STATS["calls"] += 1
    head = ranked[:RERANK_TOP]
    tail = ranked[RERANK_TOP:]
    payload = {
        "query": query_text,
        "documents": [docs[i]["text"][:4000] for i, _ in head],
        "top_n": len(head),
    }
    if RERANK_MODEL:
        payload["model"] = RERANK_MODEL
    if RERANK_KIND == "dashscope":
        # DashScope text-rerank 契约：input/parameters 包一层
        payload = {
            "model": RERANK_MODEL or "gte-rerank-v2",
            "input": {"query": query_text,
                      "documents": payload["documents"]},
            "parameters": {"return_documents": False},
        }
    headers = {"Content-Type": "application/json",
               **({"Authorization": f"Bearer {RERANK_API_KEY}"} if RERANK_API_KEY else {})}
    try:
        resp = None
        for attempt in range(max(1, RERANK_RETRIES + 1)):
            try:
                raw = http_pool.post(RERANK_URL, json.dumps(payload, ensure_ascii=False).encode(),
                                     headers, RERANK_TIMEOUT, size=CONCURRENCY_RERANK)
                resp = json.loads(raw.decode("utf-8"))
                break
            except http_pool.HTTPStatusError as e:
                # 429 / 5xx 是一次抖动；4xx（鉴权、参数）重试无意义，直接走降级
                if e.status < 500 and e.status != 429:
                    raise
                if attempt >= RERANK_RETRIES:
                    raise
                time.sleep(0.2 * (2 ** attempt))
            except Exception:
                # 超时、IncompleteRead、半截 JSON：重试比降级便宜——
                # 降级会改变排序，破坏「同一 query 重放结果一致」。
                if attempt >= RERANK_RETRIES:
                    raise
                time.sleep(0.2 * (2 ** attempt))
        if isinstance(resp, dict) and isinstance(resp.get("output"), dict):
            rows = resp["output"].get("results", resp)
        else:
            rows = resp.get("results", resp) if isinstance(resp, dict) else resp
        scored = []
        seen = set()
        if not isinstance(rows, list):
            raise ValueError("rerank results must be a list")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("rerank result row must be an object")
            raw_index = row.get("index", row.get("corpus_id"))
            if isinstance(raw_index, bool) or raw_index is None:
                raise ValueError("rerank result has invalid index")
            j = int(raw_index)
            if not 0 <= j < len(head) or j in seen:
                raise ValueError("rerank result indices must be unique and in range")
            s = float(row.get("relevance_score", row.get("score", 0.0)))
            if not math.isfinite(s):
                raise ValueError("rerank result score must be finite")
            seen.add(j)
            scored.append((head[j][0], s))
        if len(seen) != len(head):
            raise ValueError(f"rerank returned {len(seen)}/{len(head)} unique results")
        fused = {i: s for i, s in head}
        scored.sort(key=lambda t: t[0])                                     # 4th: id 升序
        scored.sort(key=lambda t: docs[t[0]]["created_at"], reverse=True)    # 3rd: 新的在前
        scored.sort(key=lambda t: -fused[t[0]])                             # 2nd: 融合分降序
        # 1st: 精排分降序，但容差内算并列（cmp_to_key 让「并列」变成真正的等价类，
        # 顺序完全由上面几轮确定性的键决定）
        scored.sort(key=functools.cmp_to_key(
            lambda a, b: 0 if abs(a[1] - b[1]) <= RERANK_TOL else (-1 if a[1] > b[1] else 1)))
        _rerank_fails = 0
        return _rebased(head, scored) + tail
    except Exception:
        # 精排是旁路：失败降级为融合序，绝不阻断 Search；失败多了就熔断一段时间
        _RERANK_STATS["fails"] += 1
        _rerank_fails += 1
        if _rerank_fails >= RERANK_FAILS:
            _rerank_fails = 0
            _rerank_cooldown_until = time.time() + RERANK_COOLDOWN
        return ranked


def diversify(ranked: list[tuple[int, float]], docs: list[dict], index=None,
              want: int | None = None, lam: float | None = None) -> list[tuple[int, float]]:
    """MMR-lite：在头部池里贪心挑「分数高且与已选项不相似」的，尾部原样接上。

    只重排，不改分：输出仍是 (index, score)，score 用来排序/展示，避免影响可复现契约。
    """
    lam = DIVERSITY if lam is None else lam
    if lam <= 0 or len(ranked) < 3:
        return ranked
    pool = ranked[:max(3, DIVERSITY_POOL)]
    tail = ranked[len(pool):]
    rows = _vec_rows([i for i, _ in pool], docs, index)
    if rows is None:
        return ranked
    n = len(pool)
    sims = [[0.0] * n for _ in range(n)]
    for a in range(n):
        va = rows[a]
        for b in range(a + 1, n):
            v = float(_np.dot(va, rows[b])) if _np is not None else cosine(va, rows[b])
            sims[a][b] = sims[b][a] = v
    order = list(range(n))
    order.sort(key=lambda i: -pool[i][1])       # 确定性起点：分数降序
    chosen = [order[0]]
    left = set(order[1:])
    limit = want if want is not None else n
    while left and len(chosen) < limit:
        best, best_v = None, None
        for i in sorted(left):
            m = max(sims[i][j] for j in chosen)
            v = pool[i][1] - lam * m
            if best_v is None or v > best_v + 1e-15:
                best, best_v = i, v
        chosen.append(best)
        left.discard(best)
    chosen.extend(sorted(left))
    return [pool[i] for i in chosen] + tail


def _vec_rows(idxs: list[int], docs: list[dict], index):
    """取候选向量（尽量复用索引里已解码的矩阵/行）。"""
    if not idxs:
        return None
    n_rows = max(idxs) + 1
    if index is not None and index.matrix is not None and n_rows <= index.matrix.shape[0]:
        return [index.matrix[i] for i in idxs]
    if index is not None and index.rows is not None and n_rows <= len(index.rows):
        return [index.rows[i] for i in idxs]
    try:
        return [unpack(docs[i]["vec"]) for i in idxs]
    except Exception:  # noqa: BLE001
        return None


def run(qvec: list[float], query_text: str, docs: list[dict],
        limit: int | None = None, cache_key=None,
        stats: dict | None = None) -> list[tuple[int, float]]:
    if limit is not None and limit <= 0:
        return []
    rank_limit = None
    if limit is not None:
        rank_limit = min(len(docs), limit + max(0, DEDUP_POOL) + max(0, RERANK_TOP))
    index = _get_search_index(cache_key, docs, stats)
    fused = fused_scores(qvec, query_text, docs, index, stats)
    ranked = [(i, fused[i]) for i in _order(fused, docs, rank_limit)]
    ranked = dedup(ranked, docs, index, want=None if limit is None else
                   limit + (max(0, RERANK_TOP) if RERANK_MODE == "http" else 0))
    if DIVERSITY > 0:
        ranked = diversify(ranked, docs, index, want=limit)
    ranked = rerank(query_text, ranked, docs)
    return ranked if limit is None else ranked[:limit]


def info() -> dict:
    """当前生效的检索配置（/version 披露；复现与提交材料核对用）。"""
    return {"fusion": FUSION, "dense_weight": DENSE_WEIGHT, "sparse_weight": SPARSE_WEIGHT,
            "sparse_norm": SPARSE_NORM, "bm25_rare_df": BM25_RARE_DF, "cjk_tokens": CJK_TOKENS,
            "temporal_rule": TEMPORAL_RULE, "temporal_weight": TEMPORAL_WEIGHT,
            "date_weight": DATE_WEIGHT, "recency_rule": RECENCY_RULE,
            "dedup_cos": DEDUP_COS, "diversity": DIVERSITY,
            "rerank": {"mode": RERANK_MODE, "kind": RERANK_KIND,
                       "model": RERANK_MODEL or None, "top": RERANK_TOP,
                       **_RERANK_STATS}}
