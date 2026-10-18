"""返回侧预算：官方两条硬墙。

1) Answer 共享 128,000 token 窗口，扣掉 8,192 输出 + 2,048 安全 = 117,760 输入；
   超出时平台「按返回顺序保留前缀」。⇒ 顺序即存活性，尾部是纯浪费。
2) Search 响应 ≤ 30 MiB，单图 ≤ 10 MiB，图片必须内联 Base64。

因此返回前必须做三重裁剪：条数 / token / 字节。
"""
from __future__ import annotations

import re

ANSWER_INPUT_TOKENS = 117_760
TOKEN_BUDGET = int(ANSWER_INPUT_TOKENS * 0.93)      # 留 ~7k 给问题/选项/指令
BYTE_BUDGET = 28 * 1024 * 1024                      # 30 MiB 留 2 MiB 余量
IMG_TOKEN_COST = 2_500                              # 2MP 图片约 2.5k visual tokens
MAX_IMAGES = 6                                      # 单次响应的图片配额
MAX_IMG_BYTES = 10 * 1024 * 1024                    # 单图 10 MiB（decoded）

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")


def est_tokens(text: str) -> int:
    """粗估 token：CJK 1 字≈1 token，ASCII 4 字符≈1 token。"""
    cjk = len(_CJK.findall(text))
    ascii_chars = len(text) - cjk
    return cjk + max(1, ascii_chars // 4)


def data_uri_bytes(uri: str) -> int:
    """data:image/...;base64,XXXX → 解码后字节数估计。"""
    b64 = uri.split(",", 1)[-1]
    return len(b64) * 3 // 4


def clip(items: list[dict], max_items: int) -> list[dict]:
    """items: [{text, images[], ...}] 已按优先级排序；返回预算内前缀。"""
    out: list[dict] = []
    tok = 0
    byt = 0
    imgs = 0
    for it in items:
        if len(out) >= max_items:
            break
        t = est_tokens(it["text"])
        b = len(it["text"].encode("utf-8"))
        n_img = len(it.get("images") or [])
        img_b = sum(data_uri_bytes(u) for u in (it.get("images") or []))
        if imgs + n_img > MAX_IMAGES:
            n_img, img_b = 0, 0          # 图片超配额就只返回文本，不丢整条
        if out and (tok + t + n_img * IMG_TOKEN_COST > TOKEN_BUDGET
                    or byt + b + img_b > BYTE_BUDGET):
            break
        out.append({**it, "images": (it.get("images") or [])[:n_img]})
        tok += t + n_img * IMG_TOKEN_COST
        byt += b + img_b
        imgs += n_img
    return out


def stats(items: list[dict]) -> dict:
    tok = sum(est_tokens(i["text"]) + len(i.get("images") or []) * IMG_TOKEN_COST for i in items)
    byt = sum(len(i["text"].encode()) + sum(data_uri_bytes(u) for u in (i.get("images") or []))
              for i in items)
    return {"items": len(items), "est_tokens": tok, "bytes": byt,
            "images": sum(len(i.get("images") or []) for i in items)}
