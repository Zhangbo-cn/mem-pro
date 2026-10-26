"""ATM-Bench 本地代理榜：仅用「文本化记忆」做检索评测（不跑 Answer / Judge）。

为什么这样做：
- AML 的 Search 只负责「按顺序吐证据」，判分在平台侧；因此 **检索质量** 是我们可以
  在本地全量、无额度、可复现地优化的唯一环节。
- ATM-Bench 的图片/视频都自带 VLM caption（`processed_memory/*_batch_results.json`），
  邮件自带 summary/detail ⇒ 整个基准可以退化成「1.1 万条文本记忆 × 1,013 条查询」，
  用与线上服务完全相同的 dense+BM25→RRF→rerank 管线打分。

指标：Recall@k / MRR@10 / nDCG@10；按 qtype（open_end / number / list_recall）与
证据模态（image+video / email / 混合）拆分。**这是相对代理榜，不是 ATM-Bench 官方分数**
（官方 QS 需要 gpt-5-mini 判分，本地不跑）。

用法：
  export EMBED_MODE=api DASHSCOPE_API_KEY=sk-xxx
  python tools/atm_proxy.py build                    # 建文档 + 向量（带缓存，可重跑）
  python tools/atm_proxy.py eval --topk 10
  python tools/atm_proxy.py eval --rerank http --rerank-url http://127.0.0.1:8010/rerank --rtop 30 --limit 200
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

import embed  # noqa: E402
import retrieve  # noqa: E402

DATA = os.path.join(ROOT, "data")
DOCS_JSON = os.path.join(DATA, "atm_docs.json")
VEC_NPY = os.path.join(DATA, "atm_vecs.npy")
ID_JSON = os.path.join(DATA, "atm_doc_ids.json")


# ---------------- 文档构建 ----------------
def _trim(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _flat(v) -> str:
    """tags/entities 结构不稳（str / list[str] / list[dict] 混用），统一拍平成短串。"""
    if not v:
        return ""
    if isinstance(v, str):
        return v
    out = []
    for x in v:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k in ("entity", "type", "name"):
                if isinstance(x.get(k), str):
                    out.append(x[k].strip('"{} '))
    return ", ".join(o for o in out if o)[:300]


def _load_optional(rel: str):
    """图片/视频的 VLM 结果不一定在仓库里：缺就跳过，只评文本模态。

    早期版本直接 open() 会让缺文件的机器整个工具跑不起来，也让「只评邮件」
    这种交叉验证没法做。
    """
    path = os.path.join(DATA, rel)
    if not os.path.exists(path):
        print(f"跳过 {rel}（不存在）：相关模态的记忆不进语料")
        return None
    return json.load(open(path))


def load_docs() -> list[dict]:
    docs = []
    img = _load_optional("processed_memory/image_batch_results.json")
    for it in img or []:
        did = os.path.splitext(os.path.basename(it["image_path"]))[0]
        tags = _flat(it.get("tags"))
        ents = _flat(it.get("entities"))
        docs.append({
            "id": did, "kind": "image", "ts": it.get("timestamp", ""),
            "text": (f"[photo] {it.get('timestamp','')} | {it.get('city') or it.get('location_name') or ''}\n"
                     f"{_trim(it.get('short_caption',''), 300)}\n{_trim(it.get('caption',''), 700)}\n"
                     f"tags: {tags}\nentities: {ents}"),
        })
    # 视频
    vid = _load_optional("processed_memory/video_batch_results.json")
    for it in vid or []:
        did = os.path.splitext(os.path.basename(it["video_path"]))[0]
        docs.append({
            "id": did, "kind": "video", "ts": it.get("timestamp", ""),
            "text": (f"[video] {it.get('timestamp','')} | {it.get('city') or it.get('location_name') or ''}\n"
                     f"{_trim(it.get('caption',''), 900)}"),
        })
    # 邮件
    em = _load_optional("raw_memory/email/emails.json") or []
    for it in em:
        docs.append({
            "id": it["id"], "kind": "email", "ts": it.get("timestamp", ""),
            "text": (f"[email] {it.get('timestamp','')}\n{_trim(it.get('short_summary',''), 400)}\n"
                     f"{_trim(it.get('detail',''), 900)}"),
        })
    # id 去重（图片/视频同秒可能撞名，后者加后缀保留）
    seen = Counter()
    for d in docs:
        seen[d["id"]] += 1
        if seen[d["id"]] > 1:
            d["id"] = f"{d['id']}#{d['kind']}{seen[d['id']]}"
    return docs


def load_qas(limit: int | None = None) -> list[dict]:
    qas = json.load(open(os.path.join(DATA, "atm-bench/atm-bench.json")))
    return qas[:limit] if limit else qas


# ---------------- 向量缓存 ----------------
def build_vectors(docs: list[dict], rebuild: bool = False) -> np.ndarray:
    ids = [d["id"] for d in docs]
    if not rebuild and os.path.exists(VEC_NPY) and os.path.exists(ID_JSON):
        old_ids = json.load(open(ID_JSON))
        if old_ids == ids:
            print(f"复用向量缓存 {VEC_NPY}")
            return np.load(VEC_NPY)
        print("文档集合变化 → 重建向量")
    t0 = time.time()
    texts = [d["text"] for d in docs]
    vecs = _embed_parallel(texts)
    m = np.asarray(vecs, dtype=np.float32)
    np.save(VEC_NPY, m)
    json.dump(ids, open(ID_JSON, "w"))
    print(f"向量已保存 {m.shape}  {time.time()-t0:.0f}s")
    return m


def _embed_parallel(texts: list[str], workers: int = 8) -> list[list[float]]:
    """并发调用 embedding（每批 10 条），把 1.1 万文档从 ~60min 压到 ~10min。

    只在 api 模式下并发；hash 模式直接串行（本地计算本来就快）。
    """
    from concurrent.futures import ThreadPoolExecutor
    if embed.MODE != "api":
        return embed.embed(texts)
    batches = [texts[i:i + 10] for i in range(0, len(texts), 10)]
    out: list[list[float] | None] = [None] * len(batches)
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(embed._call_api, b): j for j, b in enumerate(batches)}
        from concurrent.futures import as_completed
        for f in as_completed(futs):
            out[futs[f]] = [embed.norm(v) for v in f.result()]
            done += 1
            if done % 50 == 0 or done == len(batches):
                print(f"  embed {done*10}/{len(texts)}  {time.time()-t0:.0f}s", flush=True)
    return [v for b in out for v in b]  # type: ignore[union-attr]


# ---------------- 倒排 BM25 ----------------
class BM25:
    def __init__(self, docs: list[dict], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.dl = np.zeros(self.n, dtype=np.float32)
        post: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            toks = retrieve.tokens(d["text"])
            self.dl[i] = len(toks) or 1
            for term, tf in Counter(toks).items():
                post.setdefault(term, []).append((i, tf))
        self.avgdl = float(self.dl.mean() or 1.0)
        self.post = post
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in post.items()}

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float32)
        k1, b, avgdl = self.k1, self.b, self.avgdl
        for term in retrieve.tokens(query):
            p = self.post.get(term)
            if not p:
                continue
            idf = self.idf[term]
            for i, tf in p:
                out[i] += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * self.dl[i] / avgdl))
        return out


def rrf(dense: np.ndarray, sparse: np.ndarray, k: int = 60, wd: float = 1.0, ws: float = 1.0) -> np.ndarray:
    rd = np.argsort(-dense, kind="stable")
    rs = np.argsort(-sparse, kind="stable")
    out = np.zeros(len(dense), dtype=np.float32)
    out[rd] += wd / (k + np.arange(1, len(dense) + 1))
    out[rs] += ws / (k + np.arange(1, len(dense) + 1))
    return out


# ---------------- 指标 ----------------
def ndcg_at(ranked_ids: list[str], gold: set[str], k: int = 10) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, g in enumerate(ranked_ids[:k]) if g in gold)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / idcg if idcg else 0.0


def evaluate(qas, docs, vecs, bm, topk, rerank_cfg=None, rtop=30) -> dict:
    id2i = {d["id"]: i for i, d in enumerate(docs)}
    res = []
    lat = []
    skipped = 0
    for qa in qas:
        gold_ids = [e for e in qa["evidence_ids"] if e in id2i]
        # 语料只覆盖部分模态时，缺证据的样本没有意义：gold 少一条就等于把
        # 「召回全部证据」偷偷换成「召回一部分」，指标虚高。
        if len(gold_ids) != len(qa["evidence_ids"]):
            skipped += 1
            continue
        if not gold_ids:
            skipped += 1
            continue
        gold = set(gold_ids)
        t0 = time.time()
        qv = np.asarray(embed.embed([qa["question"]])[0], dtype=np.float32)
        dense = vecs @ qv
        sparse = bm.scores(qa["question"])
        fused = rrf(dense, sparse)
        order = np.argsort(-fused, kind="stable")
        # 只取头部再精排（延迟预算）
        head = order[:rtop]
        if rerank_cfg:
            texts = [docs[i]["text"][:4000] for i in head]
            head = _rerank(qa["question"], head, texts, docs, rerank_cfg)
        ranked = [docs[i]["id"] for i in head]
        lat.append((time.time() - t0) * 1000)
        r = {}
        for k in (1, 5, topk):
            r[f"recall@{k}"] = sum(1 for g in gold if g in set(ranked[:k])) / len(gold)
        rr = next((1 / (i + 1) for i, g in enumerate(ranked) if g in gold), 0.0)
        r["mrr@10"] = rr if rr else 0.0
        r["ndcg@10"] = ndcg_at(ranked, gold, 10)
        # 命中位置（第一个 gold 的排名，1-based，未命中=0）
        r["first_rank"] = next((i + 1 for i, g in enumerate(ranked) if g in gold), 0)
        r["qtype"] = qa.get("qtype", "")
        kinds = {docs[id2i[g]]["kind"] for g in gold_ids}
        r["modality"] = "+".join(sorted(kinds))
        r["n_gold"] = len(gold_ids)
        res.append(r)
    if skipped:
        print(f"跳过 {skipped} 条（证据不在当前语料里）")
    return {"rows": res, "lat": lat}


def _rerank(query: str, head: np.ndarray, texts: list[str], docs, cfg) -> np.ndarray:
    import urllib.request
    if cfg.get("kind") == "dashscope":
        payload = {"model": cfg.get("model") or "gte-rerank-v2",
                   "input": {"query": query, "documents": texts},
                   "parameters": {"return_documents": False}}
    else:
        payload = {"query": query, "documents": texts, "top_n": len(texts)}
        if cfg.get("model"):
            payload["model"] = cfg["model"]
    headers = {"Content-Type": "application/json"}
    if cfg.get("key"):
        headers["Authorization"] = f"Bearer {cfg['key']}"
    req = urllib.request.Request(cfg["url"], data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.loads(r.read())
        if isinstance(rows, dict) and isinstance(rows.get("output"), dict):
            rows = rows["output"].get("results", rows)
        else:
            rows = rows.get("results", rows)
        sc = np.zeros(len(head), dtype=np.float32)
        for row in rows:
            j = int(row["index"])
            sc[j] = float(row.get("relevance_score", row.get("score", 0.0)))
        order = np.argsort(-sc, kind="stable")
        return head[order]
    except Exception as e:
        print("rerank failed:", e)
        return head


def summarize(tag: str, out: dict, topk: int) -> None:
    rows, lat = out["rows"], out["lat"]
    n = len(rows)
    if not n:
        print("无可评样本")
        return
    avg = lambda k: sum(r[k] for r in rows) / n  # noqa: E731
    lat_s = sorted(lat)
    print(f"\n== {tag} ==  n={n}")
    print(f"  Recall@1={avg('recall@1'):.4f}  Recall@5={avg('recall@5'):.4f}  "
          f"Recall@{topk}={avg(f'recall@{topk}'):.4f}  MRR@10={avg('mrr@10'):.4f}  "
          f"nDCG@10={avg('ndcg@10'):.4f}")
    print(f"  P50={lat_s[len(lat_s)//2]:.0f}ms  P95={lat_s[int(len(lat_s)*0.95)]:.0f}ms")
    for key in ("qtype", "modality", "n_gold"):
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(str(r[key]), []).append(r)
        if len(groups) < 2:
            continue
        print(f"  by {key}:")
        for g, rs in sorted(groups.items()):
            print(f"    {g:22s} n={len(rs):4d}  R@5={sum(x['recall@5'] for x in rs)/len(rs):.3f}  "
                  f"R@{topk}={sum(x[f'recall@{topk}'] for x in rs)/len(rs):.3f}  "
                  f"nDCG@10={sum(x['ndcg@10'] for x in rs)/len(rs):.3f}")
    miss = sum(1 for r in rows if r["first_rank"] == 0 or r["first_rank"] > topk)
    print(f"  首个 gold 落在 top-{topk} 之外: {miss}/{n} = {miss/n:.1%}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--rebuild", action="store_true")
    e = sub.add_parser("eval")
    e.add_argument("--topk", type=int, default=10)
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--rtop", type=int, default=30)
    e.add_argument("--rerank", default="none", choices=["none", "http"])
    e.add_argument("--rerank-kind", default="tei", choices=["tei", "dashscope"])
    e.add_argument("--rerank-url", default="http://127.0.0.1:8010/rerank")
    e.add_argument("--rerank-model", default="")
    e.add_argument("--rerank-key", default="")
    e.add_argument("--tag", default="")
    a = ap.parse_args()

    if a.cmd == "build":
        docs = load_docs()
        print(f"文档 {len(docs)} 条: {Counter(d['kind'] for d in docs)}")
        json.dump(docs, open(DOCS_JSON, "w"), ensure_ascii=False)
        build_vectors(docs, rebuild=a.rebuild)
        return

    docs = json.load(open(DOCS_JSON)) if os.path.exists(DOCS_JSON) else load_docs()
    vecs = build_vectors(docs)
    assert len(docs) == len(vecs), (len(docs), len(vecs))
    qas = load_qas(a.limit or None)
    print(f"查询 {len(qas)} 条 / 文档 {len(docs)} 条 / 向量维度 {vecs.shape[1]}")
    t0 = time.time()
    bm = BM25(docs)
    print(f"BM25 倒排索引 {time.time()-t0:.1f}s  词表 {len(bm.post)}")
    cfg = ({"url": a.rerank_url, "model": a.rerank_model, "kind": a.rerank_kind,
            "key": a.rerank_key or os.environ.get("RERANK_API_KEY", "")}
           if a.rerank == "http" else None)
    out = evaluate(qas, docs, vecs, bm, a.topk, cfg, a.rtop)
    summarize(a.tag or f"ATM-Bench 代理榜（rerank={'none' if not cfg else a.rerank_kind}）", out, a.topk)


if __name__ == "__main__":
    main()
