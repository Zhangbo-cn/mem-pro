"""返回侧预算：官方两条硬墙 + 图片配额。

1) Answer 共享 128,000 token 窗口，扣掉 8,192 输出 + 2,048 安全 = 117,760 输入；
   超出时平台「按返回顺序保留前缀」。⇒ 顺序即存活性，尾部是纯浪费。
2) Search 响应 ≤ 30 MiB，单图 ≤ 10 MiB，图片必须内联 Base64。

因此返回前必须做三重裁剪：条数 / token / 字节。字节按 **实际传输的
Base64 字符数** 计（解码后体积只是单图上限的判据），否则会低估 33%。
"""
from __future__ import annotations

import re

ANSWER_INPUT_TOKENS = 117_760
TOKEN_BUDGET = int(ANSWER_INPUT_TOKENS * 0.93)      # 留 ~7k 给问题/选项/指令
BYTE_BUDGET = 28 * 1024 * 1024                      # 30 MiB 留 2 MiB 余量
IMG_TOKEN_COST = 2_500                              # 2MP 图片约 2.5k visual tokens
MAX_IMAGES = 6                                      # 单次响应的图片配额（可被 server 覆盖）
MAX_IMG_BYTES = 10 * 1024 * 1024                    # 单图 10 MiB（decoded）

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")


def est_tokens(text: str) -> int:
    """粗估 token：CJK 1 字≈1 token，ASCII 4 字符≈1 token。"""
    cjk = len(_CJK.findall(text))
    ascii_chars = len(text) - cjk
    return cjk + max(1, ascii_chars // 4)


def data_uri_bytes(uri: str) -> int:
    """data:image/...;base64,XXXX → 解码后字节数估计（单图 10 MiB 判据）。"""
    b64 = uri.split(",", 1)[-1]
    return len(b64) * 3 // 4


def wire_bytes(uri: str) -> int:
    """该图片在响应体里实际占用的字节数（Base64 字符串长度）。"""
    return len(uri.encode("utf-8"))


def _fit_text(text: str, token_left: int, byte_left: int) -> str:
    """首条就超预算时截断正文（只发生在异常大的单条记忆上）。"""
    if token_left <= 0 or byte_left <= 0:
        return ""
    limit = min(len(text), max(16, token_left), max(16, byte_left))
    return text[:limit]


def clip(items: list[dict], max_items: int, max_images: int = MAX_IMAGES) -> list[dict]:
    """items: [{text, images[], ...}] 已按优先级排序；返回预算内前缀。"""
    out: list[dict] = []
    tok = 0
    byt = 0
    imgs = 0
    for it in items:
        if len(out) >= max_items:
            break
        text = it["text"]
        t = est_tokens(text)
        b = len(text.encode("utf-8"))
        pictures = list(it.get("images") or [])
        n_img = len(pictures)
        if imgs + n_img > max_images:
            n_img = max(0, max_images - imgs)
            pictures = pictures[:n_img]

        # 图片优先丢弃：宁可少给图，也不要让整条证据消失
        while pictures and (
            tok + t + len(pictures) * IMG_TOKEN_COST > TOKEN_BUDGET
            or byt + b + sum(wire_bytes(u) for u in pictures) > BYTE_BUDGET
        ):
            pictures.pop()

        over = tok + t + len(pictures) * IMG_TOKEN_COST > TOKEN_BUDGET or (
            byt + b + sum(wire_bytes(u) for u in pictures) > BYTE_BUDGET
        )
        if over:
            if out:
                break  # 前缀装不下，后续更小条目也没意义（顺序即优先级）
            text = _fit_text(
                text,
                TOKEN_BUDGET - tok - len(pictures) * IMG_TOKEN_COST,
                BYTE_BUDGET - byt - sum(wire_bytes(u) for u in pictures),
            )
            if not text:
                break
            t = est_tokens(text)
            b = len(text.encode("utf-8"))

        out.append({**it, "text": text, "images": pictures})
        tok += t + len(pictures) * IMG_TOKEN_COST
        byt += b + sum(wire_bytes(u) for u in pictures)
        imgs += len(pictures)
    return out


def stats(items: list[dict]) -> dict:
    tok = sum(est_tokens(i["text"]) + len(i.get("images") or []) * IMG_TOKEN_COST for i in items)
    byt = sum(len(i["text"].encode()) + sum(wire_bytes(u) for u in (i.get("images") or []))
              for i in items)
    return {"items": len(items), "est_tokens": tok, "bytes": byt,
            "images": sum(len(i.get("images") or []) for i in items)}
