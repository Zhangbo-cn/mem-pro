"""线上 smoke：只打已部署的 HTTPS 服务，不碰本地代码。

用途：正式提交平台前确认「公网可达 + 鉴权 + 同步写入 + 隔离 + 幂等 + 体积」。
它不能替代 tools/contract_check.py（后者覆盖 27 项契约细节），但适合快速回归。

  python3 tools/smoke.py https://mem.example.com --key "$MEMORY_SYSTEM_KEY"
  python3 tools/smoke.py https://mem.example.com --key K --report smoke.json
"""
from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid

PNG = ("data:image/png;base64," + base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                  "890000000a4944415478da6300010000050001a5f645400000000049454e44ae426082")
).decode())

OK, FAIL = [], []
LAT: dict[str, list[float]] = {"add": [], "search": []}


def check(name: str, cond: bool, detail: str = "") -> None:
    (OK if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   {detail}" if detail else ""))


def call(base: str, path: str, body: dict | None = None, key: str = "", timeout: int = 60):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Token {key}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ms = (time.time() - started) * 1000
            op = path.strip("/")
            if op in LAT:
                LAT[op].append(ms)
            return r.status, raw, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode(), {}


def j(raw: bytes):
    try:
        return json.loads(raw.decode())
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="mem-pro 线上 smoke")
    ap.add_argument("url")
    ap.add_argument("--key", default="", help="Memory System Key")
    ap.add_argument("--report", default="", help="把 JSON 报告写到该路径")
    ap.add_argument("--skip-image", action="store_true", help="跳过多模态 Base64 用例")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    rid = "smoke" + uuid.uuid4().hex[:10]
    u1, u2 = f"{rid}:u1", f"{rid}:u2"
    print(f"\n目标 {base}\nrun  {rid}\n")

    print("[1] 公网可达 + Health 免鉴权")
    st, raw, _ = call(base, "/health")
    check("GET /health 2xx", 200 <= st < 300, f"status={st} {raw[:120]!r}")

    if args.key:
        print("[2] 鉴权")
        st, _, _ = call(base, "/stats", key="")
        check("无 key 访问受保护接口被拒绝", st in (401, 403), f"status={st}")
        st, raw, _ = call(base, "/stats", key=args.key)
        check("正确 key 可访问 /stats", st == 200, f"status={st} {raw[:120]!r}")
    else:
        print("[2] 鉴权：未提供 --key，跳过（生产环境必须开启鉴权）")

    print("[3] Add 同步写入 + 响应契约")
    body = {"request_id": f"{rid}:c0", "user_id": u1, "session_id": f"{rid}:s0",
            "messages": [{"role": "user", "content": "Alice lives in Berlin and likes jazz.",
                          "timestamp": 1704067200000}]}
    st, raw, _ = call(base, "/add", body, args.key)
    res = j(raw)
    check("Add 返回 200", st == 200, f"status={st} {raw[:160]!r}")
    check("success is true", res.get("success") is True)
    check("三个 ID 原样回显", all(res.get(k) == body[k]
                                for k in ("request_id", "user_id", "session_id")))

    print("[4] 幂等（同一 request_id 重放）")
    st2, raw2, _ = call(base, "/add", body, args.key)
    check("重放仍 200", st2 == 200, f"status={st2}")
    check("重放响应字节一致", raw2 == raw)

    print("[5] 写后可见 + 结构 + top_k")
    st, raw, _ = call(base, "/search", {"query": "Where does Alice live?",
                                       "user_id": u1, "top_k": 10}, args.key)
    data = j(raw).get("data")
    check("Search 200", st == 200, f"status={st}")
    check("顶层 data 数组、无 items 包装", isinstance(data, list) and "items" not in j(raw))
    check("写后立即可检索", bool(data), f"n={len(data) if isinstance(data, list) else '?'}")
    check("条数 ≤ top_k", isinstance(data, list) and len(data) <= 10)
    if data:
        item = data[0]
        check("条目含 id/content", bool(item.get("id")) and bool(item.get("content")))

    print("[6] user_id 隔离 + 空结果")
    st, raw, _ = call(base, "/search", {"query": "Where does Alice live?",
                                       "user_id": u2, "top_k": 10}, args.key)
    check("另一 user 检索为空", j(raw).get("data") == [])
    st, raw, _ = call(base, "/search", {"query": f"zzz_absent_{rid}",
                                       "user_id": u2, "top_k": 5}, args.key)
    check("空结果严格为 {\"data\": []}", j(raw) == {"data": []})

    if not args.skip_image:
        print("[7] 多模态（内联 Base64）")
        st, raw, _ = call(base, "/add", {
            "request_id": f"{rid}:mm", "user_id": u1, "session_id": f"{rid}:s0",
            "messages": [{"role": "user", "timestamp": 1772409600000, "content": [
                {"type": "text", "text": "receipt from cafe on 2026-03-02"},
                {"type": "image_url", "image_url": {"url": PNG}}]}]}, args.key)
        check("多模态 Add 200", st == 200, f"status={st} {raw[:160]!r}")
        st, raw, _ = call(base, "/search", {"query": "cafe receipt", "user_id": u1,
                                           "top_k": 5}, args.key)
        d = j(raw).get("data") or []
        has_img = any(isinstance(x.get("content"), list)
                      and any(p.get("type") == "image_url" for p in x["content"]) for x in d)
        check("图片证据按 ContentPart[] 返回", has_img, f"n={len(d)}")

    print("[8] 响应体积（≤30 MiB）")
    st, raw, _ = call(base, "/search", {"query": "receipt", "user_id": u1, "top_k": 100}, args.key)
    check(f"{len(raw) / 1048576:.2f} MiB ≤ 30 MiB", len(raw) <= 30 * 1024 * 1024)

    print("[9] 延迟（本机到目标，仅供参考）")
    for op, vals in LAT.items():
        if vals:
            p50 = statistics.median(vals)
            print(f"  {op}: n={len(vals)} p50={p50:.0f}ms max={max(vals):.0f}ms")

    report = {
        "url": base, "run": rid, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": len(OK), "fail": len(FAIL), "failures": FAIL,
        "latency_ms": {k: v for k, v in LAT.items() if v},
    }
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.report}")

    print(f"\n通过 {len(OK)} / {len(OK) + len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
