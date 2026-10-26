#!/usr/bin/env python3
"""α/β 网格调优 + 融合方式/分词方式 A/B，针对 tools/eval_queries.json 的标注集。

离线可跑：不依赖服务，只调 retrieve 的打分函数。

  python3 tools/tune.py                     # 用 .env / 环境变量里的 embedding 模式
  python3 tools/tune.py --mode hash         # 强制离线 hash（免费、可复现）
  python3 tools/tune.py --step 0.05         # 更细的网格
  python3 tools/tune.py --compare           # 额外对比 FUSION=rrf 与分词方案

指标：MRR@10 / nDCG@5 / Recall@5 / top1 命中率。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

CACHE_PATH = "/tmp/mempro-tune-cache.json"
BEST_A = 0.9
# 由 --rerank 设置；为空时评测只到融合/去重为止（与线上 RERANK_MODE=none 一致）
RERANK_URL = ""


def load_set(which: str = "all"):
    """hand=手写现实集；gen=生成集；all=合并（默认，追求统计功率）。"""
    parts = []
    for name in (("eval_queries.json",) if which == "hand" else
                 ("eval_gen.json",) if which == "gen" else
                 ("eval_queries.json", "eval_gen.json")):
        with open(os.path.join(ROOT, "tools", name), encoding="utf-8") as f:
            parts.append(json.load(f))
    docs, queries = [], []
    for j, p in enumerate(parts):          # 合并时给 id 加前缀，避免撞车
        pre = "" if len(parts) == 1 else f"s{j}-"
        docs += [dict(d, id=pre + d["id"]) for d in p["docs"]]
        queries += [dict(q, gold=[pre + g for g in q["gold"]]) for q in p["queries"]]
    return {"docs": docs, "queries": queries}


def embed_all(texts: list[str], tag: str) -> list[list[float]]:
    """带磁盘缓存：同一 (模型, 文本) 只付一次 embedding 费用。"""
    import embed as E

    key_prefix = f"{E.MODE}:{E.MODEL}:{E.DIM}"
    cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            cache = json.load(open(CACHE_PATH, encoding="utf-8"))
        except Exception:
            cache = {}
    todo = [(i, t) for i, t in enumerate(texts) if f"{key_prefix}|{t}" not in cache]
    hits = len(texts) - len(todo)
    if todo:
        vecs = E.embed([t for _, t in todo])
        for (i, t), v in zip(todo, vecs):
            cache[f"{key_prefix}|{t}"] = v
        json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"))
    print(f"  embedding[{tag}]: cache_hit={hits} api_calls={len(todo)} (mode={E.MODE})")
    return [cache[f"{key_prefix}|{t}"] for t in texts]


def metrics(ranked_ids: list[str], gold: list[str], k: int = 5) -> tuple[float, float, float, float]:
    """MRR@10 / nDCG@5 / Recall@5 / top1。"""
    pos = {gid: j + 1 for j, gid in enumerate(ranked_ids)}  # 1-based
    hits = [pos[g] for g in gold if g in pos]
    mrr = 1.0 / min(hits) if hits else 0.0
    dcg = sum(1.0 / math.log2(p + 1) for p in hits if p <= k)
    idcg = sum(1.0 / math.log2(j + 2) for j in range(min(len(gold), k)))
    ndcg = dcg / idcg if idcg else 0.0
    rec = len([p for p in hits if p <= k]) / len(gold) if gold else 0.0
    top1 = 1.0 if ranked_ids and ranked_ids[0] in gold else 0.0
    return mrr, ndcg, rec, top1


def rank(retrieve, qvec, qtext, docs):
    """始终评测线上同一条链路 `retrieve.run()`：融合 → 时态 → 去重 →（可选）精排。

    旧版在无精排时评测 `hybrid()`，而服务走 `run()`；时态规则只在前者生效，
    导致 README 里的时态增益线上拿不到。现在评测与服务共用一份代码。
    """
    return retrieve.run(qvec, qtext, docs, limit=10)


def evaluate(retrieve, docs, qvecs, queries, topk=10, kinds=None):
    acc = [0.0] * 4
    per_kind: dict[str, list[list[float]]] = {}
    for qvec, q in zip(qvecs, queries):
        ranked = rank(retrieve, qvec, q["q"], docs)
        ids = [docs[i]["id"] for i, _ in ranked[:topk]]
        m = metrics(ids, q["gold"], k=5)
        for j in range(4):
            acc[j] += m[j]
        per_kind.setdefault(q.get("kind", "?"), []).append(m)
    n = len(queries)
    out = [x / n for x in acc]
    if kinds is not None:
        for kind, ms in per_kind.items():
            kinds[kind] = [sum(x[j] for x in ms) / len(ms) for j in range(4)]
    return out


def old_hybrid(retrieve, qvec, qtext, docs):
    """旧实现：RRF + 稀疏全零也参与名次（按文档插入顺序白送分）。"""
    dense = retrieve.dense_scores(qvec, docs)
    sparse = retrieve.bm25_scores(qtext, docs)
    rrf = [0.0] * len(docs)
    for pos, i in enumerate(retrieve._rank(dense)):
        rrf[i] += 1.0 / (60 + pos + 1)
    for pos, i in enumerate(retrieve._rank(sparse)):
        rrf[i] += 1.0 / (60 + pos + 1)
    return [(i, rrf[i]) for i in retrieve._rank(rrf)]


def fmt(m) -> str:
    return f"MRR@10={m[0]:.3f}  nDCG@5={m[1]:.3f}  R@5={m[2]:.3f}  top1={m[3]:.3f}"


def per_query_mrr(retrieve, docs, qvecs, queries) -> list[float]:
    out = []
    for qvec, q in zip(qvecs, queries):
        ids = [docs[i]["id"] for i, _ in rank(retrieve, qvec, q["q"], docs)[:10]]
        out.append(metrics(ids, q["gold"], k=5)[0])
    return out


def paired_bootstrap(a: list[float], b: list[float], n: int = 10000) -> tuple[float, float, float]:
    """返回 (均值差 a-b, 95%CI 下界, P(a 更好))；固定种子 ⇒ 可复现。"""
    import random

    rng = random.Random(20261024)
    diffs = [x - y for x, y in zip(a, b)]
    m = sum(diffs) / len(diffs)
    boots = []
    k = len(diffs)
    for _ in range(n):
        boots.append(sum(diffs[rng.randrange(k)] for _ in range(k)) / k)
    boots.sort()
    lo = boots[int(0.025 * n)]
    better = sum(1 for x in boots if x > 0) / n
    return m, lo, better


def split_queries(queries, which):
    """even 索引当调参集，odd 当留出集，避免在同一批 query 上调参+报分。"""
    if which == "tune":
        return queries[0::2]
    if which == "holdout":
        return queries[1::2]
    return queries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["api", "hash"], default=None)
    ap.add_argument("--step", type=float, default=0.1)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--split", choices=["all", "tune", "holdout"], default="all")
    ap.add_argument("--set", choices=["all", "hand", "gen"], default="all")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--rerank", default="",
                    help="TEI 兼容精排地址（如 http://127.0.0.1:8010/rerank）；给定则额外评测精排增益")
    ap.add_argument("--rerank-limit", type=int, default=0,
                    help="精排评测 query 数上限；按 kind 分层抽样，0 表示全量")
    ap.add_argument("--temporal", action="store_true",
                    help="A/B 时态规则（TEMPORAL_RULE=off vs bias），并按 query 类型给出差值")
    ap.add_argument("--temporal-weight", type=float, default=None,
                    help="时态加分上限（默认取 retrieve.TEMPORAL_WEIGHT）")
    args = ap.parse_args()

    if args.mode:
        os.environ["EMBED_MODE"] = args.mode
    os.environ.setdefault("DB_PATH", "/tmp/mempro-tune.db")

    import dotenv
    dotenv.load()
    import embed as E
    import retrieve as R

    data = load_set(args.set)
    docs_meta = data["docs"]
    queries = split_queries(data["queries"], args.split)

    print(f"集子: {len(docs_meta)} 条记忆 / {len(queries)} 条 query "
          f"(set={args.set}, split={args.split}, embed mode={E.MODE})")
    dvecs = embed_all([d["text"] for d in docs_meta], "docs")
    qvecs = embed_all([q["q"] for q in queries], "queries")
    docs = [
        {"id": d["id"], "text": d["text"], "images": [], "seq": i,
         "created_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z", "vec": E.pack(v)}
        for i, (d, v) in enumerate(zip(docs_meta, dvecs))
    ]

    # ---------- 1) α/β 网格 ----------
    R.FUSION = "linear"
    grid = []
    steps = [round(x * args.step, 4) for x in range(int(1 / args.step) + 1)]
    for a in steps:
        for b in steps:
            if a + b == 0:
                continue
            R.DENSE_WEIGHT, R.SPARSE_WEIGHT = a, b
            m = evaluate(R, docs, qvecs, queries)
            grid.append((m[0] + m[1] + m[2], a, b, m))
    grid.sort(key=lambda t: -t[0])

    print("\n=== α/β 网格（按 MRR+nDCG+R@5 排序，前 %d）===" % args.top)
    for _, a, b, m in grid[: args.top]:
        print(f"  α={a:<5} β={b:<5}  {fmt(m)}")

    best = grid[0]
    print(f"\n网格最优: α={best[1]} β={best[2]}  {fmt(best[3])}")
    global BEST_A
    BEST_A = best[1]

    # 关键对照点
    print("\n=== 关键权重对照 ===")
    for a, b in [(1.0, 0.0), (0.9, 0.1), (0.7, 0.3), (0.5, 0.5), (0.3, 0.7), (0.0, 1.0)]:
        R.DENSE_WEIGHT, R.SPARSE_WEIGHT = a, b
        print(f"  α={a} β={b}  {fmt(evaluate(R, docs, qvecs, queries))}")

    # ---------- 2) β 曲线（固定 α）----------
    print("\n=== β 曲线（α=%.2f 固定）===" % BEST_A)
    for b in [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 1.0]:
        R.DENSE_WEIGHT, R.SPARSE_WEIGHT = BEST_A, b
        print(f"  β={b:<5} {fmt(evaluate(R, docs, qvecs, queries))}")

    # ---------- 2a) 候选配置的配对 bootstrap 判定 ----------
    print("\n=== 候选配置 vs 基线(α=1,β=0) 配对 bootstrap（%d 次重采样）===" % 10000)
    R.SPARSE_NORM, R.BM25_RARE_DF = "sat", 0.15
    R._TOK_CACHE.clear()
    base = None
    for a, b in [(1.0, 0.0), (0.9, 0.1), (0.85, 0.15), (0.8, 0.2), (0.7, 0.3)]:
        R.DENSE_WEIGHT, R.SPARSE_WEIGHT = a, b
        cur = per_query_mrr(R, docs, qvecs, queries)
        if base is None:
            base = cur
            print(f"  α={a:<5} β={b:<5} MRR={sum(cur)/len(cur):.4f}  (基线)")
            continue
        mean, lo, better = paired_bootstrap(cur, base)
        print(f"  α={a:<5} β={b:<5} MRR={sum(cur)/len(cur):.4f}  Δ={mean:+.4f}  "
              f"95%CI[{lo:+.4f}, ...]  P(优于基线)={better:.2f}")

    # ---------- 2b) 稀疏校准方式 × β ----------
    print("\n=== 稀疏校准方式 × β（α=%.2f）===" % BEST_A)
    saved_norm, saved_rare = R.SPARSE_NORM, R.BM25_RARE_DF
    for norm in ("max", "sat"):
        for rare in (0.0, 0.15, 0.5):
            R.SPARSE_NORM, R.BM25_RARE_DF = norm, rare
            R._TOK_CACHE.clear()
            row = []
            for b in (0.0, 0.1, 0.2, 0.3):
                R.DENSE_WEIGHT, R.SPARSE_WEIGHT = BEST_A, b
                m = evaluate(R, docs, qvecs, queries)
                row.append(f"β={b}:{m[0]:.3f}")
            print(f"  norm={norm:<4} rare_df={rare:<4} " + "  ".join(row))
    R.SPARSE_NORM, R.BM25_RARE_DF = saved_norm, saved_rare
    R._TOK_CACHE.clear()

    # ---------- 2c) 时态规则 A/B ----------
    if args.temporal:
        if args.temporal_weight is not None:
            R.TEMPORAL_WEIGHT = args.temporal_weight
        saved_rule, saved_weights = R.TEMPORAL_RULE, (R.DENSE_WEIGHT, R.SPARSE_WEIGHT)
        R.DENSE_WEIGHT, R.SPARSE_WEIGHT = 0.95, 0.05  # 线上默认，便于横向比较
        print(f"\n=== 时态规则 A/B（α=0.95 β=0.05，TEMPORAL_WEIGHT={R.TEMPORAL_WEIGHT:.3f}）===")
        base_kinds, base = None, None
        for rule in ("off", "tense"):
            R.TEMPORAL_RULE = rule
            kinds = {}
            m = evaluate(R, docs, qvecs, queries, kinds=kinds)
            print(f"  TEMPORAL_RULE={rule:<5} {fmt(m)}")
            if rule == "off":
                base, base_kinds = m, kinds
            else:
                print(f"    Δ vs off: MRR {m[0]-base[0]:+.3f}  nDCG@5 {m[1]-base[1]:+.3f}  "
                      f"top1 {m[3]-base[3]:+.3f}")
                for k in sorted(base_kinds):
                    d = kinds[k][0] - base_kinds[k][0]
                    flag = "  <-- 退化" if d <= -0.02 else ""
                    print(f"    {k:<22} MRR {base_kinds[k][0]:.3f} → {kinds[k][0]:.3f} ({d:+.3f}){flag}")
        R.TEMPORAL_RULE = saved_rule
        R.DENSE_WEIGHT, R.SPARSE_WEIGHT = saved_weights

    # ---------- 3) 融合方式 / 分词方式对比 ----------
    # ---------- 3) 可选精排增益（只有 --rerank 时跑）----------
    if args.rerank:
        import time
        import urllib.request

        def probe() -> bool:
            body = json.dumps({"query": "ping", "documents": ["ping", "pong"],
                               "top_n": 2}).encode()
            req = urllib.request.Request(args.rerank, data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    json.loads(r.read().decode())
                return True
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️ 精排服务不可用（{e}），跳过")
                return False

        rerank_pairs = list(zip(qvecs, queries))
        if 0 < args.rerank_limit < len(rerank_pairs):
            import random

            groups = {}
            for pair in rerank_pairs:
                groups.setdefault(pair[1].get("kind", "?"), []).append(pair)
            rng = random.Random(20261025)
            for group in groups.values():
                rng.shuffle(group)
            sample = []
            while len(sample) < args.rerank_limit:
                for kind in sorted(groups):
                    if groups[kind] and len(sample) < args.rerank_limit:
                        sample.append(groups[kind].pop())
            rerank_pairs = sample
        rerank_qvecs = [pair[0] for pair in rerank_pairs]
        rerank_queries = [pair[1] for pair in rerank_pairs]
        print(f"\n=== 精排增益（融合→去重→精排，RERANK_TOP=%d，query=%d/%d）===" %
              (R.RERANK_TOP, len(rerank_queries), len(queries)))
        R.RERANK_MODE, R.RERANK_URL = "http", args.rerank
        if probe():
            for a, b in [(0.95, 0.05), (best[1], best[2])]:
                R.DENSE_WEIGHT, R.SPARSE_WEIGHT = a, b
                kinds_f, kinds_r = {}, {}
                globals()["RERANK_URL"] = ""
                before = evaluate(R, docs, rerank_qvecs, rerank_queries, kinds=kinds_f)
                globals()["RERANK_URL"] = args.rerank
                t0 = time.time()
                after = evaluate(R, docs, rerank_qvecs, rerank_queries, kinds=kinds_r)
                per_q = (time.time() - t0) / max(1, len(rerank_queries))
                globals()["RERANK_URL"] = ""
                print(f"  α={a} β={b}")
                print(f"    融合序      {fmt(before)}")
                print(f"    +精排       {fmt(after)}   ΔMRR={after[0]-before[0]:+.3f}  每 query≈{per_q*1000:.0f}ms")
                for k in sorted(kinds_f):
                    d = kinds_r[k][0] - kinds_f[k][0]
                    if abs(d) >= 0.01:
                        print(f"      {k:<22} MRR {kinds_f[k][0]:.3f} → {kinds_r[k][0]:.3f} ({d:+.3f})")
        else:
            R.RERANK_MODE = "none"

    if args.compare:
        R.DENSE_WEIGHT, R.SPARSE_WEIGHT = best[1], best[2]
        print("\n=== FUSION 对比（同一 α/β）===")
        for fusion in ("rrf", "linear"):
            R.FUSION = fusion
            print(f"  FUSION={fusion:<7} {fmt(evaluate(R, docs, qvecs, queries))}")

        R.FUSION = "linear"
        print("\n=== 中文分词方案对比 ===")
        for cjk in ("bigram", "bigram_unigram", "unigram"):
            R.CJK_TOKENS = cjk
            R._TOK_CACHE.clear()
            print(f"  CJK_TOKENS={cjk:<15} {fmt(evaluate(R, docs, qvecs, queries))}")
        R.CJK_TOKENS = "bigram_unigram"
        R._TOK_CACHE.clear()

        print("\n=== 稀疏侧零命中弃权 的开/关对比 ===")
        R.FUSION = "rrf"
        print(f"  FUSION=rrf  + 弃权      {fmt(evaluate(R, docs, qvecs, queries))}")
        R.FUSION = "linear"
        print(f"  FUSION=linear + 弃权    {fmt(evaluate(R, docs, qvecs, queries))}")

        print("\n=== 旧实现（RRF + 稀疏全零也投票）===")
        old = [0.0] * 4
        for qvec, q in zip(qvecs, queries):
            ids = [docs[i]["id"] for i, _ in old_hybrid(R, qvec, q["q"], docs)[:10]]
            for j, v in enumerate(metrics(ids, q["gold"], k=5)):
                old[j] += v
        print(f"  old_hybrid             {fmt([x / len(queries) for x in old])}")

        print("\n=== 仍错的 query（新默认）===")
        for qvec, q in zip(qvecs, queries):
            ids = [docs[i]["id"] for i, _ in R.hybrid(qvec, q["q"], docs)[:5]]
            if not ids or ids[0] not in q["gold"]:
                old_ids = [docs[i]["id"] for i, _ in old_hybrid(R, qvec, q["q"], docs)[:5]]
                print(f"  [{q.get('kind','?'):<18}] {q['q'][:26]:<28} gold={q['gold']} new={ids[:3]} old={old_ids[:3]}")

        print("\n=== 按 query 类型拆解（新默认 vs 旧实现）===")
        kinds_new, kinds_old = {}, {}
        evaluate(R, docs, qvecs, queries, kinds=kinds_new)
        old_kinds = {}
        for qvec, q in zip(qvecs, queries):
            ids = [docs[i]["id"] for i, _ in old_hybrid(R, qvec, q["q"], docs)[:10]]
            old_kinds.setdefault(q.get("kind", "?"), []).append(metrics(ids, q["gold"], k=5))
        for k, ms in old_kinds.items():
            kinds_old[k] = [sum(x[j] for x in ms) / len(ms) for j in range(4)]
        for k in sorted(kinds_new):
            print(f"  {k:<22} 新 MRR={kinds_new[k][0]:.3f} | 旧 MRR={kinds_old.get(k, [0]*4)[0]:.3f}")

    if args.rerank:
        globals()["RERANK_URL"] = ""

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
