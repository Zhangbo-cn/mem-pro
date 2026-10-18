"""混合检索：dense(cosine) + sparse(BM25) → RRF 融合 → 去重 → 可选精排。

设计取舍（见 research/13、14）：
- user_id 隔离 ⇒ 每次检索只扫该 user 的分块，规模小，暴力 cosine 足够
- 精排是唯一不受模型限制的环节，但只对短名单做（延迟预算）
- 并列必须全序 tie-break（score desc, created_at asc, id asc）⇒ 复现确定
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from collections import Counter

from embed import norm, unpack

RRF_K = 60
DENSE_WEIGHT = float(os.environ.get("DENSE_WEIGHT", "1.0"))
SPARSE_WEIGHT = float(os.environ.get("SPARSE_WEIGHT", "1.0"))
RECALL_N = int(os.environ.get("RECALL_N", "50"))       # 粗排进精排的候选数
DEDUP_COS = float(os.environ.get("DEDUP_COS", "0.97"))  # 近重复阈值

RERANK_MODE = os.environ.get("RERANK_MODE", "none").lower()
RERANK_KIND = os.environ.get("RERANK_KIND", "tei").lower()   # tei | dashscope
RERANK_URL = os.environ.get("RERANK_URL", "").rstrip("/")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "")
RERANK_TOP = int(os.environ.get("RERANK_TOP", "30"))
RERANK_TIMEOUT = float(os.environ.get("RERANK_TIMEOUT", "8"))  # 单次精排超时（秒）
RERANK_API_KEY = os.environ.get("RERANK_API_KEY", "")


# ---------- 分词：英文按词，中文按字符 bigram ----------
_W = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokens(text: str) -> list[str]:
    t = text.lower()
    out = _W.findall(t)
    cjk = _CJK.findall(t)
    out.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1) if i + 1 < len(cjk))
    return out


# ---------- BM25 ----------
def bm25_scores(query: str, docs: list[dict]) -> list[float]:
    if not docs:
        return []
    q = tokens(query)
    if not q:
        return [0.0] * len(docs)
    doc_toks = [tokens(d["text"]) for d in docs]
    avgdl = sum(len(t) for t in doc_toks) / len(doc_toks) or 1.0
    dfs = Counter()
    for t in doc_toks:
        for term in set(t):
            dfs[term] += 1
    n = len(docs)
    k1, b = 1.5, 0.75
    out = []
    for t in doc_toks:
        tf = Counter(t)
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - dfs[term] + 0.5) / (dfs[term] + 0.5))
            dl = len(t) or 1
            s += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        out.append(s)
    return out


def cosine(qv: list[float], dv: list[float]) -> float:
    if len(qv) != len(dv):
        return 0.0
    return sum(a * b for a, b in zip(qv, dv))


# ---------- 融合 ----------
def _rank(scores: list[float]) -> list[int]:
    idx = list(range(len(scores)))
    idx.sort(key=lambda i: (-scores[i], i))  # 全序
    return idx


def hybrid(qvec: list[float], query_text: str, docs: list[dict]) -> list[tuple[int, float]]:
    dense = [cosine(qvec, unpack(d["vec"])) for d in docs]
    sparse = bm25_scores(query_text, docs)
    rd, rs = _rank(dense), _rank(sparse)
    rrf = [0.0] * len(docs)
    for pos, i in enumerate(rd):
        rrf[i] += DENSE_WEIGHT / (RRF_K + pos + 1)
    for pos, i in enumerate(rs):
        rrf[i] += SPARSE_WEIGHT / (RRF_K + pos + 1)
    order = _rank(rrf)
    return [(i, rrf[i]) for i in order]


# ---------- 近重复去重（同一事实的多副本只留一条）----------
def _norm_text(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())[:400]


def dedup(ranked: list[tuple[int, float]], docs: list[dict]) -> list[tuple[int, float]]:
    seen: list[tuple[str, list[float]]] = []
    out = []
    for i, sc in ranked:
        d = docs[i]
        key = _norm_text(d["text"])
        if any(key == k for k, _ in seen):
            continue
        v = unpack(d["vec"])
        if any(cosine(v, sv) > DEDUP_COS for _, sv in seen):
            continue
        seen.append((key, v))
        out.append((i, sc))
    return out


# ---------- 可选精排（唯一不受模型限制的环节）----------
def rerank(query_text: str, ranked: list[tuple[int, float]], docs: list[dict]) -> list[tuple[int, float]]:
    if RERANK_MODE != "http" or not RERANK_URL:
        return ranked
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
    req = urllib.request.Request(
        RERANK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {RERANK_API_KEY}"} if RERANK_API_KEY else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=RERANK_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if isinstance(resp, dict) and isinstance(resp.get("output"), dict):
            rows = resp["output"].get("results", resp)
        else:
            rows = resp.get("results", resp) if isinstance(resp, dict) else resp
        scored = []
        for row in rows:
            j = int(row.get("index", row.get("corpus_id", 0)))
            s = float(row.get("relevance_score", row.get("score", 0.0)))
            if 0 <= j < len(head):
                scored.append((head[j][0], s))
        if not scored:
            return ranked
        scored.sort(key=lambda t: (-t[1], docs[t[0]]["created_at"], t[0]))
        return scored + tail
    except Exception:
        return ranked  # 精排失败降级为融合序，绝不阻断 Search


def run(qvec: list[float], query_text: str, docs: list[dict]) -> list[tuple[int, float]]:
    ranked = hybrid(qvec, query_text, docs)
    ranked = dedup(ranked, docs)
    return rerank(query_text, ranked, docs)
