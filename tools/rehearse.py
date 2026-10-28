#!/usr/bin/env python3
"""远端评测预演：直接打公网 HTTPS 的 Add / Search，找「平台真打进来时」会卡在哪。

前两个代理工具都跑在进程内（`gallery_proxy` / `e2e_proxy`），量的是检索和答案质量；
这个工具**只走网络**，量的是上线后才暴露的那一层：

  1. 写入口径：按官方「单次 Add ≤ 30 MiB、单图 ≤ 10 MiB」把会话切成多个 Add，量真实上行字节；
  2. 并发：按**申报并发**发请求（默认 2 = 2 Mbit 带宽下的申报值），量 p50/p95/最大延迟；
  3. 崩溃面：记录 4xx/5xx/超时的分布和首个响应体片段 —— 422（单图超限）、413（body 超限）、
     502/504（反代超时）都在这里现形；
  4. 响应体积：Search 返回的 Base64 图片是出网流量主项，量 p50/p95/max 与 30 MiB 上限的距离；
  5. 外推：按实测吞吐推全量 1,711 条要多久、要花多少流量费。

用法：
  python3 tools/rehearse.py --url https://113.44.133.30 --key "$K" --limit 3 --sample 60
  python3 tools/rehearse.py --url https://113.44.133.30 --key "$K" --limit 0 --sample 0   # 全量预演
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import threading
import time
import http.client
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gallery_proxy as gp  # noqa: E402

DATA = os.path.join(ROOT, "data", "mem-gallery", "dialog")
MIB = 1024 * 1024


_STALL = {"n": 0}


def _post(url: str, key: str, payload: dict, timeout: float,
          retries: int = 2) -> tuple[int, bytes, float]:
    """超时/连接异常时重试：平台对同一 request_id 最多重试 32 次，我方也必须容忍瞬时 stall。

    实测过一次：EIP 上没有任何流量进来，但客户端 socket 一个字节都收不到（Caddy 侧 Send-Q 3.4 MB
    越积越高），卡了 3 分钟。这类瞬时卡死不能让它把整个预演僵住。
    """
    for attempt in range(retries + 1):
        code, body, lat = _post_once(url, key, payload, timeout)
        if code != 0:
            return code, body, lat
        _STALL["n"] += 1
        time.sleep(1 + attempt)
    return code, body, lat


_TL = threading.local()
_KEEPALIVE = {"on": True}


def _conn(host: str, port: int, timeout: float) -> http.client.HTTPSConnection:
    """每线程复用一条 TLS 连接：平台客户端也是长连接。每请求新建连接会把 TLS 握手和
    TCP 慢启动算进吞吐，2 Mbit 链路上「有效带宽」会被系统性低估。"""
    if not _KEEPALIVE["on"]:
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    c = getattr(_TL, "conn", None)
    if c is None:
        c = http.client.HTTPSConnection(host, port, timeout=timeout)
        _TL.conn = c
    c.timeout = timeout
    if c.sock is not None:
        c.sock.settimeout(timeout)
    return c


def _post_once(url: str, key: str, payload: dict, timeout: float) -> tuple[int, bytes, float]:
    body = json.dumps(payload).encode()
    _, _, rest = url.partition("://")
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    t0 = time.perf_counter()
    for attempt in (0, 1):   # 第二次处理「连接已被对端关掉」的 keep-alive 竞态
        c = _conn(host, int(port or 443), timeout)
        try:
            c.request("POST", "/" + path, body=body,
                      headers={"Content-Type": "application/json",
                               "Authorization": f"Token {key}"})
            r = c.getresponse()
            data = r.read()
            return r.status, (data if r.status == 200 else data[:2000]), time.perf_counter() - t0
        except Exception as e:
            try:
                c.close()
            except Exception:
                pass
            _TL.conn = None
            if attempt:
                return 0, repr(e).encode()[:500], time.perf_counter() - t0


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def _add_chunks(msgs: list[dict], limit: int) -> list[list[dict]]:
    """按 JSON 体积切块，模拟平台「一次 Add ≤ 30 MiB」的分批方式。"""
    out, cur, size = [], [], 0
    for m in msgs:
        n = len(json.dumps(m).encode())
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        out.append(cur)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="远端 Add/Search 预演")
    ap.add_argument("--url", default="https://113.44.133.30")
    ap.add_argument("--key", default=os.environ.get("MEMORY_SYSTEM_KEY", ""))
    ap.add_argument("--limit", type=int, default=3, help="只跑前 N 个对话文件（0=全部 20 个）")
    ap.add_argument("--sample", type=int, default=60, help="抽样多少条 QA（0=该文件全部）")
    ap.add_argument("--jobs", type=int, default=2, help="并发 = 申报并发")
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--chunk-mib", type=float, default=24.0, help="单次 Add 的 JSON 体积上限（官方 30 MiB）")
    ap.add_argument("--timeout", type=float, default=120.0, help="单请求硬超时（秒）")
    ap.add_argument("--retries", type=int, default=2, help="超时/连接异常重试次数")
    ap.add_argument("--start", type=int, default=0, help="跳过前 N 个对话文件（续跑用）")
    ap.add_argument("--no-images", action="store_true", help="只发文字（对照 2 Mbit 下的下限）")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    if not args.key:
        print("缺 --key 或 MEMORY_SYSTEM_KEY", file=sys.stderr)
        return 2
    gp._REAL["on"] = not args.no_images

    url_add, url_search = args.url.rstrip("/") + "/add", args.url.rstrip("/") + "/search"
    files = sorted(glob.glob(os.path.join(DATA, "*.json")))
    if args.start:
        files = files[args.start:]
    if args.limit:
        files = files[: args.limit]
    dialogs = [(f, json.load(open(f, encoding="utf-8"))) for f in files]

    errs: list[tuple[str, str, int, str]] = []
    add_lat: list[float] = []
    sea_lat: list[float] = []
    resp_bytes: list[int] = []
    resp_n: list[int] = []
    up_bytes = 0
    n_add = n_sea = 0
    started = time.time()
    lock = threading.Lock()

    def note(kind, rid, code, body, lat, extra=0):
        nonlocal up_bytes, n_add, n_sea
        with lock:
            if kind == "add":
                add_lat.append(lat)
                up_bytes += extra
                n_add += 1
            else:
                sea_lat.append(lat)
                resp_bytes.append(extra[0] if isinstance(extra, tuple) else 0)
                resp_n.append(extra[1] if isinstance(extra, tuple) else 0)
                n_sea += 1
            if code != 200:
                errs.append((kind, rid, code, body.decode("utf-8", "replace")[:300]))

    for f, d in dialogs:
        topic = os.path.splitext(os.path.basename(f))[0]
        gp._REAL["topic"] = topic
        uid = "reh:" + topic
        msgs = []
        for s in d["multi_session_dialogues"]:
            for k, r in enumerate(s["dialogues"]):
                msgs.extend(gp.round_messages(r, s["date"], k, not args.no_images))
        chunks = _add_chunks(msgs, int(args.chunk_mib * MIB))
        for i, chunk in enumerate(chunks):
            payload = {"user_id": uid, "request_id": f"reh:{topic}:add:{i}",
                       "session_id": topic, "messages": chunk}
            code, body, lat = _post(url_add, args.key, payload, args.timeout, args.retries)
            note("add", payload["request_id"], code, body, lat, len(json.dumps(payload).encode()))
            print(f"  Add {topic} #{i+1}/{len(chunks)} {len(body)}B {code} {lat:.2f}s", flush=True)

        qas = d["human-annotated QAs"]
        if args.sample:
            step = max(1, len(qas) // args.sample)
            qas = qas[::step][: args.sample]
        queue = list(enumerate(qas))

        def worker():
            while True:
                with lock:
                    if not queue:
                        return
                    j, q = queue.pop(0)
                payload = {"user_id": uid, "request_id": f"reh:{topic}:q:{j}",
                           "top_k": args.top_k, "query": gp.query_content(q, not args.no_images)}
                code, body, lat = _post(url_search, args.key, payload, args.timeout, args.retries)
                n = 0
                if code == 200:
                    try:
                        n = len(json.loads(body).get("data") or [])
                    except Exception:
                        n = -1
                note("search", payload["request_id"], code, body, lat, (len(body), n))

        ths = [threading.Thread(target=worker) for _ in range(args.jobs)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        print(f"  {topic}: Add {len(chunks)} 块 / Search {len(qas)} 条完成", flush=True)

    wall = time.time() - started
    out_mib = sum(resp_bytes) / MIB
    rep = {
        "url": args.url, "jobs": args.jobs, "top_k": args.top_k,
        "files": len(files), "adds": n_add, "searches": n_sea,
        "wall_s": round(wall, 1),
        "add_lat_s": {"p50": round(_pct(add_lat, 50), 2), "p95": round(_pct(add_lat, 95), 2),
                      "max": round(max(add_lat, default=0), 2)},
        "search_lat_s": {"p50": round(_pct(sea_lat, 50), 2), "p95": round(_pct(sea_lat, 95), 2),
                         "max": round(max(sea_lat, default=0), 2)},
        "up_mib": round(up_bytes / MIB, 2), "down_mib": round(out_mib, 2),
        "resp_mib": {"p50": round(_pct(resp_bytes, 50) / MIB, 2),
                     "p95": round(_pct(resp_bytes, 95) / MIB, 2),
                     "max": round(max(resp_bytes, default=0) / MIB, 2)},
        "results_per_search": _pct(resp_n, 50),
        "errors": len(errs), "error_sample": errs[:6], "stalls_retried": _STALL["n"],
        "effective_mbit": round((up_bytes + sum(resp_bytes)) * 8 / max(wall, 1e-9) / 1e6, 2),
    }
    if sea_lat:
        rep["search_per_s"] = round(n_sea / wall, 3)
        rep["projected_min_1711"] = round(1711 / (n_sea / wall) / 60, 1)
    if out_mib:
        rep["cost_cny_at_0_8_per_GB"] = round(out_mib / 1024 * 0.8, 2)

    print("\n=== 预演结果 ===")
    print(f"文件 {len(files)} 个 · Add {n_add} 次 · Search {n_sea} 次 · 并发 {args.jobs} · 墙钟 {wall:.0f}s")
    print(f"上行 {rep['up_mib']} MiB · 下行 {out_mib:.1f} MiB · 有效吞吐 {rep['effective_mbit']} Mbit/s")
    print(f"Add 延迟 p50 {rep['add_lat_s']['p50']}s / p95 {rep['add_lat_s']['p95']}s；"
          f"Search 延迟 p50 {rep['search_lat_s']['p50']}s / p95 {rep['search_lat_s']['p95']}s / "
          f"max {rep['search_lat_s']['max']}s")
    print(f"Search 响应 p50 {rep['resp_mib']['p50']} / p95 {rep['resp_mib']['p95']} / "
          f"max {rep['resp_mib']['max']} MiB（上限 30 MiB）· 返回条数中位 {rep['results_per_search']}")
    if "search_per_s" in rep:
        print(f"吞吐 {rep['search_per_s']} req/s · 全量 1,711 条外推 {rep['projected_min_1711']} 分钟"
              f" · 流量费约 ¥{rep.get('cost_cny_at_0_8_per_GB', 0)}")
    print(f"错误 {rep['errors']} 个 · 瞬时卡死重试 {_STALL['n']} 次")
    for kind, rid, code, body in rep["error_sample"]:
        print(f"  ✗ {kind} {rid} → {code} {body[:160]}")
    if args.report:
        json.dump(rep, open(args.report, "w"), ensure_ascii=False, indent=2)
        print(f"报告：{args.report}")
    print(f"\n清理：python3 tools/purge.py --db service/mem.db --user-prefix 'reh:'")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
