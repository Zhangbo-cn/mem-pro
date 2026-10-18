"""复现性检查：官方保留「重跑并比对分数」的权利，分差过大可作废。

本工具对同一 query 重复调用 N 次，比对返回字节是否完全一致，
并在 add 侧验证同一 request_id 重放不会改变记忆总量。

  python3 tools/replay.py http://127.0.0.1:8080 [N]
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 5


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def stats():
    with urllib.request.urlopen(BASE + "/stats", timeout=30) as r:
        return json.loads(r.read())


def main():
    u = "replay:user"
    add = {"request_id": "replay:c0", "user_id": u, "session_id": "s0",
           "messages": [{"role": "user", "content": "The meeting moved to Thursday.",
                         "timestamp": 1704067200000}]}
    post("/add", add)
    before = stats()["memories"]
    post("/add", add)
    post("/add", add)
    after = stats()["memories"]
    print(f"幂等：记忆数 {before} → {after}  {'✅' if before == after else '❌ 重复写入！'}")

    outs = [post("/search", {"query": "When is the meeting?", "user_id": u, "top_k": 20})
            for _ in range(N)]
    same = all(o == outs[0] for o in outs)
    print(f"确定性：{N} 次 Search 字节一致 {'✅' if same else '❌ 存在漂移'}")
    if not same:
        for i, o in enumerate(outs):
            print(f"  #{i}", o[:200])
        sys.exit(1)


if __name__ == "__main__":
    main()
