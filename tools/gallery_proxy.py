#!/usr/bin/env python3
"""Mem-Gallery 本地代理榜：走与线上完全相同的 Add / Search 代码路径（进程内调用）。

为什么：AML 多模态赛道公开的两个基准之一就是 Mem-Gallery（F1，图集式记忆检索）。
仓库里有 20 段对话 / 3,962 轮 / 1,711 条 QA（data/mem-gallery/dialog），
足够在本地无额度地比较「写入粒度 / 去重 / 时态 / 预算」等改动对证据召回的影响。

做法：
  - 每段对话一个 user_id；每一轮（round）一次 Add，request_id = round 编号，
    messages = [user, assistant]，带 session 日期时间戳；
  - 有图片的轮次按平台口径发送「原图 + caption」：caption 作为 text part，
    图片用确定性的占位 PNG（真图不在仓库里，占位图只用于检验图片去重/预算路径）；
  - QA 的 question_image 同样转成 caption 文本 + 占位图作为 query 内容数组；
  - 指标：证据轮次（clue）在返回前 k 条中的 Recall@k、全部命中率 Hit-all@k、MRR；
    按 point（FR/VR/VS/TTL/MR/TR/KR/CD；AR=拒答题无 clue，单独统计返回量）拆分。

这是**检索代理指标**，不是官方 F1（官方还要过统一 Answer + Judge）。
hash 模式只能看方向；正式结论请用 EMBED_MODE=api（text-embedding-v4）。

  python3 tools/gallery_proxy.py                       # 全部 20 段，hash 模式
  python3 tools/gallery_proxy.py --limit 5 --k 10,20,100
  EMBED_MODE=api python3 tools/gallery_proxy.py --report gallery.json
"""
from __future__ import annotations

import argparse
import base64
import calendar
import glob
import hashlib
import json
import os
import sys
import tempfile
import time
import zlib
import struct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))
DATA = os.path.join(ROOT, "data", "mem-gallery", "dialog")


def _png(seed: str) -> str:
    """1×1 PNG，像素颜色由 seed 决定 ⇒ 不同图片字节不同、同图字节相同。"""
    rgb = hashlib.sha256(seed.encode()).digest()[:3]
    raw = b"\x00" + rgb
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _ts(date: str, k: int) -> int:
    t = calendar.timegm(time.strptime(date, "%Y-%m-%d"))
    return t * 1000 + k * 60_000


def round_messages(r: dict, date: str, k: int, with_images: bool) -> list[dict]:
    user_parts = [{"type": "text", "text": r["user"]}] if r.get("user") else []
    for img_id, cap in zip(r.get("image_id") or [], r.get("image_caption") or []):
        user_parts.append({"type": "text", "text": f"[image {img_id}] {cap}"})
        if with_images:
            user_parts.append({"type": "image_url", "image_url": {"url": _png(img_id)}})
    msgs = []
    if user_parts:
        content = user_parts if len(user_parts) > 1 else user_parts[0]["text"]
        msgs.append({"role": "user", "content": content, "timestamp": _ts(date, k)})
    if r.get("assistant"):
        msgs.append({"role": "assistant", "content": r["assistant"], "timestamp": _ts(date, k) + 1})
    return msgs


def query_content(q: dict, with_images: bool):
    if not q.get("question_image"):
        return q["question"]
    parts = [{"type": "text", "text": q["question"]}]
    cap = q.get("image_caption")
    if isinstance(cap, list):
        cap = " ".join(cap)
    if cap:
        parts.append({"type": "text", "text": f"[image] {cap}"})
    if with_images:
        parts.append({"type": "image_url", "image_url": {"url": _png(q["question_image"])}})
    return parts


