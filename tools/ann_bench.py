#!/usr/bin/env python3
"""ANN 索引选型对照：精确扫描 vs IVF-Flat vs HNSW，固定语料 / 固定 query / 固定 k。

回答一个问题：**在评测规模下，近似索引到底值不值得引入**。
所有实现都用同一批向量、同一批 query，ground truth 由精确扫描给出，
因此 Recall 是相对「当前生产实现」的真实损失，而不是论文里的相对分数。

用法：
  # 合成数据（无依赖，numpy 可选；没有 numpy 时只跑精确路径）
  python3 tools/ann_bench.py --n 1000,11000,50000 --dim 1024 --queries 200

  # 真实语料：ATM-Bench 邮件记忆 + 官方问题（hash 向量，只用于结构对照）
  python3 tools/ann_bench.py --corpus data/raw_memory/email/emails.json --dim 1024

  # 最佳证据：已有 text-embedding-v4 文档向量，用官方问题现场 embed 作 query
  python3 tools/ann_bench.py --vectors data/atm_vecs.npy --questions data/atm-bench/atm-bench.json

  # 需要 faiss 才有 IVF / HNSW 行；没有则自动跳过并标注
  /path/to/venv/bin/python tools/ann_bench.py --corpus ... --indexes exact,ivf,hnsw

指标：Recall@10 / Recall@100、单 query P50/P95、索引构建耗时、索引内存占用。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

import embed  # noqa: E402

try:
    import numpy as np
except Exception:  # pragma: no cover - 纯标准库环境
    np = None


# ---------------- 数据 ----------------
def synth(n: int, dim: int, n_queries: int, seed: int = 7):
    """聚类合成数据：模拟「同一用户记忆彼此相关」的结构，避免均匀随机导致 ANN 虚高/虚低。"""
    rng = np.random.default_rng(seed)
    n_clusters = max(4, int(math.sqrt(n)))
    centers = rng.normal(size=(n_clusters, dim)).astype("float32")
    assign = rng.integers(0, n_clusters, size=n)
    noise = rng.normal(scale=0.35, size=(n, dim)).astype("float32")
    vecs = centers[assign] + noise
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    q_idx = rng.integers(0, n, size=n_queries)
    queries = vecs[q_idx] + rng.normal(scale=0.12, size=(n_queries, dim)).astype("float32")
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    return vecs, queries


def load_vectors(path: str, questions: str, limit: int | None):
    """真实 text-embedding-v4 文档向量 + 官方 query（query 走当前 EMBED_MODE）。"""
    vecs = np.load(path).astype("float32")
    if limit:
        vecs = vecs[:limit]
    with open(questions, encoding="utf-8") as f:
        qas = json.load(f)
    texts = [q["question"] for q in qas]
    queries = np.asarray(embed.embed(texts), dtype="float32")
    return vecs, queries


def load_corpus(path: str, dim: int, limit: int | None):
    """ATM-Bench 邮件记忆：与 tools/atm_proxy.py 相同的文本化方式。"""
    with open(path, encoding="utf-8") as f:
        docs = json.load(f)
    if limit:
        docs = docs[:limit]
    texts = [f"[email] {d.get('timestamp', '')}\n{(d.get('short_summary') or '')[:400]}\n"
             f"{(d.get('detail') or '')[:900]}" for d in docs]
    vecs = np.asarray([embed._hash_embed(t, dim) for t in texts], dtype="float32")
    qpath = os.path.join(ROOT, "data", "atm-bench", "atm-bench.json")
    with open(qpath, encoding="utf-8") as f:
        qas = json.load(f)
    queries = np.asarray([embed._hash_embed(q["question"], dim) for q in qas[:400]],
                         dtype="float32")
    return vecs, queries


# ---------------- 指标 ----------------
def pct(values, p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))]


def exact_topk(vecs, queries, k: int):
    """精确扫描：当前生产路径的 numpy 等价物（矩阵内积 + 部分排序）。"""
    order = np.argsort(-(queries @ vecs.T), axis=1, kind="stable")
    return order[:, :k]


def recall(got, want) -> float:
    hits = 0
    total = 0
    for g, w in zip(got, want):
        want_set = set(int(x) for x in w)
        hits += len(want_set & set(int(x) for x in g))
        total += len(want_set)
    return hits / total if total else 0.0


def timed(fn, queries, k):
    """逐 query 计时，返回 (结果, P50, P95)。"""
    lat = []
    out = []
    for i in range(len(queries)):
        q = queries[i:i + 1]
        t = time.perf_counter()
        got = fn(q)
        lat.append((time.perf_counter() - t) * 1000)
        out.append(got[0])
    return out, statistics.median(lat), pct(lat, 0.95)


# ---------------- 索引实现 ----------------
def run_exact(vecs, queries, k):
    def fn(q):
        return np.argsort(-(q @ vecs.T), axis=1, kind="stable")[:, :k]
    return timed(fn, queries, k)


def run_ivf(vecs, queries, k, k_sweep=(1, 4, 16)):
    import faiss

    n, dim = vecs.shape
    # faiss 经验值：每个聚类至少 ~39 个训练点，否则质心欠拟合会虚低召回
    nlist = max(2, min(int(math.sqrt(n)), n // 40))
    t = time.perf_counter()
    quantizer = faiss.IndexFlatIP(dim)
    index = faiss.IndexIVFFlat(quantizer, dim, nlist, faiss.METRIC_INNER_PRODUCT)
    index.train(vecs)
    index.add(vecs)
    build_ms = (time.perf_counter() - t) * 1000
    rows = []
    for nprobe in k_sweep:
        index.nprobe = min(nprobe, nlist)
        got, p50, p95 = timed(lambda q: index.search(q, k)[1], queries, k)
        rows.append({"config": f"IVF-Flat nlist={nlist} nprobe={index.nprobe}",
                     "lat_p50": p50, "lat_p95": p95,
                     "build_ms": build_ms, "got": got})
    return rows


def run_ivfpq(vecs, queries, k, m=64):
    """IVF + PQ：用 8 bit/子空间压缩向量，衡量「省内存」要付多少召回。"""
    import faiss

    n, dim = vecs.shape
    nlist = max(2, min(int(math.sqrt(n)), n // 40))
    t = time.perf_counter()
    quantizer = faiss.IndexFlatIP(dim)
    index = faiss.IndexIVFPQ(quantizer, dim, nlist, m, 8, faiss.METRIC_INNER_PRODUCT)
    try:
        index.train(vecs)
    except RuntimeError as e:  # 训练点少于 PQ 子空间数：小用户根本不该用 PQ
        return [{"config": f"IVF-PQ nlist={nlist} m={m}", "lat_p50": None,
                 "note": f"训练样本不足：{e}"}]
    index.add(vecs)
    build_ms = (time.perf_counter() - t) * 1000
    rows = []
    for nprobe in (8, 32):
        index.nprobe = min(nprobe, nlist)
        got, p50, p95 = timed(lambda q: index.search(q, k)[1], queries, k)
        rows.append({"config": f"IVF-PQ nlist={nlist} m={m} nprobe={index.nprobe}",
                     "lat_p50": p50, "lat_p95": p95, "build_ms": build_ms, "got": got,
                     "index_mib": index.ntotal * index.code_size / 1048576})
    return rows


def run_hnsw(vecs, queries, k, m=16, ef_sweep=(32, 128)):
    import faiss

    n, dim = vecs.shape
    rows = []
    t = time.perf_counter()
    index = faiss.IndexHNSWFlat(dim, m, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = 200
    index.add(vecs)
    build_ms = (time.perf_counter() - t) * 1000
    for ef in ef_sweep:
        index.hnsw.efSearch = ef
        got, p50, p95 = timed(lambda q: index.search(q, k)[1], queries, k)
        rows.append({"config": f"HNSW M={m} efSearch={ef}",
                     "lat_p50": p50, "lat_p95": p95, "build_ms": build_ms, "got": got})
    return rows


# ---------------- 主流程 ----------------
def measure(vecs, queries, k, indexes) -> dict:
    n, dim = vecs.shape
    truth = exact_topk(vecs, queries, k)
    got, p50, p95 = run_exact(vecs, queries, k)
    results = [{"config": "exact (current)", "recall@10": 1.0, "lat_p50": p50,
                "lat_p95": p95, "build_ms": 0.0}]
    exact_p50 = p50

    opt_in = False
    if np is not None:
        try:
            import faiss  # noqa: F401
            opt_in = True
        except Exception:
            opt_in = False

    if "ivf" in indexes:
        if opt_in:
            for row in run_ivf(vecs, queries, k):
                row["recall@10"] = recall(row.pop("got"), truth)
                results.append(row)
        else:
            results.append({"config": "IVF-Flat", "recall@10": None,
                            "note": "需要 faiss（pip install faiss-cpu）"})
    if "ivfpq" in indexes:
        if opt_in:
            for row in run_ivfpq(vecs, queries, k):
                if "got" in row:
                    row["recall@10"] = recall(row.pop("got"), truth)
                results.append(row)
        else:
            results.append({"config": "IVF-PQ", "recall@10": None,
                            "note": "需要 faiss（pip install faiss-cpu）"})
    if "hnsw" in indexes:
        if opt_in:
            for row in run_hnsw(vecs, queries, k):
                row["recall@10"] = recall(row.pop("got"), truth)
                results.append(row)
        else:
            results.append({"config": "HNSW", "recall@10": None,
                            "note": "需要 faiss（pip install faiss-cpu）"})

    for row in results:
        if row.get("lat_p50") and row["lat_p50"] > 0 and exact_p50 > 0:
            row["speedup_vs_exact"] = round(exact_p50 / row["lat_p50"], 2)
    return {"n": int(n), "dim": int(dim), "k": k, "rows": results}


def main() -> int:
    ap = argparse.ArgumentParser(description="ANN index selection benchmark")
    ap.add_argument("--n", default="1000,3000,11000,50000",
                    help="合成数据的记忆条数列表（逗号分隔）")
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--corpus", default="", help="改用 JSON 语料（ATM-Bench 邮件记忆格式）")
    ap.add_argument("--vectors", default="", help="改用 .npy 文档向量（如 data/atm_vecs.npy）")
    ap.add_argument("--questions", default="data/atm-bench/atm-bench.json",
                    help="配合 --vectors：问题文件，query 用当前 EMBED_MODE 现场编码")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--indexes", default="exact,ivf,hnsw,ivfpq")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if np is None:
        print("未安装 numpy：本工具只用于选型分析，请在有 numpy 的环境运行", file=sys.stderr)
        return 2

    indexes = {x.strip() for x in args.indexes.split(",") if x.strip()}
    out = []
    if args.vectors:
        vecs, queries = load_vectors(args.vectors, args.questions, args.limit or None)
        out.append(measure(vecs, queries[:args.queries], args.k, indexes))
    elif args.corpus:
        vecs, queries = load_corpus(args.corpus, args.dim, args.limit or None)
        queries = queries[:args.queries]
        out.append(measure(vecs, queries, args.k, indexes))
    else:
        for n in [int(x) for x in args.n.split(",") if x.strip()]:
            vecs, queries = synth(n, args.dim, args.queries)
            out.append(measure(vecs, queries[:args.queries], args.k, indexes))

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    for block in out:
        n, dim = block["n"], block["dim"]
        mib = n * dim * 4 / 1048576
        print(f"\n== n={n} dim={dim} k={block['k']}  精确向量内存≈{mib:.1f} MiB ==")
        print(f"  {'配置':34s} {'R@'+str(block['k']):>6s} {'P50 ms':>8s} {'P95 ms':>8s} "
              f"{'build ms':>9s} {'vs exact':>8s}")
        for row in block["rows"]:
            r = "  —   " if row.get("recall@10") is None else f"{row['recall@10']:.3f}"
            p50 = "—" if row.get("lat_p50") is None else f"{row['lat_p50']:.2f}"
            p95 = "—" if row.get("lat_p95") is None else f"{row['lat_p95']:.2f}"
            build = "—" if not row.get("build_ms") else f"{row['build_ms']:.0f}"
            sp = "—" if not row.get("speedup_vs_exact") else f"{row['speedup_vs_exact']:.1f}x"
            if row.get("index_mib"):
                sp += f"  idx={row['index_mib']:.0f}MiB"
            note = f"  ({row['note']})" if row.get("note") else ""
            print(f"  {row['config']:34s} {r:>6s} {p50:>8s} {p95:>8s} {build:>9s} {sp:>8s}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
