#!/usr/bin/env python3
"""Concurrent Search load probe for an already-running mem-pro endpoint.

Examples:
  python3 tools/search_load.py https://mem.example.com --user-id u1 --sweep 1,4,8,16,32 --key "$KEY"
  python3 tools/search_load.py http://127.0.0.1:8080 --mode add --user-id bench-add --requests 100 --concurrency 8
  python3 tools/search_load.py http://127.0.0.1:8080 --user-id bench-user --requests 200 --concurrency 16
  python3 tools/search_load.py https://memory.example.com --user-id u1 --queries-file tools/eval_queries.json --key "$MEMORY_SYSTEM_KEY"
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid


def load_queries(path: str, fallback: str) -> list[str]:
    if not path:
        return [fallback]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    values = data.get("queries", data) if isinstance(data, dict) else data
    if not isinstance(values, list):
        raise ValueError("query file must contain a list or an object with a queries list")
    queries = []
    for item in values:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("q") or item.get("query")
        else:
            continue
        if isinstance(text, str) and text.strip():
            queries.append(text)
    if not queries:
        raise ValueError("query file contains no query strings")
    return queries


def add_body(user_id: str, request_id: str, n_messages: int, seed: int) -> dict:
    """Realistic Add payload: one dialogue chunk of n messages, unique text per request."""
    msgs = []
    for j in range(n_messages):
        role = "user" if j % 2 == 0 else "assistant"
        msgs.append({"role": role, "timestamp": 1704067200000 + seed * 60000 + j,
                     "content": f"[{seed}:{j}] Session note {seed}: the user mentioned item "
                                f"{seed * 7 + j} and plans for week {seed % 52}."})
    return {"request_id": request_id, "user_id": user_id, "session_id": f"load-{seed % 8}",
            "messages": msgs}


def call(url: str, key: str, user_id: str, query: str, top_k: int,
         timeout: float, request_id: str, body_obj: dict | None = None) -> dict:
    body = json.dumps(body_obj if body_obj is not None else
                      {"query": query, "user_id": user_id, "top_k": top_k},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Request-Id": request_id}
    if key:
        headers["Authorization"] = f"Token {key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
    except Exception as e:  # network errors have no HTTP status
        return {"status": 0, "ms": (time.perf_counter() - started) * 1000,
                "valid": False, "error": type(e).__name__}
    try:
        payload = json.loads(raw.decode("utf-8"))
        valid = isinstance(payload, dict) and (isinstance(payload.get("data"), list)
                                               or payload.get("success") is True)
    except Exception:
        valid = False
    return {"status": status, "ms": (time.perf_counter() - started) * 1000,
            "valid": valid, "error": "invalid_response" if not valid else ""}


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Concurrent Search / Add load probe")
    ap.add_argument("url", help="base URL, e.g. http://127.0.0.1:8080")
    ap.add_argument("--user-id", required=True, help="existing user_id to search")
    ap.add_argument("--key", default="", help="Memory System Key")
    ap.add_argument("--query", default="memory retrieval benchmark")
    ap.add_argument("--queries-file", default="", help="JSON list or eval set with a queries array")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--requests", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--warmup", type=int, default=3, help="sequential warm-up calls excluded from measurements")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--mode", choices=("search", "add"), default="search",
                    help="add: each request writes a fresh n-message chunk (real embedding cost!)")
    ap.add_argument("--messages", type=int, default=10, help="messages per Add request")
    ap.add_argument("--sweep", default="", help="comma list of concurrency levels, e.g. 1,4,8,16,32")
    ap.add_argument("--p95-budget-ms", type=float, default=5000,
                    help="a sweep level counts as stable only if p95 is within this budget")
    ap.add_argument("--nonce", action="store_true",
                    help="append a per-request token so every query is a fresh embedding call "
                         "(otherwise the in-process vector cache absorbs the load and the "
                         "numbers say nothing about upstream capacity)")
    ap.add_argument("--report", default="", help="write sweep rows as JSON")
    args = ap.parse_args()
    if min(args.top_k, args.requests, args.concurrency) <= 0 or args.warmup < 0 or args.timeout <= 0:
        ap.error("top-k, requests, concurrency, and timeout must be positive; warmup must be nonnegative")

    try:
        queries = load_queries(args.queries_file, args.query)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        ap.error(str(e))
    url = args.url.rstrip("/") + ("/add" if args.mode == "add" else "/search")
    run_id = uuid.uuid4().hex[:12]

    def body_for(tag: str, i: int):
        if args.mode != "add":
            return None
        return add_body(args.user_id, f"load-{run_id}-{tag}{i}", args.messages, i)

    for i in range(args.warmup):
        q = queries[i % len(queries)] + (f" #warm{run_id}{i}" if args.nonce else "")
        result = call(url, args.key, args.user_id, q,
                      args.top_k, args.timeout, f"load-{run_id}-warm-{i}", body_for("w", i))
        if result["status"] != 200 or not result["valid"]:
            print(f"warm-up failed: status={result['status']} {result['error']}", file=sys.stderr)
            return 1

    def measure(concurrency: int, offset: int) -> dict:
        def task(i: int) -> dict:
            j = offset + i
            q = queries[j % len(queries)]
            if args.nonce:
                q = f"{q} #{run_id}{j}"
            return call(url, args.key, args.user_id, q, args.top_k,
                        args.timeout, f"load-{run_id}-{j}", body_for("r", j))

        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(task, range(args.requests)))
        elapsed = time.perf_counter() - started
        latencies = [r["ms"] for r in results]
        valid = [r for r in results if r["status"] == 200 and r["valid"]]
        status_counts: dict[str, int] = {}
        for result in results:
            label = str(result["status"])
            status_counts[label] = status_counts.get(label, 0) + 1
        errors = [r["error"] for r in results if r["error"]]
        return {"concurrency": concurrency, "requests": len(results), "valid": len(valid),
                "elapsed_s": round(elapsed, 2), "rps": round(len(valid) / elapsed, 2),
                "p50": round(statistics.median(latencies), 1),
                "p95": round(percentile(latencies, .95), 1),
                "p99": round(percentile(latencies, .99), 1), "max": round(max(latencies), 1),
                "statuses": status_counts,
                "errors": {e: errors.count(e) for e in sorted(set(errors))}}

    levels = [args.concurrency]
    if args.sweep:
        levels = [int(x) for x in args.sweep.split(",") if x.strip()]
    rows = []
    for k, c in enumerate(levels):
        r = measure(c, k * args.requests)
        rows.append(r)
        print(f"target={url} requests={r['requests']} concurrency={c}")
        print(f"valid={r['valid']}/{r['requests']} elapsed={r['elapsed_s']:.2f}s "
              f"throughput={r['rps']:.2f} successful req/s")
        print(f"latency_ms p50={r['p50']:.1f} p95={r['p95']:.1f} p99={r['p99']:.1f} max={r['max']:.1f}")
        print("statuses=" + json.dumps(r["statuses"], sort_keys=True))
        if r["errors"]:
            print("errors=" + json.dumps(r["errors"], sort_keys=True))

    if args.sweep:
        # 「稳定」= 全部成功且 p95 不超过预算；申报值取最高稳定并发的 60–70%（只有一次 Full）
        stable = [r for r in rows if r["valid"] == r["requests"] and r["p95"] <= args.p95_budget_ms]
        print("\n=== sweep summary ===")
        print(f"{'conc':>5} {'ok':>9} {'req/s':>8} {'p50':>8} {'p95':>8} {'p99':>8}")
        for r in rows:
            mark = " *" if r in stable else ""
            print(f"{r['concurrency']:>5} {r['valid']:>4}/{r['requests']:<4} {r['rps']:>8.2f} "
                  f"{r['p50']:>8.1f} {r['p95']:>8.1f} {r['p99']:>8.1f}{mark}")
        if stable:
            best = max(stable, key=lambda r: r["concurrency"])
            lo, hi = max(1, int(best["concurrency"] * 0.6)), max(1, int(best["concurrency"] * 0.7))
            print(f"max stable concurrency={best['concurrency']} (p95 ≤ {args.p95_budget_ms:.0f} ms) → "
                  f"declare {args.mode} concurrency {lo}–{hi}")
        else:
            print("no stable level: lower concurrency or fix errors before declaring capacity")
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump({"mode": args.mode, "url": url, "rows": rows}, f, indent=2)
    return 0 if all(r["valid"] == r["requests"] for r in rows) else 1

if __name__ == "__main__":
    raise SystemExit(main())