def main() -> int:
    ap = argparse.ArgumentParser(description="Mem-Gallery retrieval proxy (in-process Add/Search)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N dialogue files")
    ap.add_argument("--k", default="5,10,20,100", help="cut-offs to report")
    ap.add_argument("--top-k", type=int, default=100, help="Search top_k (official Full = 100)")
    ap.add_argument("--no-images", action="store_true", help="send captions only")
    ap.add_argument("--add-by", choices=("round", "session"), default="session",
                    help="one Add per round, or one Add per session (closer to platform chunking)")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    ks = [int(x) for x in args.k.split(",")]

    tmp = tempfile.mkdtemp(prefix="mempro-gallery-")
    os.environ["DB_PATH"] = os.path.join(tmp, "gallery.db")
    os.environ.setdefault("STARTUP_CHECK", "0")
    import server  # noqa: E402  (reads env at import)
    import store   # noqa: E402
    import embed   # noqa: E402

    store.init()
    files = sorted(glob.glob(os.path.join(DATA, "*.json")))
    if args.limit:
        files = files[: args.limit]

    per_point: dict[str, dict[str, float]] = {}
    rows = []
    add_ms = search_ms = 0.0
    n_add = n_search = 0
    started = time.time()
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        uid = "gallery:" + os.path.splitext(os.path.basename(f))[0]
        msg_owner: dict[tuple[str, str, int], str] = {}
        for s in d["multi_session_dialogues"]:
            per_round = []
            for k, r in enumerate(s["dialogues"]):
                msgs = round_messages(r, s["date"], k, not args.no_images)
                if msgs:
                    per_round.append((r["round"], msgs))
            if args.add_by == "session":
                batches = [(s["session_id"], [m for _, ms in per_round for m in ms],
                            [rid for rid, ms in per_round for _ in ms])]
            else:
                batches = [(rid, ms, [rid] * len(ms)) for rid, ms in per_round]
            for req_id, msgs, owners in batches:
                if not msgs:
                    continue
                t0 = time.perf_counter()
                server.do_add({"request_id": req_id, "user_id": uid,
                               "session_id": s["session_id"], "messages": msgs})
                add_ms += (time.perf_counter() - t0) * 1000
                n_add += 1
                for mi, owner in enumerate(owners):
                    msg_owner[(s["session_id"], req_id, mi)] = owner
        # 记忆 id → 证据轮次：按 server 的确定性 id 规则反查（message 下标 → round）
        id2round = {}
        for (sess, req_id, mi), owner in msg_owner.items():
            for si in range(64):
                id2round[server.det_id(uid, sess, req_id, mi, si)] = owner

        for q in d["human-annotated QAs"]:
            t0 = time.perf_counter()
            out = server.do_search({"query": query_content(q, not args.no_images),
                                    "user_id": uid, "top_k": args.top_k})
            search_ms += (time.perf_counter() - t0) * 1000
            n_search += 1
            got = [id2round.get(it["id"]) for it in out["data"]]
            gold = set(q.get("clue") or [])
            acc = per_point.setdefault(q["point"], {"n": 0, "mrr": 0.0, "returned": 0.0,
                                                    **{f"R@{k}": 0.0 for k in ks},
                                                    **{f"All@{k}": 0.0 for k in ks}})
            acc["n"] += 1
            acc["returned"] += len(got)
            if not gold:
                continue
            first = next((j for j, rid in enumerate(got) if rid in gold), None)
            acc["mrr"] += 0.0 if first is None else 1.0 / (first + 1)
            for k in ks:
                hit = gold & set(got[:k])
                acc[f"R@{k}"] += len(hit) / len(gold)
                acc[f"All@{k}"] += 1.0 if hit == gold else 0.0
            rows.append({"file": os.path.basename(f), "point": q["point"], "q": q["question"],
                         "gold": sorted(gold), "top10": got[:10]})

    def fmt_row(name, a):
        n = a["n"]
        if name == "AR":
            return f"{name:<5} n={n:<4} (拒答题，无 clue)  avg returned={a['returned'] / n:.1f}"
        cells = "  ".join(f"R@{k}={a[f'R@{k}'] / n:.3f}" for k in ks)
        return f"{name:<5} n={n:<4} MRR={a['mrr'] / n:.3f}  {cells}  All@{ks[-1]}={a[f'All@{ks[-1]}'] / n:.3f}"

    print(f"Mem-Gallery proxy  files={len(files)} embed={embed.info()['mode']} "
          f"images={'off' if args.no_images else 'placeholder'}  top_k={args.top_k}")
    print(f"Add {n_add} 次 avg {add_ms / max(1, n_add):.1f} ms | Search {n_search} 次 "
          f"avg {search_ms / max(1, n_search):.1f} ms | 总耗时 {time.time() - started:.0f}s")
    total = {"n": 0, "mrr": 0.0, "returned": 0.0, **{f"R@{k}": 0.0 for k in ks},
             **{f"All@{k}": 0.0 for k in ks}}
    for name in sorted(per_point):
        a = per_point[name]
        print("  " + fmt_row(name, a))
        if name != "AR":
            for key in total:
                total[key] += a[key]
    print("  " + fmt_row("ALL", total).replace("ALL  ", "ALL* ") + "   (*不含 AR)")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"per_point": per_point, "total": total, "rows": rows}, fh,
                      ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
