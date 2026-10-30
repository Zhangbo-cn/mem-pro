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
import threading
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


IMG_DIR = os.path.join(ROOT, "data", "mem-gallery", "image")
_REAL = {"on": False, "topic": "", "missing": 0, "oversize": 0, "bytes": 0}
_MIME = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}


def _real(rel: str) -> str | None:
    """真实图片 → data URI。缺文件或超过官方单图 10 MiB 上限时返回 None（平台不会发这种图）。"""
    path = os.path.join(IMG_DIR, _REAL["topic"], rel)
    for cand in (path, path + ".jpg", path + ".png"):
        if os.path.isfile(cand):
            path = cand
            break
    else:
        _REAL["missing"] += 1
        return None
    raw = open(path, "rb").read()
    if len(raw) > 10 * 1024 * 1024:
        _REAL["oversize"] += 1
        return None
    _REAL["bytes"] += len(raw)
    ext = path.rsplit(".", 1)[-1].lower()
    return f"data:image/{_MIME.get(ext, 'jpeg')};base64," + base64.b64encode(raw).decode()


def _image(img_id: str) -> str | None:
    """--real-images 时读 data/mem-gallery/image/<topic>/D1_IMG_001.jpg，否则 1×1 占位图。"""
    if not _REAL["on"]:
        return _png(img_id)
    if img_id.startswith("../image/"):          # QA 的 question_image 是相对路径
        return _real(img_id.split("/", 3)[-1])
    return _real(img_id.replace(":", "_"))


def _ts(date: str, k: int) -> int:
    t = calendar.timegm(time.strptime(date, "%Y-%m-%d"))
    return t * 1000 + k * 60_000


def round_messages(r: dict, date: str, k: int, with_images: bool) -> list[dict]:
    user_parts = [{"type": "text", "text": r["user"]}] if r.get("user") else []
    for img_id, cap in zip(r.get("image_id") or [], r.get("image_caption") or []):
        user_parts.append({"type": "text", "text": f"[image {img_id}] {cap}"})
        if with_images and (url := _image(img_id)):
            user_parts.append({"type": "image_url", "image_url": {"url": url}})
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
    if with_images and (url := _image(q["question_image"])):
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _attach_disk_cache(path: str, embed) -> None:
    """把 embed.embed 包一层 SQLite 缓存：同一 (模型,维度,文本) 跨进程只算一次。

    复现性靠它：同一次提交的语料/查询向量恒定，A/B 只重新计算改了的那部分。
    """
    import sqlite3

    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS vec("
               "k TEXT PRIMARY KEY, v BLOB, hits INTEGER DEFAULT 0)")
    db.commit()
    tag = f"{embed.MODE}:{embed.MODEL}:{embed.DIM}"
    lock = threading.Lock()
    real = embed.embed

    def cached(texts: list[str]) -> list[list[float]]:
        keys = [hashlib.sha256(f"{tag}\x00{t}".encode()).hexdigest() for t in texts]
        out: list[list[float] | None] = [None] * len(texts)
        with lock:
            for start in range(0, len(keys), 400):
                chunk = keys[start:start + 400]
                marks = ",".join("?" * len(chunk))
                for k, v in db.execute(
                        f"SELECT k, v FROM vec WHERE k IN ({marks})", chunk).fetchall():
                    out[keys.index(k)] = struct.unpack(f"{embed.DIM}f", v)
        todo = [i for i, v in enumerate(out) if v is None]
        if todo:
            for i, v in zip(todo, real([texts[i] for i in todo])):
                out[i] = v
            rows = [(keys[i], struct.pack(f"{embed.DIM}f", *(out[i] or []))) for i in todo]
            with lock:
                db.executemany("INSERT OR REPLACE INTO vec(k, v) VALUES(?,?)", rows)
                db.commit()
        return [v for v in out if v is not None]

    cached.cache_info = lambda: {"vec_cache": path, **_count(db)}  # type: ignore[attr-defined]
    embed.embed = cached


def _count(db) -> dict:
    row = db.execute("SELECT COUNT(*) FROM vec").fetchone()
    return {"vec_cached": int(row[0])}


def _warm(dialogs, server, embed, args) -> None:
    """按 server 的真实规则预计算「将被向量化的文本」，并行灌进缓存。

    写入侧直接复用 server.plan_add（与 do_add 逐字一致），查询侧走 norm_content。
    """
    import concurrent.futures

    texts: list[str] = []
    for f, d in dialogs:
        _REAL["topic"] = os.path.splitext(os.path.basename(f))[0]
        for s in d["multi_session_dialogues"]:
            msgs = [m for k, r in enumerate(s["dialogues"])
                    for m in round_messages(r, s["date"], k, not args.no_images)]
            if msgs:
                _, inputs = server.plan_add({"request_id": s["session_id"], "user_id": "warm",
                                             "session_id": s["session_id"], "messages": msgs})
                texts.extend(inputs)
        for q in d["human-annotated QAs"]:
            qt, _ = server.norm_content(query_content(q, not args.no_images), "warm")
            texts.append(qt)

    texts = list(dict.fromkeys(texts))
    print(f"预热 {len(texts)} 条文本（jobs={args.jobs}, embed={embed.MODE}）…", flush=True)
    t0 = time.time()
    size = max(1, int(embed.BATCH))
    chunks = [texts[i:i + size] for i in range(0, len(texts), size)]
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for n in ex.map(lambda c: (embed.embed(c), len(c))[1], chunks):
            done += n
    info = getattr(embed.embed, "cache_info", None)
    print(f"预热完成 {done} 条 / {time.time() - t0:.0f}s"
          f" {info() if info else ''}", flush=True)

