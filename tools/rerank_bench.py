"""精排模型横向评测（同一语料 / 同一候选集 / 同一指标）。

指标：Recall@10、MRR@10、nDCG@10、P50 延迟（每 query）
候选集：dense(text-embedding-v4) + BM25 → RRF 取 top-K，再交由各精排器重排

语料为**确定性合成**（含同实体干扰项、时间更新、中英混排），
用于在拿不到 ATM-Bench/Mem-Gallery 数据时做**相对**比较，不可当绝对分数。

用法：
  DASHSCOPE_API_KEY=sk-xxx .venv/bin/python tools/rerank_bench.py \
      --models none,bge,qwen3,gte-api --topk 20 --docs 2000 --queries 60
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

import budget  # noqa: E402
import embed  # noqa: E402
import retrieve  # noqa: E402

DS_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
GTE_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

CITIES = ["Berlin", "Paris", "Tokyo", "Lisbon", "Shanghai", "Toronto", "Oslo", "Bogota"]
CITIES_ZH = ["柏林", "巴黎", "东京", "里斯本", "上海", "多伦多", "奥斯陆", "波哥大"]
JOBS = ["data engineer", "nurse", "architect", "chef", "teacher", "pilot"]
PETS = ["corgi", "tabby cat", "parrot", "beagle", "rabbit"]
FOODS = ["peanuts", "shellfish", "gluten", "dairy"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
NAMES = ["Alice", "Bruno", "Chen", "Dana", "Eli", "Fatima", "Giorgio", "Hana",
         "Ivan", "Jun", "Kira", "Luis", "Maya", "Nils", "Olga", "Pablo"]


def build(n_docs: int, n_queries: int, seed: int = 7):
    rnd = random.Random(seed)
    docs, queries, qid = [], [], 0
    per_conv = max(20, n_docs // 40)
    for c in range(40):
        name = f"{NAMES[c % len(NAMES)]}-{c}"  # 唯一化：避免跨会话同文
        city0 = rnd.choice(CITIES)
        city1 = rnd.choice([x for x in CITIES if x != city0])
        job, pet, food = rnd.choice(JOBS), rnd.choice(PETS), rnd.choice(FOODS)
        day = rnd.choice(DAYS)
        # 事实
        f = [
            f"{name} lives in {city0}.",
            f"{name} works as a {job}.",
            f"{name} has a {pet} named Momo.",
            f"{name} is allergic to {food}.",
            f"{name} usually has a team meeting on {day}.",
        ]
        # 时间更新（旧事实 + 新事实）
        upd = (f"{name} moved from {city0} to {city1} last month.",
               f"{name} now lives in {city1}.")
        # 干扰：同实体不同值
        dis = [
            f"{name}'s colleague thinks {name} lives in {rnd.choice(CITIES)}.",
            f"{name} once visited {rnd.choice(CITIES)} for a conference.",
            f"{name} said the meeting might move to {rnd.choice(DAYS)}.",
        ]
        # 硬干扰：同主语 + 同谓词 + 不同取值（纯相似度会排错）
        hard = [f"{name} previously lived in {x}." for x in CITIES if x not in (city0, city1)]
        hard += [f"{name} is not allergic to {x}." for x in FOODS if x != food]
        hard += [f"{name} used to work as a {x}." for x in JOBS if x != job]
        hard += [f"{name}'s friend said {name} might move to {x} next year." for x in CITIES[:4]]
        block = f + [upd[0], upd[1]] + dis + hard
        for t in block:
            docs.append({"id": f"d{len(docs)}", "text": t, "conv": c})
        # 噪声填充
        while len([d for d in docs if d["conv"] == c]) < per_conv:
            docs.append({"id": f"d{len(docs)}",
                         "text": f"Small talk with {name} about {rnd.choice(['weather', 'food', 'travel', 'music'])}, "
                                 f"mentioning {rnd.choice(CITIES)} and {rnd.choice(DAYS)}.",
                         "conv": c})
        # 查询：gold = 更新后的那条（考时间正确）
        qs = [
            (f"Where is {name} currently based?", upd[1]),
            (f"Which city did {name} leave?", upd[0]),
            (f"What is {name}'s current occupation?", f[1]),
            (f"Which animal does {name} keep at home?", f[2]),
            (f"Which ingredient must {name} avoid?", f[3]),
        ]
        rnd.shuffle(qs)
        for q, g in qs[: max(1, n_queries // 40)]:
            gold = [d["id"] for d in docs if d["conv"] == c and d["text"] == g]
            if gold:
                queries.append({"qid": qid, "query": q, "gold": gold, "conv": c})
                qid += 1
    # 中文查询（同 gold，考跨语言）
    for qy in queries[: max(1, n_queries // 5)]:
        zh = qy["query"].replace("Where does", "住在哪里？").replace("What does", "做什么工作？")
        queries.append({"qid": qid, "query": zh, "gold": qy["gold"], "conv": qy["conv"]})
        qid += 1
    return docs, queries[:n_queries] if n_queries < len(queries) else queries


# ---------------- 精排器 ----------------
class NoRerank:
    name = "none (RRF)"

    def score(self, q, docs):
        return [0.0] * len(docs)


class LocalSeqCls:
    def __init__(self, path, name, maxlen=256, batch=32, threads=12):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(threads)
        torch.set_grad_enabled(False)
        self.name, self.batch, self.maxlen = name, batch, maxlen
        self.tok = AutoTokenizer.from_pretrained(path)
        self.m = AutoModelForSequenceClassification.from_pretrained(path).eval()

    def score(self, q, docs):
        import torch
        out = []
        for i in range(0, len(docs), self.batch):
            enc = self.tok([[q, d] for d in docs[i:i + self.batch]], padding=True,
                           truncation=True, max_length=self.maxlen, return_tensors="pt")
            out += torch.sigmoid(self.m(**enc).logits.view(-1)).tolist()
        return out


class Qwen3Rerank:
    name = "Qwen3-Reranker-0.6B"

    def __init__(self, path, maxlen=512, batch=8, threads=12):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        torch.set_num_threads(threads)
        torch.set_grad_enabled(False)
        self.torch, self.batch, self.maxlen = torch, batch, maxlen
        self.tok = AutoTokenizer.from_pretrained(path, padding_side="left")
        self.m = AutoModelForCausalLM.from_pretrained(path).eval()
        self.yes = self.tok.convert_tokens_to_ids("yes")
        self.no = self.tok.convert_tokens_to_ids("no")
        self.pre = ("<|im_start|>system\nJudge whether the Document meets the requirements based on "
                    "the Query and the Instruct provided. Note that the answer can only be \"yes\" or \"no\"."
                    "<|im_end|>\n<|im_start|>user\n<Instruct>: Given a memory query, retrieve relevant memories that answer it\n")

    def score(self, q, docs):
        torch = self.torch
        out = []
        for i in range(0, len(docs), self.batch):
            chunk = docs[i:i + self.batch]
            prompts = [f"{self.pre}<Query>: {q}\n<Document>: {d}<|im_end|>\n<|im_start|>assistant\n"
                       for d in chunk]
            enc = self.tok(prompts, padding=True, truncation=True, max_length=self.maxlen,
                           return_tensors="pt")
            logits = self.m(**enc).logits[:, -1, :]
            pair = torch.stack([logits[:, self.no], logits[:, self.yes]], dim=1)
            out += torch.softmax(pair, dim=1)[:, 1].tolist()
        return out


class GteApi:
    name = "gte-rerank-v2 (API)"

    def score(self, q, docs):
        body = json.dumps({"model": "gte-rerank-v2",
                           "input": {"query": q, "documents": docs},
                           "parameters": {"return_documents": False}}).encode()
        req = urllib.request.Request(GTE_URL, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {DS_KEY}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        sc = [0.0] * len(docs)
        for row in d["output"]["results"]:
            sc[row["index"]] = row["relevance_score"]
        return sc


# ---------------- 指标 ----------------
def ndcg(ranked_ids, gold, k=10):
    dcg = sum(1 / math.log2(i + 2) for i, g in enumerate(ranked_ids[:k]) if g in gold)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / idcg if idcg else 0.0


def metrics(ranked_ids, gold, k=10):
    top = ranked_ids[:k]
    hit = sum(1 for g in gold if g in top)
    rr = next((1 / (i + 1) for i, g in enumerate(ranked_ids) if g in gold), 0.0)
    return hit / len(gold), rr, ndcg(ranked_ids, set(gold), k)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="none,bge,qwen3,gte-api")
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--docs", type=int, default=2000)
    ap.add_argument("--queries", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    docs, queries = build(a.docs, a.queries, a.seed)
    print(f"语料 {len(docs)} 条 / 查询 {len(queries)} 条 / 候选 top-{a.topk}\n")

    if embed.MODE == "api" and not DS_KEY:
        sys.exit("EMBED_MODE=api 需要 DASHSCOPE_API_KEY")

    print("• 计算查询向量与语料向量 …", flush=True)
    dv = embed.embed([d["text"] for d in docs])
    for d, v in zip(docs, dv):
        d["vec"] = embed.pack(v)

    cand = []
    t0 = time.time()
    for qt in queries:
        # 按 conv 取子集（模拟 user_id 隔离）
        pool = [d for d in docs if d["conv"] == qt["conv"]]
        qv = embed.embed([qt["query"]])[0]
        ranked = retrieve.hybrid(qv, qt["query"], pool)
        ranked = retrieve.dedup(ranked, pool)
        cand.append((qt, pool, ranked[: a.topk]))
    print(f"  粗排完成 {time.time()-t0:.1f}s\n", flush=True)

    scorers = []
    for m in a.models.split(","):
        m = m.strip()
        if m == "none":
            scorers.append(NoRerank())
        elif m == "bge":
            scorers.append(LocalSeqCls(f"{ROOT}/models/bge-reranker-v2-m3", "bge-reranker-v2-m3"))
        elif m == "jina":
            scorers.append(LocalSeqCls(f"{ROOT}/models/jina-reranker-v2", "jina-reranker-v2-base"))
        elif m == "qwen3":
            scorers.append(Qwen3Rerank(f"{ROOT}/models/qwen3-reranker-0.6b"))
        elif m == "gte-api":
            if not DS_KEY:
                print("跳过 gte-api（无 key）")
                continue
            scorers.append(GteApi())
        else:
            print(f"未知模型 {m}")
    del dv

    rows = []
    for sc in scorers:
        rec = mrr = ndcg_ = 0.0
        lat = []
        for qt, pool, head in cand:
            texts = [pool[i]["text"] for i, _ in head]
            t = time.time()
            scores = sc.score(qt["query"], texts)
            lat.append((time.time() - t) * 1000)
            order = sorted(range(len(head)), key=lambda j: (-scores[j], j))
            ids = [pool[head[j][0]]["id"] for j in order]
            r, m, n = metrics(ids, qt["gold"])
            rec += r; mrr += m; ndcg_ += n
        n = len(cand)
        lat.sort()
        rows.append([sc.name, rec / n, mrr / n, ndcg_ / n,
                     lat[len(lat) // 2], lat[int(len(lat) * 0.95)] if len(lat) > 2 else lat[-1]])
        print(f"  ✓ {sc.name}  nDCG@10={ndcg_/n:.4f}", flush=True)

    print("\n| 精排器 | Recall@10 | MRR@10 | nDCG@10 | P50 ms | P95 ms |")
    print("|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: -x[3]):
        print(f"| {r[0]} | {r[1]:.4f} | {r[2]:.4f} | {r[3]:.4f} | {r[4]:.0f} | {r[5]:.0f} |")

    base = next((x for x in rows if "none" in x[0]), None)
    if base:
        print("\n相对 RRF 基线的 nDCG@10 提升：")
        for r in sorted(rows, key=lambda x: -x[3]):
            print(f"  {r[0]:26s} {'+' if r[3]>=base[3] else ''}{(r[3]-base[3])*100:.1f}%")


if __name__ == "__main__":
    main()
