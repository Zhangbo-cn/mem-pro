"""返回给平台的图片降采样。

只改 Search 响应里的 Base64 字节，不动 SQLite 里的原图 —— 原图仍按官方口径
（单图 ≤10 MiB）原样入库，回放/审计随时能取回。

为什么要这一步：Mem-Gallery 的图片原始大小中位数不大，但长尾很凶（实测某主题
单张 1.9 MiB → Base64 后 2.58 MiB）。公网带宽只有 2 Mbit 时，一条 Search 响应
p50 因此到 4.8 MiB ≈ 19 秒，全量 1,711 条要十几小时，还可能撞平台超时。

`IMG_MAX_SIDE=0`（默认）时完全关闭，行为与历史版本逐字节一致。打开后按最长边
等比缩到该像素、存 JPEG（有 alpha 的图先合成到白底），单图通常 60–150 KB。
Pillow 缺失时自动退化为不缩放，不阻断服务。
"""

from __future__ import annotations

import base64
import io
import os

MAX_SIDE = int(os.environ.get("IMG_MAX_SIDE", "0") or 0)
QUALITY = int(os.environ.get("IMG_QUALITY", "82") or 82)

try:  # 可选依赖：没装就退化为直传原图
    from PIL import Image
except Exception:  # pragma: no cover - 环境相关
    Image = None  # type: ignore


def enabled() -> bool:
    return MAX_SIDE > 0 and Image is not None


def _split_uri(uri: str):
    head, _, b64 = uri.partition(",")
    return head, b64


def shrink_uri(uri: str) -> str:
    """把 data:image/...;base64,<b64> 缩成更小的 JPEG data URI。

    任何一步失败都原样返回 —— 缩图是优化，不是正确性要求。
    """
    if not enabled() or not uri.startswith("data:image/") or ";base64," not in uri:
        return uri
    try:
        head, b64 = _split_uri(uri)
        raw = base64.b64decode(b64, validate=False)
        with Image.open(io.BytesIO(raw)) as im:  # type: ignore
            im.load()
            if max(im.size) > MAX_SIDE:
                im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)  # type: ignore
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))  # type: ignore
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=QUALITY, optimize=True,
                    progressive=True)
        out = buf.getvalue()
        if len(out) >= len(raw):  # 已经很小，别再变大
            return uri
        return "data:image/jpeg;base64," + base64.b64encode(out).decode("ascii")
    except Exception:
        return uri


def info() -> dict:
    return {"max_side": MAX_SIDE, "quality": QUALITY,
            "on": enabled(), "pillow": Image is not None}
