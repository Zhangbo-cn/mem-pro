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
        """返回 (conn, 是否来自池)。第二项用于判定「可能是上游早已关掉的空闲连接」。"""
        try:
            return self._free.get_nowait(), True
        except queue.Empty:
            pass
        if self._slots.acquire(blocking=False):
            # 额度换一条新连接；额度只有在连接归还/关闭时才释放
            return self._new(timeout), False
        # 已达连接上限：等别人归还，绝不能再占一个额度（否则额度被等待者占死后
        # 永远没有连接可归还，整个池卡死——实测 48 并发时表现为请求挂到 60 s 超时）
        return self._free.get(), True

    def release(self, conn, reuse: bool) -> None:
        # 注意：HTTPConnection 本身没有 will_close 属性，判定必须用 response.will_close
        if reuse and conn.sock is not None:
            self._free.put(conn)
        else:
            conn.close()
            self._slots.release()

    def flush_idle(self) -> int:
        """丢弃所有空闲连接并归还额度，返回丢弃条数。

        为什么需要：上游（DashScope）会关掉长时间空闲的 keep-alive 连接，而我们这边
        只有读写时才能发现。实测线上 19 小时无流量后，池里 12 条连接全部已死，一次 Add
        的 5 次重试正好把 5 条死连接各撞一次 → 重试预算耗尽 → 503。因此只要有一条空闲
        连接报「对端已关闭」，就认为整池都不可信，全部丢掉用新连接。
        """
        n = 0
        while True:
            try:
                conn = self._free.get_nowait()
            except queue.Empty:
                return n
            conn.close()
            self._slots.release()
            n += 1


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
    """POST 一段 JSON，返回响应体字节。失败一律抛出（由调用方决定是否重试）。

    唯一的内部重试：如果报错的是**从池里取出的旧连接**且错误是「对端已关闭/重置」，
    则清空池子、换全新连接重试一次，这次重试对调用方透明（不计入 embed 的重试统计）。
    这是长空闲后必现的故障（见 `_Pool.flush_idle`），不重试就只能靠调用方的 5 次预算
    去撞 5 条死连接。
    """
    u = urllib.parse.urlsplit(url)
    path = u.path + (f"?{u.query}" if u.query else "")
    p = _pool_for(url, size)
    conn, reused = p.acquire(timeout)
    while True:
        try:
            data, reuse = _exchange(conn, path, body, headers)
        except _STALE_CONNECTION:
            p.release(conn, reuse=False)
            if not reused:
                raise
            # 空闲期被上游关掉的连接通常不止一条：整池作废，用新连接再来一次
            p.flush_idle()
            conn, reused = p.acquire(timeout)
            continue
        except HTTPStatusError as e:
            p.release(conn, getattr(e, "reuse", False))
            raise
        except BaseException:
            p.release(conn, reuse=False)
            raise
        p.release(conn, reuse)
        return data


# 「对端已经不声不响把连接关了」：浏览器/urllib 里统称 stale connection。
# RemoteDisconnected 是 ConnectionResetError 的子类，这里一并列出以示意。
# 关键一条：**读超时也要算**。池里被上游悄悄丢掉的连接不会发 FIN，写请求能成功、
# 读响应就一直等到超时（线上表现为 `The read operation timed out`，占 Full 期间 2% 的
# Add）。旧逻辑把它当「上游慢」重试，但重试仍从池里拿连接、预算又被这次超时吃光 → 503。
# 现在只要「超时 + 连接来自池」就作废整池、换新连接重试一次（实测新连接 0.2~1s 就好）。
_STALE_CONNECTION = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
                     TimeoutError)


def _exchange(conn, path: str, body: bytes, headers: dict):
    """在一条连接上做一次完整请求，返回 (响应体, 连接是否可复用)。"""
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    # 4xx/5xx 也把响应读完再归还连接——读完才能复用
    reuse = not resp.will_close
    if resp.status >= 400:
        err = HTTPStatusError(resp.status, data)
        err.reuse = reuse
        raise err
    return data, reuse
