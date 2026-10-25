#!/usr/bin/env python3
"""Concurrent Search load probe for an already-running mem-pro endpoint.

Examples:
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


def call(url: str, key: str, user_id: str, query: str, top_k: int,
         timeout: float, request_id: str) -> dict:
    body = json.dumps({"query": query, "user_id": user_id, "top_k": top_k},
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
        valid = isinstance(payload, dict) and isinstance(payload.get("data"), list)
    except Exception:
        valid = False
    return {"status": status, "ms": (time.perf_counter() - started) * 1000,
            "valid": valid, "error": "invalid_response" if not valid else ""}


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Concurrent Search load probe")
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
    args = ap.parse_args()
    if min(args.top_k, args.requests, args.concurrency) <= 0 or args.warmup < 0 or args.timeout <= 0:
        ap.error("top-k, requests, concurrency, and timeout must be positive; warmup must be nonnegative")

    try:
        queries = load_queries(args.queries_file, args.query)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        ap.error(str(e))
    url = args.url.rstrip("/") + "/search"
    run_id = uuid.uuid4().hex[:12]
    for i in range(args.warmup):
        result = call(url, args.key, args.user_id, queries[i % len(queries)],
                      args.top_k, args.timeout, f"load-{run_id}-warm-{i}")
        if result["status"] != 200 or not result["valid"]:
            print(f"warm-up failed: status={result['status']} {result['error']}", file=sys.stderr)
            return 1

    def task(i: int) -> dict:
        return call(url, args.key, args.user_id, queries[i % len(queries)], args.top_k,
                    args.timeout, f"load-{run_id}-{i}")

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(task, range(args.requests)))
    elapsed = time.perf_counter() - started
    latencies = [r["ms"] for r in results]
    valid = [r for r in results if r["status"] == 200 and r["valid"]]
    status_counts: dict[str, int] = {}
    for result in results:
        label = str(result["status"])
        status_counts[label] = status_counts.get(label, 0) + 1

    print(f"target={url} requests={len(results)} concurrency={args.concurrency}")
    print(f"valid={len(valid)}/{len(results)} elapsed={elapsed:.2f}s throughput={len(valid)/elapsed:.2f} successful req/s")
    print(f"latency_ms p50={statistics.median(latencies):.1f} p95={percentile(latencies, .95):.1f} "
          f"p99={percentile(latencies, .99):.1f} max={max(latencies):.1f}")
    print("statuses=" + json.dumps(status_counts, sort_keys=True))
    errors = [r["error"] for r in results if r["error"]]
    if errors:
        print("errors=" + json.dumps({e: errors.count(e) for e in sorted(set(errors))}, sort_keys=True))
    return 0 if len(valid) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
