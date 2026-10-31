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
import subprocess
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
import caption  # noqa: E402
import embed  # noqa: E402
import imgsize  # noqa: E402
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
# 返回条数上限（0 = 老实按平台给的 top_k，默认）。契约只要求「不得超过 top_k」，收窄合法。
# 端到端答案评测（tools/e2e_proxy.py，1,711 条 QA，qwen-flash 作答）：
#   top_k  5 / 10 / 20 / 50 / 100 → token-F1 0.504 / 0.527 / 0.521 / 0.514 / 0.505
# 但若评分对象是 Search 输出本身（研究笔记 08/09/10 的口径），收窄到 20 会让
# R@100 0.70→0.35。两种口径下最优值相反，官方答复前默认不收窄（见 README §十六）。
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "0"))
MAX_INFLIGHT = int(os.environ.get("MAX_INFLIGHT", "64"))
STARTUP_CHECK = os.environ.get("STARTUP_CHECK", "1") == "1"
APPEND_OPTIONS = os.environ.get("APPEND_OPTIONS", "0") == "1"
# 向量化时拼接同一 Add 内前后各 N 条消息作为上下文（0=关闭）；只影响向量，不影响返回正文
EMBED_CONTEXT = int(os.environ.get("EMBED_CONTEXT", "0"))
EMBED_CONTEXT_CHARS = int(os.environ.get("EMBED_CONTEXT_CHARS", "400"))
# 记忆单元与正文抬头（见 _units）：message|round；1=正文带「[日期 时间] 角色:」
# /add、/search（已绑定的 v1.0.0）保持历史行为；/v2/add、/v2/search 用 V2_PROFILE。
MEMORY_UNIT = os.environ.get("MEMORY_UNIT", "message").strip().lower()
CONTENT_HEADER = os.environ.get("CONTENT_HEADER", "0") == "1"
# HEADER_SESSION=1：抬头里再加会话标识，例如 `[2024-06-17 09:00 D1] user: …`。
# 为什么：基准的许多问题（「哪张图是 6-17 那次对话里的」「跨会话有没有矛盾」）答案本身
# 就带会话身份，正文里没有会话标识时回答模型只能猜（本地 VS 类 EM 因此长期接近 0）。
HEADER_SESSION = os.environ.get("HEADER_SESSION", "0") == "1"
V2_PROFILE = {"unit": "round", "header": True, "session": HEADER_SESSION}
ROUTES = {"/add": ("add", None), "/search": ("search", None),
          "/v2/add": ("add", V2_PROFILE), "/v2/search": ("search", V2_PROFILE)}
V2_NS = "\x1fv2\x1f"   # 不能用 \x00：SQLite 的 LIKE/比较遇 NUL 截断
LOG_JSON = os.environ.get("LOG_JSON", "1") == "1"
SERVER_VERSION = "mem-pro/0.2.0"


