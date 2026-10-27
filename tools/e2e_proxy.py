#!/usr/bin/env python3
"""端到端答案评测（代理）：召回的证据 + 回答模型 → 与官方答案比对。

为什么需要它：`gallery_proxy.py` 只量「证据有没有被召回」，但平台最终按**答案正确率**
打分。召回 top-100 里第 30 条命中，与把这条排在 top-5，对答案模型的影响完全不同；
`top_k` 要开 100 还是 20、图片要不要塞 6 张，都得看答案对不对，不能看 Recall。

做法：
  1. 与线上同一条代码路径写入（复用 gallery_proxy 的 round_messages / 预热 / 向量缓存）；
  2. 每个 QA 只 Search 一次（top_k = 最大档），更小的档取前缀 —— 排序一致，等价于
     分别以该 top_k 检索，省掉重复调用；
  3. 证据按「[n] 内容」拼给回答模型，要求只用证据作答、答不出就写 Not mentioned；
  4. 指标：EM（归一化全等）、Contains（gold 是否被包含）、token-F1，按 point × top_k 拆分。

**重要局限（必须在报告里写明）**：本仓库的 Mem-Gallery 只有 `image_caption`，没有真实
图片字节（`data/mem-gallery/image/` 为空）。所以这里的「多模态」是**caption 通道**：
能评「caption 够不够用来答题」，**评不了**「给原图比给 caption 多赚多少」。真实收益
只能在官方 Full 或自备图集上量。

  # 抽样 300 条（按类型分层），sweep 5 档 top_k
  EMBED_MODE=api DASHSCOPE_API_KEY=$KEY python3 tools/e2e_proxy.py --sample 300

  # 只切两档、全量
  EMBED_MODE=api DASHSCOPE_API_KEY=$KEY python3 tools/e2e_proxy.py --top-ks 10,100
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import gallery_proxy as gp  # noqa: E402

ANSWER_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

SYSTEM = (
    "你在读一份「用户过往对话」的记忆条目列表。每个条目以 [n] 开头，内容里可能带 "
    "[image IMG_xxx] 形式的图片说明。\n"
    "只允许使用条目里的信息回答；条目里没有就输出：Not mentioned.\n"
    "规则：\n"
    "1) 问是哪张图/哪条记录 → 直接输出线索标识（例如 D1:IMG_002 或 D1:4），不要解释；\n"
    "2) 问数量 → 只输出阿拉伯数字；\n"
    "3) 问 before/after、有没有 → 只输出 After. / Before. / Yes. / No.；\n"
    "4) 列举题 → 用逗号分隔全部要素，不要遗漏；\n"
    "5) 不要复述问题、不要写推理过程，答案不超过 25 个词。"
)


def evidence(results: list[dict], max_chars: int) -> str:
    lines = []
    for i, it in enumerate(results, 1):
        body = " ".join((it.get("content") or "").split())
        if max_chars and len(body) > max_chars:
            body = body[:max_chars] + "…"
        lines.append(f"[{i}] {body}")
    return "\n".join(lines)


def ask(url: str, key: str, model: str, q: str, ev: str, timeout: float,
        retries: int = 3) -> tuple[str, int]:
    payload = {"model": model, "temperature": 0, "max_tokens": 200,
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": f"记忆条目：\n{ev}\n\n问题：{q}"}]}
    data = json.dumps(payload).encode()
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
            msg = body["choices"][0]["message"]["content"]
            return (msg or "").strip(), int((body.get("usage") or {}).get("total_tokens") or 0)
        except urllib.error.HTTPError as e:
            last = f"HTTP{e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                break
            time.sleep(min(8, 1.5 * (attempt + 1)))
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            time.sleep(min(8, 1.5 * (attempt + 1)))
    raise RuntimeError(f"回答模型调用失败：{last}")


_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def norm(s: str) -> str:
    s = (s or "").strip().lower().replace("‑", "-")
    s = _PUNCT.sub(" ", s)
    return " ".join(s.split())


def score(pred: str, gold: str) -> tuple[int, int, float]:
    """返回 (EM, Contains, token-F1)。"""
    p, g = norm(pred), norm(gold)
    if not g:
        return 0, 0, 0.0
    em = int(p == g)
    contains = int(g in p or (p and p in g))
    ps, gs = set(p.split()), set(g.split())
    inter = len(ps & gs)
    f1 = 0.0 if not inter else 2 * inter / (len(ps) + len(gs))
    return em, contains, f1


def pick(items: list[tuple[object, dict]], sample: int, seed: int = 7) -> set:
    """按 point 分层抽样（**全局**上限）。items = [(key, qa), ...]，返回保留的 key 集合。

    CD/KR/TR 这些类别样本少、恰恰是弱项，纯随机抽样会把它们抽没。
    """
    if not sample or sample >= len(items):
        return {key for key, _ in items}
    import random
    by = collections.defaultdict(list)
    for key, q in items:
        by[q["point"]].append(key)
    rnd = random.Random(seed)
    per = max(1, sample // max(1, len(by)))
    keep = set()
    for _pt, keys in sorted(by.items()):
        rnd.shuffle(keys)
        keep.update(keys[:per])
    return keep


def main() -> int:
    ap = argparse.ArgumentParser(description="端到端答案评测（代理）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个对话文件")
    ap.add_argument("--split", choices=("all", "a", "b"), default="all")
    ap.add_argument("--sample", type=int, default=300, help="按类型分层抽样条数（0=全量）")
    ap.add_argument("--top-ks", default="5,10,20,50,100")
    ap.add_argument("--answer-model", default="qwen-flash")
    ap.add_argument("--answer-url", default=ANSWER_URL)
    ap.add_argument("--answer-key", default=os.environ.get("DASHSCOPE_API_KEY", ""))
    ap.add_argument("--max-chars", type=int, default=600, help="单条证据截断字符数")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--no-warm", action="store_true")
    ap.add_argument("--vec-cache", default=os.path.join(ROOT, "data", ".cache", "gallery-vecs.db"))
    ap.add_argument("--oracle", action="store_true",
                    help="上限诊断：直接喂金标 clue 记忆，量「检索完美时答案能到多少」")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    if not args.answer_key:
        print("需要 --answer-key 或 DASHSCOPE_API_KEY", file=sys.stderr)
        return 2
    ks = [int(x) for x in args.top_ks.split(",")]
    top = max(ks)

    tmp = tempfile.mkdtemp(prefix="mempro-e2e-")
    os.environ["DB_PATH"] = os.path.join(tmp, "e2e.db")
    os.environ.setdefault("STARTUP_CHECK", "0")
    # 代理榜量的是「检索质量」：返回条数必须等于 --top-k，不能被服务默认的 MAX_RESULTS 截断
    # （否则 R@100 会被悄悄砍成 R@20，跟历史基线不可比）。要测收窄效果就显式传环境变量。
    os.environ.setdefault("MAX_RESULTS", "0")
    import server  # noqa: E402
    import store   # noqa: E402
    import embed   # noqa: E402

    store.init()
    files = sorted(glob.glob(os.path.join(gp.DATA, "*.json")))
    if args.split != "all":
        want = 0 if args.split == "a" else 1
        files = [f for i, f in enumerate(files) if i % 2 == want]
    if args.limit:
        files = files[: args.limit]
    if args.vec_cache:
        gp._attach_disk_cache(args.vec_cache, embed)
    dialogs = [(f, json.load(open(f, encoding="utf-8"))) for f in files]
    if not args.no_warm:
        ns = argparse.Namespace(no_images=True, jobs=args.jobs)
        gp._warm(dialogs, server, embed, ns)

    # 1) 写入（与 gallery_proxy 同口径：一次 Add = 一个 session）
    uid_of: dict[str, str] = {}
    round_text: dict[tuple[str, str], str] = {}   # (user, round_id) → 该轮的正文，oracle 用
    for f, d in dialogs:
        uid = "gallery:" + os.path.splitext(os.path.basename(f))[0]
        uid_of[f] = uid
        for s in d["multi_session_dialogues"]:
            msgs = [m for k, r in enumerate(s["dialogues"])
                    for m in gp.round_messages(r, s["date"], k, False)]
            if msgs:
                server.do_add({"request_id": s["session_id"], "user_id": uid,
                               "session_id": s["session_id"], "messages": msgs})
            for r, m in zip(s["dialogues"], range(0, len(msgs))):
                body = " ".join(str(x) for x in [r.get("user"), r.get("assistant")] if x)
                for img_id, cap in zip(r.get("image_id") or [], r.get("image_caption") or []):
                    body += f" [image {img_id}] {cap}"
                round_text[(uid, r["round"])] = body

    # 2) 每条 QA 只 Search 一次（最大档），小档取前缀
    kept = pick([((f, i), q) for f, d in dialogs
                 for i, q in enumerate(d["human-annotated QAs"])], args.sample)
    tasks = []
    for f, d in dialogs:
        for i, q in enumerate(d["human-annotated QAs"]):
            if kept and (f, i) not in kept:
                continue
            if args.oracle:
                items = [{"content": round_text.get((uid_of[f], c), "")}
                         for c in (q.get("clue") or [])]
                tasks.append((q, items))
                continue
            out = server.do_search({"query": gp.query_content(q, False),
                                    "user_id": uid_of[f], "top_k": top})
            tasks.append((q, out["data"]))
    mode = "oracle（金标 clue 直接作证据）" if args.oracle else f"top_k 档位 {ks}"
    print(f"QA {len(tasks)} 条，{mode}，回答模型 {args.answer_model}", flush=True)

    # 3) 并发调用回答模型
    lock = threading.Lock()
    stats: dict[tuple[str, int], dict[str, float]] = collections.defaultdict(
        lambda: {"n": 0, "em": 0, "contains": 0, "f1": 0.0})
    tokens = [0]
    fails = [0]
    done = [0]
    started = time.time()

    def work(item):
        q, hits = item
        for k in ks:
            try:
                pred, ntok = ask(args.answer_url, args.answer_key, args.answer_model,
                                 q["question"], evidence(hits[:k], args.max_chars), 60)
            except Exception as e:  # noqa: BLE001
                with lock:
                    fails[0] += 1
                print(f"  ⚠️ {type(e).__name__}: {e}", file=sys.stderr)
                continue
            em, contains, f1 = score(pred, q.get("answer") or "")
            with lock:
                a = stats[(q["point"], k)]
                a["n"] += 1
                a["em"] += em
                a["contains"] += contains
                a["f1"] += f1
                tokens[0] += ntok
                done[0] += 1
                if done[0] % 200 == 0:
                    print(f"  … {done[0]}/{len(tasks) * len(ks)}"
                          f"（{time.time() - started:.0f}s，{tokens[0] / 1000:.0f}k tok）",
                          flush=True)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        list(ex.map(work, tasks))

    # 4) 报告
    print(f"\n端到端代理（caption 通道，无真实图片）  模型={args.answer_model} "
          f"证据截断={args.max_chars} 字符")
    print(f"调用 {done[0]} 次（失败 {fails[0]}）  用量 {tokens[0] / 1000:.0f}k tok  "
          f"耗时 {time.time() - started:.0f}s")
    hdr = "类型  " + "  ".join(f"top{k:<3}" for k in ks)
    print(hdr)
    points = sorted({p for p, _ in stats})
    agg = {k: {"n": 0, "em": 0, "contains": 0, "f1": 0.0} for k in ks}
    for p in points:
        cells = []
        for k in ks:
            a = stats[(p, k)]
            if not a["n"]:
                cells.append(" " * 6)
                continue
            cells.append(f"{a['em'] / a['n']:.2f} ")
            for key in agg[k]:
                agg[k][key] += a[key]
        print(f"{p:<5} " + "  ".join(cells))
    total = []
    for k in ks:
        a = agg[k]
        total.append(f"{a['em'] / max(1, a['n']):.3f}")
    print("ALL    " + "  ".join(f"{v:<6}" for v in total) + "   (EM)")
    for label, key in (("Contains", "contains"), ("token-F1", "f1")):
        row = []
        for k in ks:
            a = agg[k]
            row.append(f"{a[key] / max(1, a['n']):.3f}")
        print(f"{label:<8} " + "  ".join(f"{v:<6}" for v in row))
    best = max(ks, key=lambda k: agg[k]["em"] / max(1, agg[k]["n"]))
    print(f"\n最优 top_k = {best}（EM 最高）；更大的档只增加 token 与延迟")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"model": args.answer_model, "top_ks": ks, "tokens": tokens[0],
                       "fails": fails[0],
                       "stats": {f"{p}@{k}": v for (p, k), v in stats.items()}},
                      fh, ensure_ascii=False, indent=1)
        print(f"报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
