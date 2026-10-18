"""text-embedding-v4 客户端（DashScope / 阿里云百炼）+ 离线 hash 模式。

- 只用标准库（urllib），无需 pip 安装
- EMBED_MODE=hash 时完全离线、确定性，用于开发与契约测试
- EMBED_MODE=api 时调用 DashScope 的 OpenAI 兼容端点
- 进程内 LRU 缓存：同一文本（重复 caption / 重试）只付一次钱

环境变量：
  EMBED_MODE=api|hash        默认 hash
  DASHSCOPE_API_KEY=sk-xxx    api 模式必填
  EMBED_MODEL=text-embedding-v4
  EMBED_DIM=1024
  EMBED_BATCH=10             text-embedding-v4 单次最多 10 行
  DASHSCOPE_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import threading
import time
import urllib.error
import urllib.request

import dotenv

dotenv.load()

MODE = os.environ.get("EMBED_MODE", "hash").strip().lower()
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
MODEL = os.environ.get("EMBED_MODEL", "text-embedding-v4")
DIM = int(os.environ.get("EMBED_DIM", "1024"))
BASE = os.environ.get("DASHSCOPE_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
BATCH = max(1, min(10, int(os.environ.get("EMBED_BATCH", "10"))))  # 官方上限 10 行
CACHE_MAX = int(os.environ.get("EMBED_CACHE_MAX", "20000"))
_CACHE: dict[str, list[float]] = {}
_CACHE_LOCK = threading.Lock()


# ---------- 向量编解码：float32 blob，省空间 ----------
def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def norm(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


# ---------- 离线确定性 embedding：词袋哈希 ----------
def _hash_embed(text: str, dim: int = DIM) -> list[float]:
    """词袋哈希：英文按词，中文按单字 + bigram（无分词依赖）。"""
    vec = [0.0] * dim
    toks = text.lower().split()
    cjk = [ch for ch in text if "\u4e00" <= ch <= "\u9fff"]
    if cjk:
        toks += cjk
        toks += [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    if not toks:
        s = text.strip()
        toks = [s[i : i + 2] for i in range(max(1, len(s) - 1))]
    for t in toks:
        h = hashlib.blake2b(t.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    return norm(vec)


# ---------- DashScope 调用 ----------
def _call_api(texts: list[str]) -> list[list[float]]:
    if not API_KEY:
        raise RuntimeError("EMBED_MODE=api 但未设置 DASHSCOPE_API_KEY")
    body = json.dumps(
        {"model": MODEL, "input": texts, "dimensions": DIM, "encoding_format": "float"},
        ensure_ascii=False,
    ).encode("utf-8")
    last = None
    for attempt in range(5):
        req = urllib.request.Request(
            f"{BASE}/embeddings",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8"))
            data = payload["data"]
            data.sort(key=lambda d: d["index"])  # 保证顺序
            return [d["embedding"] for d in data]
        except urllib.error.HTTPError as e:
            last = e
            # 429 / 5xx 才值得退避；4xx（鉴权、参数）重试没有意义
            if e.code != 429 and e.code < 500:
                detail = e.read()[:300].decode("utf-8", "replace")
                raise RuntimeError(f"embedding 调用失败 HTTP {e.code}: {detail}") from e
            retry_after = e.headers.get("Retry-After") if e.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.8 * (2**attempt)
            time.sleep(min(delay, 30))
        except (urllib.error.URLError, TimeoutError, KeyError) as e:  # 网络/超时/响应异常
            last = e
            time.sleep(0.8 * (2**attempt))
    raise RuntimeError(f"embedding 调用失败: {last}")


def _cache_key(text: str) -> str:
    return hashlib.sha1(f"{MODE}|{MODEL}|{DIM}|{text}".encode("utf-8")).hexdigest()


def embed(texts: list[str]) -> list[list[float]]:
    """返回与输入等长的向量列表；内部按 BATCH 条一批，带进程内缓存。"""
    if not texts:
        return []
    keys = [_cache_key(t) for t in texts]
    out: list[list[float] | None] = [None] * len(texts)

    with _CACHE_LOCK:
        for i, k in enumerate(keys):
            if k in _CACHE:
                out[i] = _CACHE[k]

    todo = [i for i, v in enumerate(out) if v is None]
    for start in range(0, len(todo), BATCH):
        idx = todo[start : start + BATCH]
        batch = [texts[i] for i in idx]
        vecs = [norm(v) for v in _call_api(batch)] if MODE == "api" else [_hash_embed(t) for t in batch]
        if len(vecs) != len(idx):
            raise RuntimeError(f"embedding 返回条数不匹配: {len(vecs)} != {len(idx)}")
        for i, v in zip(idx, vecs):
            out[i] = v

    result = [v if v is not None else [] for v in out]
    with _CACHE_LOCK:
        if len(_CACHE) > CACHE_MAX:
            _CACHE.clear()  # 简单策略：整表清空，避免无界增长
        for k, v in zip(keys, result):
            _CACHE[k] = v
    return result


def ping() -> tuple[bool, str]:
    """真实调用一次 embedding，用于 doctor / 启动自检。"""
    if MODE != "api":
        return True, "hash 模式（离线，无需 API）"
    if not API_KEY:
        return False, "缺少 DASHSCOPE_API_KEY"
    try:
        vec = embed(["mem-pro connectivity check"])[0]
    except Exception as e:  # noqa: BLE001 - 自检需要把原因原样带出
        return False, str(e)
    if len(vec) != DIM:
        return False, f"返回维度 {len(vec)} != EMBED_DIM {DIM}"
    return True, f"OK（{MODEL} / dim={len(vec)}）"


def info() -> dict:
    return {"mode": MODE, "model": MODEL if MODE == "api" else "hash-bow", "dim": DIM,
            "cache": len(_CACHE)}