def _commit() -> str:
    """线上跑的到底是哪份代码。镜像里没有 .git，构建时用 GIT_COMMIT 注入。"""
    sha = os.environ.get("GIT_COMMIT", "").strip()
    if sha:
        return sha[:12]
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return out.stdout.strip() + ("-dirty" if subprocess.run(
                ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True, timeout=2).stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


_RUNTIME_COUNTERS = ("calls", "fails", "degraded", "cooldown_skips")


def _config_fingerprint(profile: dict | None = None) -> str:
    """影响输出字节的配置指纹：改了任何一个都会改变它。

    检索侧直接取 `retrieve.info()` 全量（去掉运行时计数器），新加开关只要进了 info()
    就自动进指纹——之前手工列字段漏过 DIVERSITY / DATE_WEIGHT。
    """
    import retrieve
    r = retrieve.info()
    r["rerank"] = {k: v for k, v in r.get("rerank", {}).items() if k not in _RUNTIME_COUNTERS}
    ecfg = {"mode": embed.MODE, "model": embed.MODEL, "dim": embed.DIM}
    parts = [SERVER_VERSION, json.dumps(ecfg, sort_keys=True),
             json.dumps(r, sort_keys=True, default=str),
             "ctx:%d:%d" % (EMBED_CONTEXT, EMBED_CONTEXT_CHARS),
             "budget:%d:%d:%d:%d:%d" % (budget.TOKEN_BUDGET, budget.BYTE_BUDGET,
                                        MAX_IMAGES, MAX_RESULTS, budget.IMG_TOKEN_COST),
             "caption:%s:%s" % (caption.MODE, caption.MODEL),
             "imgsize:%d:%d" % (imgsize.MAX_SIDE, imgsize.QUALITY)]
    unit, header, with_session = _profile(profile)
    if (unit, header) != ("message", False):   # 历史配置不加这一段：v1 的 config_hash 保持不变
        parts.append("unit:%s:%d:%d" % (unit, header, with_session))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


COMMIT = _commit()

# 任意 image/* 子类型都收：ATM-Bench 是手机相册（heic/gif 常见），这里只存、只回传，
# 不解码，没理由因为子类型不认识就让整条 Add 失败（规则要求「所有评测任务均执行成功」）。
IMG_URI = re.compile(r"^data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+$", re.I)
# 宽进：空 text 段 / 未知 part 类型 / 非 data URI 的图片 → 跳过该 part 并计数，不拒整条。
# 只有整条 content 跳完之后什么都不剩才 422。计数在 /version 的 skipped_parts 里。
_SKIPPED: dict[str, int] = {"empty_text": 0, "unknown_type": 0, "bad_image": 0}
_SKIPPED_LOCK = threading.Lock()


def _skip(kind: str) -> None:
    with _SKIPPED_LOCK:
        _SKIPPED[kind] += 1


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
                _skip("empty_text")          # 平台「原图 + caption」里 caption 可能为空
                continue
            texts.append(v)
        elif t == "image_url":
            iu = part.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if not isinstance(url, str) or not IMG_URI.match(url):
                _skip("bad_image")           # 远程 URL / 非图片：不抓取，跳过
                continue
            if body_bytes(url) > MAX_IMG_BYTES:
                raise Bad(f"{field} 中单图超过 10 MiB")
            images.append(url)
        else:
            _skip("unknown_type")
            continue
    text = "\n".join(texts).strip()
    if not text and not images:
        raise Bad(f"{field} 没有任何可用内容（需要非空 text 或内联 Base64 图片）")
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


def _profile(profile: dict | None) -> tuple[str, bool, bool]:
    if profile is None:
        return MEMORY_UNIT, CONTENT_HEADER, HEADER_SESSION
    return profile["unit"], bool(profile["header"]), bool(profile.get("session"))


def sess_label(session_id) -> str:
    """抬头里的会话标识：短 id 原样用（基准的 `D1`／`D2`），长 id（平台 uuid）折叠成 7 字符。"""
    sid = str(session_id or "")
    if not sid:
        return ""
    if len(sid) <= 16:
        return sid
    return "s" + hashlib.sha1(sid.encode("utf-8")).hexdigest()[:6]


def _units(normed: list[dict], profile: dict | None = None,
           session_id=None) -> list[dict]:
    """消息 → 记忆单元。

    MEMORY_UNIT=message：一条消息一条记忆（历史行为）。
    MEMORY_UNIT=round：把 user 消息与紧随其后的 assistant 回复合成一条——基准的证据粒度
    （Mem-Gallery 的 clue `D1:3`）就是「一轮」，单条 assistant 回复脱离提问几乎不可读，
    单条 user 消息又丢了对方补充的名字/结论。图片、时间戳跟随 user 消息。

    CONTENT_HEADER=1 时正文前加 `[YYYY-MM-DD HH:MM] user:` 抬头：平台 Answer 只看 content，
    不看 created_at，没有日期就答不了先后/跨会话题，没有说话人就分不清谁说的。
    抬头只是来源元数据（时间+角色），不改写原文。
    """
    unit, header, with_session = _profile(profile)
    sid = sess_label(session_id) if with_session else ""
    out: list[dict] = []
    for m in normed:
        prev = out[-1] if out else None
        if (unit == "round" and prev is not None and m["role"] == "assistant"
                and prev["roles"] == ["user"]):
            prev["roles"].append("assistant")
            prev["parts"].append(("assistant", m["text"]))
            prev["images"] = prev["images"] + m["images"]
            continue
        out.append({"mi": m["mi"], "roles": [m["role"]], "parts": [(m["role"], m["text"])],
                    "images": list(m["images"]), "ts": m["ts"], "created": m["created"]})
    for u in out:
        lines = []
        for k, (role, text) in enumerate(u["parts"]):
            if header:
                stamp = f"{u['created'][:10]} {u['created'][11:16]}"
                if sid:
                    stamp += f" {sid}"
                head = f"[{stamp}] " if k == 0 and u["ts"] else ""
                lines.append(f"{head}{role}: {text}")
            else:
                lines.append(text)
        u["text"] = "\n".join(lines)
    return out


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
def plan_add(req: dict, profile: dict | None = None) -> tuple[list[dict], list[str]]:
    """校验并展开一次 Add：返回待写入的记忆行与对应的向量化文本（不调用 embedding）。

    tools/gallery_proxy 的预热复用它，保证预热文本与真实写入逐字一致。
    """
    msgs = req["messages"]
    normed, total_bytes = [], 0
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
        if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (int, float))):
            raise Bad(f"messages[{mi}].timestamp 必须是毫秒时间戳")
        created = iso(ts)
        if images:
            try:
                descs = [d for d in caption.describe(images) if d]
            except caption.CaptionError as e:
                raise Upstream(str(e)) from e
            if descs:
                # 派生文本只追加并显式标注，原文一字不改
                derived = "\n".join(f"{caption.MARKER} {d}" for d in descs)
                text = derived if text == "(image only)" else f"{text}\n{derived}"
        normed.append({"mi": mi, "role": m["role"], "text": text, "images": images,
                       "ts": ts if isinstance(ts, (int, float)) else None,
                       "created": created})

    pending = []
    msg_texts: list[tuple[int, str]] = []
    for u in _units(normed, profile, req.get("session_id")):
        mi, text, images, ts, created = u["mi"], u["text"], u["images"], u["ts"], u["created"]
        sig = caption.image_sig(images)
        msg_texts.append((mi, text))
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
                    "img_sig": sig if si == 0 else "",
                    "ts": ts,
                    "created_at": created,
                    "_mi": mi,
                }
            )

    # 上下文化向量：只改「用于向量化的文本」，返回的正文仍是原文。
    # 单条 assistant 回复（"好的，发我看看"）自身几乎没有语义，拼上同一 Add
    # 内的相邻消息后才可能被问题命中。默认关闭：hash 代理榜上 R@100 反降 0.03，需用真实 embedding 复测。
    embed_input = []
    for k, p in enumerate(pending):
        body = p["text"] + (f" [images:{len(p['images'])}]" if p["images"] else "")
        if EMBED_CONTEXT > 0:
            mi = p["_mi"]
            ctx = [t for j, t in msg_texts if j != mi and abs(j - mi) <= EMBED_CONTEXT]
            if ctx:
                body = body + "\n[context] " + " ".join(c[:EMBED_CONTEXT_CHARS] for c in ctx)
        embed_input.append(body)
    for p in pending:
        p.pop("_mi", None)
    return pending, embed_input


