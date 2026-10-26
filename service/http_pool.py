"""极简 keep-alive 连接池：只为本项目的两个上游（embedding / rerank）服务。

为什么不用 `urllib.request.urlopen`：它每次调用都新建 TCP + TLS。实测从本机到
DashScope 单条 embedding 的耗时在 0.3–1.9 s 之间抖动，其中握手占 0.5–1.0 s；
放到 24 并发就是「握手风暴」，p95 从 1.75 s 涨到 4 s。这里按
`(scheme, host, port)` 持有最多 `size` 条长连接：

- 只在**成功读完响应**后归还；读到一半出错就丢弃（半截连接不能复用）；
- 池空就新建，但总数受 `size` 限制（避免把上游打成 SYN 洪水）；
- 任何异常都向调用方抛出，重试策略留给调用方（embedding 与 rerank 的重试规则不同）。

只用标准库，无第三方依赖。
"""
from __future__ import annotations

import http.client
import os
import queue
import threading
import urllib.parse


class HTTPStatusError(Exception):
    """4xx/5xx：响应读完了但状态码非法。status/body 供调用方判断是否值得重试。"""

    def __init__(self, status: int, body: bytes):
        super().__init__(f"HTTP {status}: {body[:300]!r}")
        self.status = status
        self.body = body


class _Pool:
    def __init__(self, scheme: str, host: str, port: int, size: int):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.size = max(1, size)
        self._free: queue.LifoQueue = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(self.size)
        self._lock = threading.Lock()

    def _new(self, timeout: float):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def acquire(self, timeout: float):
        try:
            return self._free.get_nowait()
        except queue.Empty:
            pass
        if self._slots.acquire(blocking=False):
            # 额度换一条新连接；额度只有在连接归还/关闭时才释放
            return self._new(timeout)
        # 已达连接上限：等别人归还，绝不能再占一个额度（否则额度被等待者占死后
        # 永远没有连接可归还，整个池卡死——实测 48 并发时表现为请求挂到 60 s 超时）
        return self._free.get()

    def release(self, conn, reuse: bool) -> None:
        # 注意：HTTPConnection 本身没有 will_close 属性，判定必须用 response.will_close
        if reuse and conn.sock is not None:
            self._free.put(conn)
        else:
            conn.close()
            self._slots.release()


_POOLS: dict[tuple, _Pool] = {}
_POOLS_LOCK = threading.Lock()


def pool_size() -> int:
    return max(1, int(os.environ.get("HTTP_POOL_SIZE", "32")))


def _pool_for(url: str, size: int | None = None) -> _Pool:
    u = urllib.parse.urlsplit(url)
    key = (u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80))
    with _POOLS_LOCK:
        p = _POOLS.get(key)
        if p is None:
            p = _Pool(key[0], key[1], key[2], size or pool_size())
            _POOLS[key] = p
        return p


def post(url: str, body: bytes, headers: dict, timeout: float,
         size: int | None = None) -> bytes:
    """POST 一段 JSON，返回响应体字节。失败一律抛出（由调用方决定是否重试）。"""
    u = urllib.parse.urlsplit(url)
    path = u.path + (f"?{u.query}" if u.query else "")
    p = _pool_for(url, size)
    conn = p.acquire(timeout)
    reuse = False
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        # 4xx/5xx 也把响应读完再归还连接——读完才能复用
        reuse = not resp.will_close
        if resp.status >= 400:
            raise HTTPStatusError(resp.status, data)
        return data
    finally:
        p.release(conn, reuse)
