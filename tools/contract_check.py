"""本地契约校验器：不烧 smoke 额度，先把 11 条官方约束跑绿。

用法：
  python3 tools/contract_check.py http://127.0.0.1:8080 [--key YOUR_MEMORY_SYSTEM_KEY]
  python3 tools/contract_check.py https://your.host     # 线上 smoke 前自检
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

import dotenv  # noqa: E402

dotenv.load()

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("MEMORY_SYSTEM_KEY", "").strip()
if "--key" in sys.argv:
    KEY = sys.argv[sys.argv.index("--key") + 1]

OK, FAIL = [], []


def call(path, body=None, key=True, method=None):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if key and KEY:
        headers["Authorization"] = f"Token {KEY}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   {detail}" if detail and not cond else ""))


def j(raw):
    try:
        return json.loads(raw.decode())
    except Exception:
        return {}


def run_id() -> str:
    return "chk" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:8]


RID = run_id()
U1, U2 = f"chk:{RID}:u1", f"chk:{RID}:u2"
S1 = f"chk:{RID}:s1"


def png_b64() -> str:
    # 1x1 透明 PNG
    return "data:image/png;base64," + base64.b64encode(
        bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                      "890000000a4944415478da6300010000050001a5f645400000000049454e44ae426082")
    ).decode()


def main():
    print(f"\n目标 {BASE}   run={RID}\n")

    print("[1] Health（无需鉴权，任意 2xx）")
    st, _ = call("/health", key=False)
    check("GET /health 返回 2xx", 200 <= st < 300, f"status={st}")

    print("[2] 同步写入 + 三个 ID 原样回显")
    add1 = {"request_id": f"{RID}:c0", "user_id": U1, "session_id": S1,
            "messages": [{"role": "user", "content": "Alice lives in Berlin and likes jazz.",
                          "timestamp": 1704067200000}]}
    st, raw = call("/add", add1)
    b = j(raw)
    check("Add 返回 200", st == 200, f"status={st} {raw[:160]}")
    check("success == true（布尔）", b.get("success") is True)
    check("request_id 原样回显", b.get("request_id") == add1["request_id"])
    check("user_id 原样回显", b.get("user_id") == U1)
    check("session_id 原样回显", b.get("session_id") == S1)

    print("[3] 幂等：同一 request_id 重放不应产生新记忆")
    st2, raw2 = call("/add", add1)
    check("重复 Add 仍返回 200", st2 == 200, f"status={st2}")
    check("重复 Add 响应完全一致", raw2 == raw, "两次响应不同")

    print("[4] 写后可见 + 返回结构")
    st, raw = call("/search", {"query": "Where does Alice live?", "user_id": U1, "top_k": 100})
    d = j(raw)
    check("Search 返回 200", st == 200, f"status={st}")
    check("顶层是对象且含 data 数组", isinstance(d, dict) and isinstance(d.get("data"), list))
    check("没有 items 包装层", "items" not in d)
    check("命中非空（写后立即可检索）", len(d.get("data", [])) > 0, f"n={len(d.get('data', []))}")
    if d.get("data"):
        it = d["data"][0]
        check("每条含非空 id", isinstance(it.get("id"), str) and it["id"] != "")
        check("每条含非空 content", bool(it.get("content")))
        check("score 为数值（可选字段）", isinstance(it.get("score"), (int, float)))

    print("[5] user_id 严格隔离")
    st, raw = call("/search", {"query": "Where does Alice live?", "user_id": U2, "top_k": 100})
    check("另一 user_id 检索为空", j(raw).get("data") == [], raw[:160])

    print("[6] 无结果返回 {\"data\": []}")
    st, raw = call("/search", {"query": "zzz_absent_" + RID, "user_id": U2, "top_k": 5})
    check("空结果为 {\"data\": []}", j(raw) == {"data": []}, raw[:160])

    print("[7] top_k 上限")
    many = {"request_id": f"{RID}:c1", "user_id": U1, "session_id": S1,
            "messages": [{"role": "user", "content": f"note number {i} about topic {i}",
                          "timestamp": 1704067200000 + i} for i in range(40)]}
    call("/add", many)
    for k in (1, 3, 10):
        st, raw = call("/search", {"query": "note number", "user_id": U1, "top_k": k})
        n = len(j(raw).get("data", []))
        check(f"top_k={k} 时返回 ≤ {k}", n <= k, f"n={n}")

    print("[8] 多模态 ContentPart[]（内联 Base64）")
    mm = {"request_id": f"{RID}:c2", "user_id": U1, "session_id": S1,
          "messages": [{"role": "user", "content": [
              {"type": "text", "text": "receipt from cafe on 2026-03-02"},
              {"type": "image_url", "image_url": {"url": png_b64()}}],
              "timestamp": 1772409600000}]}
    st, raw = call("/add", mm)
    check("多模态 Add 200", st == 200, f"status={st} {raw[:200]}")
    st, raw = call("/search", {"query": [{"type": "text", "text": "cafe receipt"}],
                               "user_id": U1, "top_k": 5})
    check("多模态 query（数组）可检索", st == 200 and len(j(raw).get("data", [])) > 0, raw[:200])

    print("[9] 非法请求应被拒绝（4xx，不 200）")
    st, _ = call("/add", {"request_id": f"{RID}:bad", "user_id": U1, "session_id": S1,
                          "messages": [{"role": "user", "content": ""}]})
    check("空 content → 4xx", 400 <= st < 500, f"status={st}")
    st, _ = call("/search", {"query": "x", "user_id": U1})
    check("缺 top_k → 4xx", 400 <= st < 500, f"status={st}")
    st, _ = call("/add", {"request_id": f"{RID}:bad2", "user_id": U1, "session_id": S1,
                          "messages": [{"role": "user", "content": [
                              {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]})
    check("远程图片 URL → 4xx（必须内联 Base64）", 400 <= st < 500, f"status={st}")

    print("[10] 响应体积（≤30 MiB）")
    st, raw = call("/search", {"query": "receipt", "user_id": U1, "top_k": 100})
    check(f"响应 {len(raw)/1048576:.2f} MiB ≤ 30 MiB", len(raw) <= 30 * 1024 * 1024)

    print("[11] 确定性：同一 query 连续 3 次结果一致")
    a = call("/search", {"query": "cafe receipt", "user_id": U1, "top_k": 5})[1]
    b1 = call("/search", {"query": "cafe receipt", "user_id": U1, "top_k": 5})[1]
    b2 = call("/search", {"query": "cafe receipt", "user_id": U1, "top_k": 5})[1]
    check("3 次响应字节完全一致", a == b1 == b2, "存在不确定性")

    print(f"\n通过 {len(OK)} / {len(OK)+len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
