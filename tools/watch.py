#!/usr/bin/env python3
"""评测期间的看护循环：定时拉 /version，指标异常时打印告警（可选推飞书）。

为什么要它：Full 连跑 0.5–2 天，且只有一次机会。真正会毁掉一次评测的是
「上游配额被打穿 → 大量 503」和「精排降级 → 排序悄悄变了」，这两件事
在日志里不显眼，只在 /version 的计数器里看得到。

用法：
  python3 tools/watch.py https://你的域名 --key KEY                 # 每 300s 打一行
  python3 tools/watch.py https://你的域名 --key KEY --interval 60 \
      --feishu https://open.feishu.cn/open-apis/bot/v2/hook/xxxx

告警阈值：
  - api_give_up 比上一轮增加 → 上游重试预算耗尽，已经有 503 出去了
  - rerank.degraded 变 true  → 精排已降级，排序口径变了
  - api_429/api_calls > 0.5  → 配额墙，考虑降并发
  - 请求本身失败（401/5xx/连不上）连续 3 次 → 服务或证书有问题
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

FIELDS = ("api_calls", "api_429", "api_retries", "api_give_up", "api_net")


def fetch(url: str, key: str, insecure: bool) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/version",
                                 headers={"Authorization": f"Token {key}"})
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.load(r)


def notify(hook: str, text: str) -> None:
    if not hook:
        return
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(hook, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except (urllib.error.URLError, OSError) as e:
        print(f"[watch] 飞书推送失败：{e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="评测期 /version 看护")
    ap.add_argument("base")
    ap.add_argument("--key", default="")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--feishu", default="")
    ap.add_argument("--rounds", type=int, default=0, help="0 = 一直跑")
    ap.add_argument("--insecure", action="store_true", help="自签证书联调用")
    args = ap.parse_args()

    prev: dict | None = None
    fails = 0
    n = 0
    while True:
        n += 1
        stamp = time.strftime("%H:%M:%S")
        try:
            v = fetch(args.base, args.key, args.insecure)
            fails = 0
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            fails += 1
            print(f"{stamp} 拉取失败({fails}/3)：{e}", flush=True)
            if fails >= 3:
                notify(args.feishu, f"[mem-pro] {stamp} 连续 {fails} 次拉不到 /version：{e}")
            time.sleep(args.interval)
            continue

        e = v.get("embed", {})
        rr = v.get("retrieval", {}).get("rerank", {})
        cur = {k: e.get(k, 0) for k in FIELDS}
        delta = "" if prev is None else " Δ" + " ".join(
            f"{k.replace('api_', '')}+{cur[k] - prev[k]}" for k in FIELDS)
        print(f"{stamp} commit={v.get('commit')} cfg={v.get('config_hash')} "
              + " ".join(f"{k.replace('api_', '')}={cur[k]}" for k in FIELDS)
              + f" rerank_degraded={rr.get('degraded')}{delta}", flush=True)

        alerts = []
        if prev is not None:
            if cur["api_give_up"] > prev["api_give_up"]:
                alerts.append(f"api_give_up +{cur['api_give_up'] - prev['api_give_up']}"
                              "（重试预算耗尽，已对平台返回 503）")
            if cur["api_calls"] and (cur["api_429"] / cur["api_calls"]) > 0.5:
                alerts.append(f"429 占比 {cur['api_429'] / cur['api_calls']:.0%}（配额墙，考虑降并发）")
        if rr.get("degraded"):
            alerts.append("精排已降级（排序口径变了，记入材料）")
        for a in alerts:
            print(f"  ⚠ {a}", flush=True)
        if alerts:
            notify(args.feishu, f"[mem-pro] {stamp} " + "；".join(alerts))

        prev = cur
        if args.rounds and n >= args.rounds:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
