"""AML Add / Search 服务（纯标准库实现，无需 pip 安装）。

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
  python3 service/server.py                            # 读取 .env / 环境变量
  EMBED_MODE=hash python3 service/server.py            # 离线自测（覆盖 .env）
  EMBED_MODE=api DASHSCOPE_API_KEY=sk-xxx python3 service/server.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dotenv  # noqa: E402

dotenv.load()

import budget  # noqa: E402
import embed  # noqa: E402
import retrieve  # noqa: E402
import store  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
AUTH_KEY = os.environ.get("MEMORY_SYSTEM_KEY", "").strip()
MAX_BODY = 64 * 1024 * 1024
MAX_IMG_BYTES = 10 * 1024 * 1024
MAX_ADD_BYTES = 30 * 1024 * 1024
MAX_RESPONSE_BYTES = 30 * 1024 * 1024
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", str(budget.MAX_IMAGES)))
MAX_INFLIGHT = int(os.environ.get("MAX_INFLIGHT", "64"))
STARTUP_CHECK = os.environ.get("STARTUP_CHECK", "1") == "1"
APPEND_OPTIONS = os.environ.get("APPEND_OPTIONS", "0") == "1"
LOG_JSON = os.environ.get("LOG_JSON", "1") == "1"
SERVER_VERSION = "mem-pro/0.2.0"

IMG_URI = re.compile(r"^data:image/(jpeg|jpg|png|webp);base64,[A-Za-z0-9+/=\s]+$", re.I)


class Bad(Exception):
    """契约错误 → 422"""


class Upstream(Exception):
    """向量化等上游失败 → 503（平台会退避重试）"""


# ---------------- 并发闸门 / 幂等锁 ----------------
_INFLIGHT = threading.BoundedSemaphore(MAX_INFLIGHT) if MAX_INFLIGHT > 0 else None
_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


def request_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


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
    """长文本切成多段，便于逐段向量化（embedding 单行有 token 上限）。

    优先在句子边界切；硬上限由 EMBED_CHUNK_CHARS 控制（默认 3000 字符，
    对中文 ≈3k token、英文 ≈750 token，均远低于 text-embedding-v4 上限）。
    """
    limit = max(200, int(os.environ.get("EMBED_CHUNK_CHARS", str(limit))))
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
    if isinstance(ts_ms, (int, float)) and ts_ms > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def uid_hash(user_id: str) -> str:
    return hashlib.sha1(str(user_id).encode("utf-8")).hexdigest()[:12]


# ---------------- 业务 ----------------
def do_add(req: dict) -> dict:
    for k in ("request_id", "messages", "user_id", "session_id"):
        if not req.get(k):
            raise Bad(f"缺少必填字段 {k}")
    msgs = req["messages"]
    if not isinstance(msgs, list) or not msgs:
        raise Bad("messages 必须是非空数组")
    if not isinstance(req["request_id"], str) or len(req["request_id"]) > 512:
        raise Bad("request_id 必须是字符串且 ≤512 字符")
    if not isinstance(req["user_id"], str) or len(req["user_id"]) > 512:
        raise Bad("user_id 必须是字符串且 ≤512 字符")
    if not isinstance(req["session_id"], str) or len(req["session_id"]) > 512:
        raise Bad("session_id 必须是字符串且 ≤512 字符")

    lock = request_lock(f"{req['user_id']}\x00{req['request_id']}")
    with lock:
        cached = store.get_idempotent(req["user_id"], req["request_id"])
        if cached is not None:
            return cached

        pending, total_bytes = [], 0
        for mi, m in enumerate(msgs):
            if not isinstance(m, dict):
                raise Bad("messages 的每一项必须是对象")
            if m.get("role") not in ("user", "assistant"):
                raise Bad("messages[].role 必须是 user 或 assistant")
            if m.get("content") is None:
                raise Bad(f"messages[{mi}] 缺少 content")
            text, images = norm_content(m.get("content"), f"messages[{mi}].content")
            total_bytes += sum(body_bytes(u) for u in images)
            if total_bytes > MAX_ADD_BYTES:
                raise Bad("单次 Add 的图片总量超过 30 MiB")
            ts = m.get("timestamp")
            if ts is not None and not isinstance(ts, (int, float)):
                raise Bad(f"messages[{mi}].timestamp 必须是毫秒时间戳")
            created = iso(ts)
            for si, seg in enumerate(split_text(text)):
                pending.append(
                    {
                        "id": det_id(req["user_id"], req["session_id"],
                                     req["request_id"], mi, si),
                        "user_id": req["user_id"],
                        "session_id": req["session_id"],
                        "request_id": req["request_id"],
                        "seq": None,  # 由 store 分配该 user 的全局递增序号
                        "text": seg,
                        # 图片只挂在第一条分块上，避免同一图重复占用返回预算
                        "images": images if si == 0 else [],
                        "ts": ts if isinstance(ts, (int, float)) else None,
                        "created_at": created,
                    }
                )

        embed_input = [
            p["text"] + (f" [images:{len(p['images'])}]" if p["images"] else "")
            for p in pending
        ]
        try:
            vecs = embed.embed(embed_input)
        except Exception as e:  # 上游 / 配额 / 网络 → 让平台重试
            raise Upstream(f"embedding failed: {e}") from e
        if len(vecs) != len(pending):
            raise Upstream("embedding 返回条数不匹配")
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
    if not isinstance(req["user_id"], str):
        raise Bad("user_id 必须是字符串")
    if isinstance(req["top_k"], bool) or not isinstance(req["top_k"], int) or req["top_k"] <= 0:
        raise Bad("top_k 必须是正整数")

    qtext, _ = norm_content(req["query"], "query")
    if APPEND_OPTIONS and isinstance(req.get("options"), list):
        qtext += "\n" + "\n".join(str(o) for o in req["options"])

    docs = store.fetch_user(req["user_id"])          # 严格 user_id 隔离；不读图片
    if not docs:
        return {"data": []}

    try:
        qvec = embed.embed([qtext])[0]
    except Exception as e:
        raise Upstream(f"embedding failed: {e}") from e
    ranked = retrieve.run(qvec, qtext, docs)

    items = []
    for i, score in ranked[: req["top_k"]]:
        d = docs[i]
        items.append({"id": d["id"], "text": d["text"], "images": [],
                      "score": round(float(score), 6), "created_at": d["created_at"]})
    if items:
        images = store.fetch_images([it["id"] for it in items], req["user_id"])
        for it in items:
            it["images"] = images.get(it["id"], [])
    items = budget.clip(items, req["top_k"], MAX_IMAGES)

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
    server_version = SERVER_VERSION

    def log_message(self, fmt, *a):  # 交给结构化日志，屏蔽默认 Common Log
        pass

    def _send(self, code: int, obj: dict, headers: dict | None = None):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:  # 理论不可达；兜底防止契约违规
            payload = json.dumps({"data": []}, ensure_ascii=False).encode("utf-8")
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Server-Version", SERVER_VERSION)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _auth(self) -> bool:
        if not AUTH_KEY:
            return True
        got = self.headers.get("X-Api-Key", "") or ""
        if not got:
            auth = self.headers.get("Authorization", "")
            for scheme in ("Token ", "Bearer "):
                if auth.startswith(scheme):
                    got = auth[len(scheme):].strip()
                    break
        return hmac.compare_digest(got, AUTH_KEY)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Bad("Content-Length 非法")
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

    def _log(self, status: int, started: float, **extra):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "method": self.command, "path": self.path.split("?")[0],
               "status": status, "ms": round((time.time() - started) * 1000, 1),
               "req_id": getattr(self, "req_id", ""), **extra}
        if LOG_JSON:
            sys.stderr.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            sys.stderr.write(
                f"{rec['ts']} {rec['method']} {rec['path']} {status} {rec['ms']}ms"
                f" req={rec['req_id']}\n"
            )
        sys.stderr.flush()

    def do_GET(self):
        started = time.time()
        self.req_id = self.headers.get("X-Request-Id", "")[:128]
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/health", "/healthz", "/ready", ""):
            if not store.ping():
                self._log(503, started, reason="db_unavailable")
                return self._send(503, {"status": "error", "db": "unavailable"})
            self._log(200, started)
            return self._send(200, {"status": "ok", "embed": embed.info(),
                                    "db": "ok", "version": SERVER_VERSION})
        if path in ("/stats", "/version"):
            if not self._auth():
                self._log(401, started, reason="auth")
                self.close_connection = True  # 未读请求体，不能复用连接
                return self._send(401, {"detail": {"reason": "invalid credential"}},
                                  {"Connection": "close"})
            if path == "/version":
                self._log(200, started)
                return self._send(200, {"version": SERVER_VERSION, "embed": embed.info()})
            self._log(200, started)
            return self._send(200, {"memories": store.count(), "embed": embed.info(),
                                    "budget": {"tokens": budget.TOKEN_BUDGET,
                                               "bytes": budget.BYTE_BUDGET,
                                               "max_images": MAX_IMAGES},
                                    "max_inflight": MAX_INFLIGHT})
        self._log(404, started)
        return self._send(404, {"detail": {"reason": "not found"}})

    def do_POST(self):
        started = time.time()
        self.req_id = self.headers.get("X-Request-Id", "")[:128]
        path = self.path.split("?", 1)[0].rstrip("/")
        acquired = False
        try:
            if path not in ("/add", "/search"):
                self._log(404, started)
                return self._send(404, {"detail": {"reason": "not found"}})
            if not self._auth():
                self._log(401, started, reason="auth")
                self.close_connection = True  # 未读请求体，不能复用连接
                return self._send(401, {"detail": {"reason": "invalid credential"}},
                                  {"Connection": "close"})
            if _INFLIGHT is not None:
                acquired = _INFLIGHT.acquire(blocking=False)
                if not acquired:
                    self._log(429, started, reason="inflight_limit")
                    self.close_connection = True  # 未读请求体，不能复用连接
                    return self._send(429, {"detail": {"reason": "too many in-flight requests"}},
                                      {"Retry-After": "1", "Connection": "close"})
            req = self._read_json()
            out = do_add(req) if path == "/add" else do_search(req)
            self._log(200, started, user=uid_hash(str(req.get("user_id", ""))),
                      items=len(out.get("data", [])) if isinstance(out, dict) else None)
            return self._send(200, out)
        except Bad as e:
            self._log(422, started, reason=str(e)[:200])
            return self._send(422, {"detail": {"reason": str(e)}})
        except Upstream as e:
            self._log(503, started, reason=str(e)[:200])
            return self._send(503, {"detail": {"reason": str(e)}}, {"Retry-After": "5"})
        except Exception as e:  # 明确 5xx，便于平台退避重试
            traceback.print_exc()
            self._log(500, started, reason=type(e).__name__)
            return self._send(500, {"detail": {"reason": f"internal: {type(e).__name__}"}})
        finally:
            if acquired and _INFLIGHT is not None:
                _INFLIGHT.release()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    store.init()
    ok, msg = embed.ping() if (STARTUP_CHECK and embed.MODE == "api") else (True, "hash 模式")
    if not ok:
        print(f"[mem-pro] 启动失败：{msg}", file=sys.stderr, flush=True)
        sys.exit(2)
    srv = Server((HOST, PORT), Handler)

    def _stop(signum, _frame):
        print(f"[mem-pro] signal {signum}，正在关闭…", file=sys.stderr, flush=True)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)

    print(f"[mem-pro] listen {HOST}:{PORT}  embed={embed.info()}  "
          f"auth={'on' if AUTH_KEY else 'off'}  max_inflight={MAX_INFLIGHT}", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        print("[mem-pro] stopped", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