def do_add(req: dict, profile: dict | None = None) -> dict:
    for k in ("request_id", "messages", "user_id", "session_id"):
        if not req.get(k):
            raise Bad(f"缺少必填字段 {k}")
    msgs = req["messages"]
    if not isinstance(msgs, list) or not msgs:
        raise Bad("messages 必须是非空数组")
    if not isinstance(req["request_id"], str) or len(req["request_id"]) > 512:
        raise Bad("request_id 必须是字符串且 ≤512 字符")
    if not isinstance(req["user_id"], str) or len(req["user_id"].removeprefix(V2_NS)) > 512:
        raise Bad("user_id 必须是字符串且 ≤512 字符")
    if not isinstance(req["session_id"], str) or len(req["session_id"]) > 512:
        raise Bad("session_id 必须是字符串且 ≤512 字符")

    lock = request_lock(f"{req['user_id']}\x00{req['request_id']}")
    with lock:
        cached = store.get_idempotent(req["user_id"], req["request_id"])
        if cached is not None:
            return cached

        pending, embed_input = plan_add(req, profile)
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


def do_search(req: dict, timings: dict | None = None) -> dict:
    timings = timings if timings is not None else {}
    for k in ("query", "user_id", "top_k"):
        if req.get(k) in (None, ""):
            raise Bad(f"缺少必填字段 {k}")
    if not isinstance(req["user_id"], str):
        raise Bad("user_id 必须是字符串")
    if isinstance(req["top_k"], bool) or not isinstance(req["top_k"], int) or req["top_k"] <= 0:
        raise Bad("top_k 必须是正整数")

    qtext, qimages = norm_content(req["query"], "query")
    if qimages and caption.enabled():
        # 以图搜记忆：把查询图也转成同一口径的描述（同样只用 gpt-4o-mini，结果缓存）
        try:
            descs = [d for d in caption.describe(qimages) if d]
        except caption.CaptionError as e:
            raise Upstream(str(e)) from e
        if descs:
            joined = "\n".join(descs)
            qtext = joined if qtext == "(image only)" else f"{qtext}\n{joined}"
    if APPEND_OPTIONS and isinstance(req.get("options"), list):
        qtext += "\n" + "\n".join(str(o) for o in req["options"])

    started = time.perf_counter()
    store_stats = {}
    docs = store.fetch_user(req["user_id"], stats=store_stats)
    timings["search_store_ms"] = round((time.perf_counter() - started) * 1000, 2)
    timings["search_store_cache_hit"] = store_stats.get("cache_hit", False)
    timings["search_docs"] = len(docs)
    if not docs:
        timings["search_results"] = 0
        return {"data": []}

    started = time.perf_counter()
    try:
        qvec = embed.embed([qtext])[0]
    except Exception as e:
        raise Upstream(f"embedding failed: {e}") from e
    finally:
        timings["search_embed_ms"] = round((time.perf_counter() - started) * 1000, 2)

    rank_stats = {}
    started = time.perf_counter()
    try:
        ranked = retrieve.run(qvec, qtext, docs, limit=req["top_k"],
                              cache_key=(store.DB_PATH, req["user_id"]), stats=rank_stats)
    finally:
        timings["search_rank_ms"] = round((time.perf_counter() - started) * 1000, 2)
    timings["search_index_cache_hit"] = rank_stats.get("index_cache_hit", False)
    if rank_stats.get("index_extended"):
        timings["search_index_extended"] = True
    timings["search_results"] = len(ranked)

    items = []
    for i, score in ranked:
        d = docs[i]
        items.append({"id": d["id"], "text": d["text"], "images": [],
                      "score": round(float(score), 6), "created_at": d["created_at"]})
    started = time.perf_counter()
    if items:
        # 只取够用的图片：budget.clip 最多放 MAX_IMAGES 张，按顺序分批取、够数就停。
        # 之前一次取 100 条的全部 Base64（真图实测单次可达几十 MiB），16 并发时 RSS 1.2 GiB。
        # 预算裁掉图片时（字节/ token 超限）后面的条目才可能补位，所以多取一批余量。
        need = MAX_IMAGES * 2 if MAX_IMAGES > 0 else 0
        got = 0
        for start in range(0, len(items), 10):
            if got >= need:
                break
            chunk = items[start:start + 10]
            images = store.fetch_images([it["id"] for it in chunk], req["user_id"])
            for it in chunk:
                got_list = images.get(it["id"], [])
                if imgsize.enabled():
                    got_list = [imgsize.shrink_uri(u) for u in got_list]
                it["images"] = got_list
                got += len(got_list)
    timings["search_images_ms"] = round((time.perf_counter() - started) * 1000, 2)

    started = time.perf_counter()
    if MAX_RESULTS > 0:
        items = items[:MAX_RESULTS]
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
    timings["search_format_ms"] = round((time.perf_counter() - started) * 1000, 2)
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
        if path == "/v2" or path.startswith("/v2/"):
            path = path[3:]  # /v2/health 等与 v1 共用（契约/烟测工具以 BASE+路径 拼接）
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
                return self._send(200, {"version": SERVER_VERSION, "commit": COMMIT,
                                        "config_hash": _config_fingerprint(),
                                        "config_hash_v2": _config_fingerprint(V2_PROFILE),
                                        "v2_profile": V2_PROFILE,
                                        "imgsize": imgsize.info(),
                                        "embed": embed.info(),
                                        "caption": caption.info(),
                                        "max_results": MAX_RESULTS,
                                        "skipped_parts": dict(_SKIPPED),
                                        "retrieval": retrieve.info()})
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
        search_timings = {}
        try:
            if path not in ROUTES:
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
            op, profile = ROUTES[path]
            if profile is not None and isinstance(req.get("user_id"), str) and req["user_id"]:
                # v2 的记忆按单元粒度不同，不能和 v1 写进同一个 user 空间（也避免幂等缓存串用）
                uid = req["user_id"]
                req = {**req, "user_id": V2_NS + uid}
            else:
                uid = None
            if op == "add":
                out = do_add(req, profile)
                if uid is not None:
                    out = {**out, "user_id": uid}
            else:
                out = do_search(req, search_timings)
            self._log(200, started, user=uid_hash(str(req.get("user_id", ""))),
                      items=len(out.get("data", [])) if isinstance(out, dict) else None,
                      **search_timings)
            return self._send(200, out)
        except Bad as e:
            self._log(422, started, **search_timings, reason=str(e)[:200])
            return self._send(422, {"detail": {"reason": str(e)}})
        except Upstream as e:
            self._log(503, started, **search_timings, reason=str(e)[:200])
            return self._send(503, {"detail": {"reason": str(e)}}, {"Retry-After": "5"})
        except Exception as e:  # 明确 5xx，便于平台退避重试
            traceback.print_exc()
            self._log(500, started, **search_timings, reason=type(e).__name__)
            return self._send(500, {"detail": {"reason": f"internal: {type(e).__name__}"}})
        finally:
            if acquired and _INFLIGHT is not None:
                _INFLIGHT.release()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 默认 listen backlog 只有 5：平台按申报并发同时建连时 SYN 会被丢，
    # 客户端 1s/3s 重传 ⇒ p95 飙到秒级（实测 32 并发 p95 1.2s）。
    request_queue_size = int(os.environ.get("LISTEN_BACKLOG", "512"))


def main():
    store.init()
    ok_cap, cap_msg = caption.check()
    if not ok_cap:
        print(f"[mem-pro] 启动失败：{cap_msg}", file=sys.stderr, flush=True)
        sys.exit(2)
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
          f"auth={'on' if AUTH_KEY else 'off'}  max_inflight={MAX_INFLIGHT}  "
          f"caption={caption.info()}  rerank={retrieve.RERANK_MODE}", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        print("[mem-pro] stopped", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
