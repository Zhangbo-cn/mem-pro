"""可选：Add 时为图片生成文字描述（caption / 可见文字），让图片能被文本检索命中。

为什么需要：服务只有文本 embedding（`text-embedding-v4`），图片本身不参与向量化。
平台通常会同时发送原图与 caption；但只有图片、没有任何文字的消息原先会被
向量化成占位符 "(image only)" ⇒ 几乎不可检索。

合规（research/14 §7.2）：开源方法榜 Add/Search 侧任何模型都只能是 `gpt-4o-mini`，
所以这里只允许该模型；OCR/caption 结果作为**附加的派生文本**，原文不改、不替换。

确定性（复现条款）：temperature=0 + 固定 prompt 版本 + 按 (图片 sha256, 模型, prompt 版本)
落盘缓存到 SQLite ⇒ 同一张图只调用一次，重放/重跑得到逐字相同的索引。

环境变量：
  CAPTION_MODE=none|openai      默认 none（不调用任何模型）
  OPENAI_API_KEY=sk-...          openai 模式必填
  OPENAI_BASE=https://api.openai.com/v1   兼容端点（Azure/代理需自行保证仍是 gpt-4o-mini）
  CAPTION_MODEL=gpt-4o-mini      非该模型时拒绝启动（开源榜硬约束）
  CAPTION_TIMEOUT=60
  CAPTION_MAX_TOKENS=300
  CAPTION_STRICT=1               1=失败则 Add 返回 503 让平台重试（保证索引可复现）；0=降级跳过
  CAPTION_DETAIL=low|high|auto   图片细节档位，默认 low（便宜且足够做检索）
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import dotenv

dotenv.load()

MODE = os.environ.get("CAPTION_MODE", "none").strip().lower()
API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com/v1").rstrip("/")
MODEL = os.environ.get("CAPTION_MODEL", "gpt-4o-mini").strip()
TIMEOUT = float(os.environ.get("CAPTION_TIMEOUT", "60"))
MAX_TOKENS = int(os.environ.get("CAPTION_MAX_TOKENS", "300"))
STRICT = os.environ.get("CAPTION_STRICT", "1") == "1"
DETAIL = os.environ.get("CAPTION_DETAIL", "low").strip().lower()
ALLOWED_MODELS = ("gpt-4o-mini",)

PROMPT_VERSION = "v1"
PROMPT = (
    "Describe this image for a personal memory search index. In 2-4 short factual "
    "sentences state: what it shows (objects, people count, place/scene type), any "
    "visible text verbatim (signs, receipts, code, labels, numbers, dates), and notable "
    "colors or attributes. Do not guess identities or facts that are not visible. "
    "Answer in the same language as any visible text; otherwise English."
)
MARKER = "[image description · auto-generated]"

_LOCK = threading.Lock()
_MEM: dict[str, str] = {}


class CaptionError(RuntimeError):
    pass


def enabled() -> bool:
    return MODE == "openai"


def check() -> tuple[bool, str]:
    """配置自检（不发请求）。"""
    if not enabled():
        return True, "关闭（CAPTION_MODE=none）"
    if MODEL not in ALLOWED_MODELS:
        return False, f"CAPTION_MODEL={MODEL} 不合规：开源方法榜只允许 {ALLOWED_MODELS}"
    if not API_KEY:
        return False, "CAPTION_MODE=openai 但未设置 OPENAI_API_KEY"
    return True, f"开启（{MODEL}, detail={DETAIL}, strict={int(STRICT)}）"


def image_sig(urls: list[str]) -> str:
    """有序图片列表的指纹：去重时用来区分「正文相同但图片不同」的记忆。"""
    if not urls:
        return ""
    h = hashlib.sha256()
    for u in urls:
        h.update(hashlib.sha256(u.split(",", 1)[-1].encode("ascii", "ignore")).digest())
    return h.hexdigest()[:24]


def _key(url: str) -> str:
    body = url.split(",", 1)[-1].encode("ascii", "ignore")
    return f"{hashlib.sha256(body).hexdigest()}|{MODEL}|{PROMPT_VERSION}|{DETAIL}"


# ---------- 持久缓存（与记忆同库，复现时零调用） ----------
def _db():
    import store  # 延迟导入，避免循环依赖
    c = sqlite3.connect(store.DB_PATH, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("CREATE TABLE IF NOT EXISTS captions(key TEXT PRIMARY KEY, text TEXT NOT NULL,"
              " created_at TEXT NOT NULL)")
    return c


def _cache_get(key: str) -> str | None:
    with _LOCK:
        if key in _MEM:
            return _MEM[key]
    with _db() as c:
        row = c.execute("SELECT text FROM captions WHERE key=?", (key,)).fetchone()
    if row:
        with _LOCK:
            _MEM[key] = row[0]
        return row[0]
    return None


def _cache_put(key: str, text: str) -> None:
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO captions(key,text,created_at) VALUES(?,?,?)",
                  (key, text, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    with _LOCK:
        _MEM[key] = text


def _call(url: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "temperature": 0,
        "seed": 7,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": url, "detail": DETAIL}},
        ]}],
    }).encode("utf-8")
    last = None
    for attempt in range(4):
        req = urllib.request.Request(
            f"{BASE}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload = json.loads(r.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise CaptionError("empty caption")
            return " ".join(text.split())
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                detail = e.read()[:300].decode("utf-8", "replace")
                raise CaptionError(f"caption HTTP {e.code}: {detail}") from e
            time.sleep(min(20.0, 1.0 * (2 ** attempt)))
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
            last = e
            time.sleep(min(20.0, 1.0 * (2 ** attempt)))
    raise CaptionError(f"caption failed: {last}")


def describe(urls: list[str]) -> list[str]:
    """返回与 urls 等长的描述（未开启时全为空串）。STRICT 下失败抛 CaptionError。"""
    if not enabled() or not urls:
        return [""] * len(urls)
    out = []
    for u in urls:
        key = _key(u)
        got = _cache_get(key)
        if got is None:
            try:
                got = _call(u)
            except CaptionError:
                if STRICT:
                    raise
                out.append("")
                continue
            _cache_put(key, got)
        out.append(got)
    return out


def info() -> dict:
    return {"mode": MODE, "model": MODEL if enabled() else None,
            "prompt": PROMPT_VERSION if enabled() else None}
