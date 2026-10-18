"""text-embedding-v4 客户端（DashScope / 阿里云百炼）+ 离线 hash 模式。

- 只用标准库（urllib），无需 pip 安装
- EMBED_MODE=hash 时完全离线、确定性，用于开发与契约测试
- EMBED_MODE=api 时调用 DashScope 的 OpenAI 兼容端点

环境变量：
  EMBED_MODE=api|hash        默认 hash
  DASHSCOPE_API_KEY=sk-xxx    api 模式必填
  EMBED_MODEL=text-embedding-v4
  EMBED_DIM=1024
  DASHSCOPE_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import time
import urllib.error
import urllib.request

MODE = os.environ.get("EMBED_MODE", "hash").strip().lower()
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
MODEL = os.environ.get("EMBED_MODEL", "text-embedding-v4")
DIM = int(os.environ.get("EMBED_DIM", "1024"))
BASE = os.environ.get("DASHSCOPE_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
BATCH = 10  # text-embedding-v4 单次最多 10 行（官方文档）


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
    vec = [0.0] * dim
    toks = text.lower().split()
    if not toks:  # 中文没有空格，用字符 bigram 兜底
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
    req = urllib.request.Request(
        f"{BASE}/embeddings",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST",
    )
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8"))
            data = payload["data"]
            data.sort(key=lambda d: d["index"])  # 保证顺序
            return [d["embedding"] for d in data]
        except (urllib.error.URLError, TimeoutError, KeyError) as e:  # 有限退避重试
            last = e
            time.sleep(0.6 * (2**attempt))
    raise RuntimeError(f"embedding 调用失败: {last}")


def embed(texts: list[str]) -> list[list[float]]:
    """返回与输入等长的向量列表；内部按 10 条一批。"""
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        batch = texts[i : i + BATCH]
        if MODE == "api":
            vecs = [norm(v) for v in _call_api(batch)]
        else:
            vecs = [_hash_embed(t) for t in batch]
        out.extend(vecs)
    return out


def info() -> dict:
    return {"mode": MODE, "model": MODEL if MODE == "api" else "hash-bow", "dim": DIM}