def main() -> int:
    ap = argparse.ArgumentParser(description="Mem-Gallery retrieval proxy (in-process Add/Search)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N dialogue files")
    ap.add_argument("--k", default="5,10,20,100", help="cut-offs to report")
    ap.add_argument("--top-k", type=int, default=100, help="Search top_k (official Full = 100)")
    ap.add_argument("--no-images", action="store_true", help="send captions only")
    ap.add_argument("--add-by", choices=("round", "session"), default="session",
                    help="one Add per round, or one Add per session (closer to platform chunking)")
    ap.add_argument("--no-warm", action="store_true",
                    help="skip the parallel embedding warm-up (api mode: warms cache so Adds are local)")
    ap.add_argument("--vec-cache", default=os.path.join(ROOT, "data", ".cache", "gallery-vecs.db"),
                    help="persistent embedding cache; the same run repeats for free (empty = off)")
    ap.add_argument("--jobs", type=int, default=8, help="parallel warm-up workers")
    ap.add_argument("--split", choices=("all", "a", "b"), default="all",
                    help="a/b: alternating files — tune on one half, verify on the other")
    ap.add_argument("--real-images", action="store_true",
                    help="用 data/mem-gallery/image/ 下的真实图片（先下载，见 README §十四）"
                         "替换 1×1 占位图：测体积、预算裁剪与延迟")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    _REAL["on"] = args.real_images
    ks = [int(x) for x in args.k.split(",")]

    tmp = tempfile.mkdtemp(prefix="mempro-gallery-")
    os.environ["DB_PATH"] = os.path.join(tmp, "gallery.db")
    os.environ.setdefault("STARTUP_CHECK", "0")
    # 代理榜量的是「检索质量」：返回条数必须等于 --top-k，不能被服务默认的 MAX_RESULTS 截断
    # （否则 R@100 会被悄悄砍成 R@20，跟历史基线不可比）。要测收窄效果就显式传环境变量。
    os.environ.setdefault("MAX_RESULTS", "0")
    import server  # noqa: E402  (reads env at import)
    import store   # noqa: E402
    import embed   # noqa: E402

    store.init()
    files = sorted(glob.glob(os.path.join(DATA, "*.json")))
    if args.split != "all":
        # 交替取：两个半集的主题分布尽量接近，避免「a 全是旅行、b 全是编程」
        want = 0 if args.split == "a" else 1
        files = [f for i, f in enumerate(files) if i % 2 == want]
    if args.limit:
        files = files[: args.limit]

    if args.vec_cache:
        _attach_disk_cache(args.vec_cache, embed)
    dialogs = [(f, json.load(open(f, encoding="utf-8"))) for f in files]
    if not args.no_warm:
        # Add 是同步的：不预热就是「等 200 ms → 写 → 等 200 ms」。先并行把要用到的
        # 文本全灌进进程内缓存（以及磁盘缓存），后面的 Add/Search 全是本地命中。
        _warm(dialogs, server, embed, args)

    per_point: dict[str, dict[str, float]] = {}
    rows = []
    add_ms = search_ms = 0.0
    n_add = n_search = 0
    started = time.time()
    resp_bytes: list[int] = []
    resp_images: list[int] = []
    for f, d in dialogs:
        uid = "gallery:" + os.path.splitext(os.path.basename(f))[0]
        _REAL["topic"] = os.path.splitext(os.path.basename(f))[0]
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
            if args.real_images:
                resp_bytes.append(len(json.dumps(out, ensure_ascii=False).encode()))
                resp_images.append(sum(1 for it in out["data"] if isinstance(it["content"], list)
                                       for p in it["content"] if p.get("type") == "image_url"))
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

    print(f"Mem-Gallery proxy  split={args.split} files={len(files)} embed={embed.info()['mode']} "
          f"images={'off' if args.no_images else ('real' if args.real_images else 'placeholder')}  top_k={args.top_k}  "
          f"max_results={server.MAX_RESULTS}  config_hash={server._config_fingerprint()}")
    print(f"Add {n_add} 次 avg {add_ms / max(1, n_add):.1f} ms | Search {n_search} 次 "
          f"avg {search_ms / max(1, n_search):.1f} ms | 总耗时 {time.time() - started:.0f}s")
    if args.real_images and resp_bytes:
        rb = sorted(resp_bytes)
        ri = sorted(resp_images)
        print(f"真图: 写入 {_REAL['bytes'] / 2**20:.0f} MiB；缺失 {_REAL['missing']} 张、"
              f"超 10 MiB 跳过 {_REAL['oversize']} 张（平台不会发）")
        print(f"Search 响应体积 p50 {rb[len(rb) // 2] / 2**20:.2f} MiB / "
              f"p95 {rb[int(len(rb) * .95)] / 2**20:.2f} MiB / max {rb[-1] / 2**20:.2f} MiB / "
              f"均值 {sum(rb) / len(rb) / 2**20:.2f} MiB"
              f"（上限 30 MiB）；每次返回图片 p50 {ri[len(ri) // 2]} / max {ri[-1]} 张")
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
            json.dump({"config_hash": server._config_fingerprint(),
                       "max_results": server.MAX_RESULTS, "top_k": args.top_k,
                       "per_point": per_point, "total": total, "rows": rows}, fh,
                      ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
