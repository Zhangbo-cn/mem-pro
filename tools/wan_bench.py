#!/usr/bin/env python3
"""公网上行带宽实测：用真图跑一遍 Add + Search，量真实返回字节与吞吐。

为什么不用 speedtest：我们的瓶颈是**服务端出向**（Search 响应里内联的图片 Base64）。
只有拿真实响应体量才算得准。这个工具从外网（你的开发机）打已部署的接口：

  1. 用 Mem-Gallery 官方图选出贴近真实分布的 12 张（p50≈41KB / p90≈776KB / 长尾几 MB）
  2. Add 成一个临时用户（可用 --images 调整张数）
  3. 跑 N 次 Search，记录每次响应字节数与耗时
  4. 输出有效 MB/s、req/s、p50/p95 延迟，以及评测 1,711 条 query 的预计耗时
  5. --purge 删掉临时用户（默认保留，方便复测；提交前记得清）

用法：
  python3 tools/wan_bench.py --url https://113.44.133.30 --key <Key>
  python3 tools/wan_bench.py --url http://113.44.133.30 --key <Key> --searches 30 --purge
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_ROOT = os.path.join(ROOT, "data", "mem-gallery", "image")
MIME = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}


def pick_images(n: int) -> list[str]:
    """按大小分位挑图，尽量逼近官方集的真实体积分布。"""
    files = [p for p in glob.glob(os.path.join(IMG_ROOT, "*", "*.jpg"))
             + glob.glob(os.path.join(IMG_ROOT, "*", "*.png"))
             if os.path.getsize(p) <= 10 * 1024 * 1024]
    if not files:
        sys.exit(f"没找到图片：先跑 bash tools/fetch_gallery_images.sh")
    files.sort(key=os.path.getsize)
    out = []
    for i in range(n):
        # 均匀取分位点，跳过最末的超大长尾（平台不发，也会被 30 MiB 预算裁掉）
        q = i / max(1, n - 1) * 0.95
        out.append(files[min(len(files) - 1, int(q * (len(files) - 1)))])
    return out


def data_uri(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    raw = open(path, "rb").read()
    return f"data:image/{MIME.get(ext, 'jpeg')};base64," + base64.b64encode(raw).decode()


def call(base: str, path: str, body: dict, key: str, timeout: float) -> tuple[int, int]:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data,
                                 {"Authorization": f"Token {key}",
                                  "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, len(e.read())


def main() -> int:
    ap = argparse.ArgumentParser(description="公网上行带宽实测（真图 Add + Search）")
    ap.add_argument("--url", required=True, help="例如 https://113.44.133.30")
    ap.add_argument("--key", default=os.environ.get("MEMORY_SYSTEM_KEY", ""))
    ap.add_argument("--images", type=int, default=12)
    ap.add_argument("--searches", type=int, default=20)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--user", default="")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--purge", action="store_true", help="量完删掉临时用户")
    ap.add_argument("--report", default="")
    args = ap.parse_args()
    if not args.key:
        sys.exit("缺 --key 或环境变量 MEMORY_SYSTEM_KEY")
    uid = args.user or f"wanbench:{int(time.time())}"

    paths = pick_images(args.images)
    sizes = [os.path.getsize(p) for p in paths]
    print(f"图片 {len(paths)} 张：p50 {statistics.median(sizes) / 2**10:.0f} KB / "
          f"max {max(sizes) / 2**20:.2f} MiB / 合计 {sum(sizes) / 2**20:.1f} MiB", flush=True)

    t0 = time.time()
    up = 0
    for start in range(0, len(paths), 6):
        chunk = paths[start:start + 6]
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": f"旅行照片 {i}，在海边拍的"},
            {"type": "image_url", "image_url": {"url": data_uri(p)}}]} for i, p in enumerate(chunk)]
        code, n = call(args.url, "/add", {"request_id": f"wan-{start}", "user_id": uid,
                                          "session_id": "wan", "messages": msgs},
                       args.key, args.timeout)
        up += sum(os.path.getsize(p) for p in chunk)
        if code != 200:
            sys.exit(f"Add 失败 {code}（{n} 字节）")
    add_s = time.time() - t0
    print(f"Add {len(paths)} 张：{add_s:.1f}s，上行 {up / add_s / 2**20:.2f} MiB/s", flush=True)

    lat, nbytes, queries = [], [], []
    for i, p in enumerate(paths):
        queries.append(f"第 {i} 张照片拍的是什么地方")
    for q in queries[:args.searches]:
        t = time.perf_counter()
        code, n = call(args.url, "/search", {"query": q, "user_id": uid, "top_k": args.top_k},
                       args.key, args.timeout)
        lat.append(time.perf_counter() - t)
        nbytes.append(n)
        if code != 200:
            print(f"  Search 失败 {code}（{n} 字节）")
    ok = [x for x in nbytes if x > 100]
    total_b, total_s = sum(ok), sum(lat)
    lat.sort()
    pick = lambda v, q: v[min(len(v) - 1, int(len(v) * q))]
    rate_b = total_b / total_s if total_s else 0
    print(f"\nSearch {len(lat)} 次：p50 {pick(lat, .5):.2f}s / p95 {pick(lat, .95):.2f}s")
    print(f"响应体积：p50 {pick(sorted(ok), .5) / 2**20:.2f} MiB / "
          f"max {max(ok) / 2**20:.2f} MiB（上限 30 MiB）")
    print(f"有效吞吐：{rate_b / 2**20:.2f} MiB/s = {rate_b * 8 / 1e6:.1f} Mbit/s，"
          f"{len(lat) / total_s:.2f} req/s（串行）")
    print(f"评测 1,711 条 query 预计耗时：{1711 / max(1e-9, len(lat) / total_s) / 60:.0f} 分钟")
    if args.report:
        json.dump({"user": uid, "url": args.url, "images": len(paths),
                   "miB_per_s": rate_b / 2**20, "mbit": rate_b * 8 / 1e6,
                   "req_s": len(lat) / total_s, "p50_s": pick(lat, .5),
                   "p95_s": pick(lat, .95), "resp_p50_mib": pick(sorted(ok), .5) / 2**20,
                   "add_mib_s": up / add_s / 2**20,
                   "projected_min_1711": 1711 / max(1e-9, len(lat) / total_s) / 60},
                  open(args.report, "w"), indent=2)
        print("报告：", args.report)
    if args.purge:
        print(f"\n清理临时用户（在服务器上执行，DB 在那边）：")
        print(f"  python3 tools/purge.py --user-prefix '{uid}' --db /data/mem.db")
    else:
        print(f"\n临时用户 {uid} 仍在库里，提交前记得清：")
        print(f"  python3 tools/purge.py --user-prefix '{uid}' --db /data/mem.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
