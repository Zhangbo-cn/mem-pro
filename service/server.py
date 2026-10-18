"""AML 第二期 Add / Search 服务（纯标准库实现，无需 pip 安装）。

契约要点（官方 HTTP 200 才代表写入完成并立即可检索）：
  GET  /health                无需鉴权，任意 2xx
  POST /add     {request_id, messages[], user_id, session_id} → {success, request_id, user_id, session_id}
  POST /search  {query, options?, user_id, top_k}             → {data: [{id, content, score, created_at}]}

规则要点：
  - request_id 幂等（官方允许最多 32 次重试，同一 request_id 与 payload）
  - user_id 唯一隔离边界；session_id 不作为 Search 过滤条件
  - 返回条数 ≤ top_k；超量/非法条目判契约错误
  - 多模态 content 支持有序 [{"type":"text"},{"type":"image_url","image_url":{"url":"data:..."}}]
  - 图片必须内联 Base64；单图 ≤10 MiB、单次 Add ≤30 MiB、单次 Search 响应 ≤30 MiB
  - 返回顺序即证据顺序，且平台按顺序截断 Answer 上下文（117,760 token）

启动：
  EMBED_MODE=hash python3 service/server.py            # 离线自测
  EMBED_MODE=api DASHSCOPE_API_KEY=sk-xxx python3 service/server.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget
import embed
import retrieve
import store

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
AUTH_KEY = os.environ.get("MEMORY_SYSTEM_KEY", "").strip()
MAX_BODY = 64 * 1024 * 1024
MAX_IMG_BYTES = 10 * 1024 * 1024
MAX_ADD_BYTES = 30 * 1024 * 1024
APPEND_OPTIONS = os.environ.get("APPEND_OPTIONS", "0") == "1"

IMG_URI = re.compile(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=\s]+$", re.I)


class Bad(Exception):
    """契约错误 → 422"""


# ---------------- 工具 ----------------
def body_bytes(uri: str) -> int:
    return len(uri.split(",", 1)[-1]) * 3 // 4


def norm_content(content, field: str = "content") -> tuple[str, list[str]]:
    """把 str 或 ContentPart[] 归一化为 (文本, 图片dataURI列表)。"""
    if isinstance(content, str):
        if not content.strip():
            raise Bad(f"{field} 不得为空字符串")
        return content, []
    if not isinstance(content, list) or not content:
        raise Bad(f"{field} 必须是字符串或非空数组")
    texts, images = [], []
    for part in content:
        if not isinstance(part, dict):
            raise Bad(f"{field} 的每个 part 必须是对象")
        t = part.get("type")
        if t == "text":
            v = part.get("text")
            if not isinstance(v, str) or not v.strip():
                raise Bad(f"{field} 中 type=text 必须带非空 text")
            texts.append(v)
        elif t == "image_url":
            iu = part.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else None
            if not isinstance(url, str) or not IMG_URI.match(url):
                raise Bad(f"{field} 中 image_url.url 必须是内联 Base64 data URI（不接受远程 URL）")
            if body_bytes(url) > MAX_IMG_BYTES:
                raise Bad(f"{field} 中单图超过 10 MiB")
            images.append(url)
        else:
            raise Bad(f"{field} 中出现不支持的 type: {t!r}")
    text = "\n".join(texts).strip()
    if not text and not images:
        raise Bad(f"{field} 没有任何内容")
    return (text or "(image only)"), images


def split_text(text: str, limit: int = 3000) -> list[str]:
    """长文本切成多段，便于逐段向量化（embedding 单行有 token 上限）。"""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[。！？.!?\n])", text):
        if len(cur) + len(sent) > limit and cur:
            parts.append(cur)
            cur = sent
        else:
            cur += sent
    if cur.strip():
        parts.append(cur)
    return parts or [text]


def det_id(*parts) -> str:
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"mem_{h[:20]}"


def iso(ts_ms) -> str:
    import time
    if isinstance(ts_ms, (int, float)) and ts_ms > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------- 业务 ----------------
def do_add(req: dict) -> dict:
    for k in ("request_id", "messages", "user_id", "session_id"):
        if not req.get(k):
            raise Bad(f"缺少必填字段 {k}")
    msgs = req["messages"]
    if not isinstance(msgs, list) or not msgs:
        raise Bad("messages 必须是非空数组")

    cached = store.get_idempotent(req["user_id"], req["request_id"])
    if cached is not None:
        return cached

    pending, total_bytes = [], 0
    for mi, m in enumerate(msgs):
        if not isinstance(m, dict):
            raise Bad("messages 的每一项必须是对象")
        if m.get("role") not in ("user", "assistant"):
            raise Bad("messages[].role 必须是 user 或 assistant")
        text, images = norm_content(m.get("content"), f"messages[{mi}].content")
        total_bytes += sum(body_bytes(u) for u in images)
        if total_bytes > MAX_ADD_BYTES:
            raise Bad("单次 Add 的图片总量超过 30 MiB")
        ts = m.get("timestamp")
        created = iso(ts)
        seq_base = mi * 1000
        for si, seg in enumerate(split_text(text)):
            pending.append(
                {
                    "id": det_id(req["user_id"], req["session_id"], req["request_id"], mi, si),
                    "user_id": req["user_id"],
                    "session_id": req["session_id"],
                    "request_id": req["request_id"],
                    "seq": seq_base + si,
                    "text": seg,
                    "images": images if si == 0 else [],
                    "ts": ts if isinstance(ts, int) else None,
                    "created_at": created,
                }
            )

    vecs = embed.embed([p["text"] + (f" [images:{len(p['images'])}]" if p["images"] else "")
                        for p in pending])
    for p, v in zip(pending, vecs):
        p["vec"] = embed.pack(v)
    store.add_memories(pending)

    resp = {
        "success": True,
        "request_id": req["request_id"],
        "user_id": req["user_id"],
        "session_id": req["session_id"],
    }
    store.put_idempotent(req["user_id"], req["request_id"], resp)
    return resp


def do_search(req: dict) -> dict:
    for k in ("query", "user_id", "top_k"):
        if req.get(k) in (None, ""):
            raise Bad(f"缺少必填字段 {k}")
    if not isinstance(req["top_k"], int) or req["top_k"] <= 0:
        raise Bad("top_k 必须是正整数")

    qtext, _ = norm_content(req["query"], "query")
    if APPEND_OPTIONS and isinstance(req.get("options"), list):
        qtext += "\n" + "\n".join(str(o) for o in req["options"])

    docs = store.fetch_user(req["user_id"])          # 严格 user_id 隔离
    if not docs:
        return {"data": []}

    qvec = embed.embed([qtext])[0]
    ranked = retrieve.run(qvec, qtext, docs)

    items = []
    for i, score in ranked:
        d = docs[i]
        items.append({"id": d["id"], "text": d["text"], "images": d["images"],
                      "score": round(float(score), 6), "created_at": d["created_at"]})
    items = budget.clip(items, req["top_k"])

    data = []
    for it in items:
        if it["images"]:
            content = [{"type": "text", "text": it["text"]}]
            content += [{"type": "image_url", "image_url": {"url": u}} for u in it["images"]]
        else:
            content = it["text"]
        data.append({"id": it["id"], "content": content,
                     "score": it["score"], "created_at": it["created_at"]})
    return {"data": data}


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aml-mem/1.0"

    def log_message(self, fmt, *a):  # 精简日志
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def _send(self, code: int, obj: dict):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _auth(self) -> bool:
        if not AUTH_KEY:
            return True
        got = self.headers.get("X-Api-Key")
        if not got:
            auth = self.headers.get("Authorization", "")
            for scheme in ("Token ", "Bearer "):
                if auth.startswith(scheme):
                    got = auth[len(scheme):].strip()
                    break
        return got == AUTH_KEY

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise Bad("请求体为空")
        if n > MAX_BODY:
            raise Bad("请求体过大")
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            raise Bad("请求体不是合法 JSON")
        if not isinstance(obj, dict):
            raise Bad("请求体必须是 JSON 对象")
        return obj

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz", ""):
            return self._send(200, {"status": "ok", "embed": embed.info()})
        if self.path.rstrip("/") == "/stats":
            return self._send(200, {"memories": store.count(), "embed": embed.info(),
                                    "budget": {"tokens": budget.TOKEN_BUDGET,
                                               "bytes": budget.BYTE_BUDGET}})
        return self._send(404, {"detail": {"reason": "not found"}})

    def do_POST(self):
        path = self.path.rstrip("/")
        try:
            if path not in ("/add", "/search"):
                return self._send(404, {"detail": {"reason": "not found"}})
            if not self._auth():
                return self._send(401, {"detail": {"reason": "invalid credential"}})
            req = self._read_json()
            out = do_add(req) if path == "/add" else do_search(req)
            return self._send(200, out)
        except Bad as e:
            return self._send(422, {"detail": {"reason": str(e)}})
        except Exception as e:  # 明确 5xx，便于平台退避重试
            traceback.print_exc()
            return self._send(500, {"detail": {"reason": f"internal: {type(e).__name__}"}})


def main():
    store.init()
    print(f"[aml] listen {HOST}:{PORT}  embed={embed.info()}  auth={'on' if AUTH_KEY else 'off'}",
          flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
