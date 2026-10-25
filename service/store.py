"""SQLite 存储层：记忆、向量、幂等表。

关键点（对应官方契约）：
- 幂等：同一 (user_id, request_id) 只写一次，重复请求回放原响应
- user_id 是唯一隔离边界，所有检索强制 WHERE user_id = ?
- session_id 只用于组织，不作为 Search 过滤条件
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict

import dotenv

dotenv.load()

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "mem.db"))
if not os.path.isabs(DB_PATH):  # 相对路径统一相对仓库根，避免 cwd 不同写错位置
    DB_PATH = str(dotenv.ROOT / DB_PATH)
_LOCK = threading.Lock()
_USER_CACHE_MAX_BYTES = int(os.environ.get("USER_CACHE_MAX_BYTES", str(64 * 1024 * 1024)))
_USER_CACHE: OrderedDict[tuple[str, str], tuple[list[dict], int]] = OrderedDict()
_USER_CACHE_BYTES = 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  session_id  TEXT,
  request_id  TEXT,
  seq         INTEGER,
  text        TEXT NOT NULL,
  images      TEXT NOT NULL DEFAULT '[]',
  ts          INTEGER,
  created_at  TEXT NOT NULL,
  vec         BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id, seq);

CREATE TABLE IF NOT EXISTS idem(
  user_id    TEXT NOT NULL,
  request_id TEXT NOT NULL,
  response   TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, request_id)
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(SCHEMA)


# ---------- 幂等 ----------
def get_idempotent(user_id: str, request_id: str) -> dict | None:
    with _LOCK, _conn() as c:
        row = c.execute(
            "SELECT response FROM idem WHERE user_id=? AND request_id=?", (user_id, request_id)
        ).fetchone()
    return json.loads(row[0]) if row else None


def put_idempotent(user_id: str, request_id: str, response: dict) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO idem(user_id, request_id, response, created_at) VALUES(?,?,?,?)",
            (user_id, request_id, json.dumps(response, ensure_ascii=False), _now()),
        )


# ---------- 写入 ----------
def _row_size(row: dict) -> int:
    """Rough per-row cache cost: dict + str overhead, text bytes, plus the vector blob."""
    return 320 + len(row["text"].encode("utf-8")) + len(row["vec"])


def _cache_rows(out: list[dict]) -> list[dict]:
    """Store rows in Search shape (no images) so callers can be served from cache."""
    return [{"id": r["id"], "text": r["text"], "images": [], "ts": r["ts"],
             "created_at": r["created_at"], "seq": r["seq"], "vec": r["vec"]} for r in out]


def add_memories(items: list[dict]) -> None:
    """items: {id,user_id,session_id,request_id,seq,text,images(vec? list[str]),ts,vec(bytes)}"""
    global _USER_CACHE_BYTES
    if not items:
        return
    users = {it["user_id"] for it in items}
    with _LOCK:
        with _conn() as c:
            # 每次 Add 内分配连续、全局递增的 seq：后写入的记忆一定排在后面，
            # 不受单次请求内 messages 下标影响（旧实现会在多次 Add 间错序）。
            row = c.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM memories WHERE user_id=?",
                (items[0]["user_id"],),
            ).fetchone()
            next_seq = int(row[0]) + 1
            for it in items:
                if it.get("seq") is None:
                    it["seq"] = next_seq
                    next_seq += 1
                if not it.get("created_at"):
                    it["created_at"] = _now()  # cache rows must match what the DB stores
            ids = [it["id"] for it in items]
            existing = set()
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                marks = ",".join("?" * len(chunk))
                existing.update(
                    r[0] for r in c.execute(
                        f"SELECT id FROM memories WHERE id IN ({marks})", chunk).fetchall()
                )
            c.executemany(
                """INSERT OR IGNORE INTO memories
                   (id,user_id,session_id,request_id,seq,text,images,ts,created_at,vec)
                   VALUES(:id,:user_id,:session_id,:request_id,:seq,:text,:images,:ts,:created_at,:vec)""",
                [
                    {
                        **it,
                        "images": json.dumps(it.get("images") or [], ensure_ascii=False),
                    }
                    for it in items
                ],
            )
        inserted = [it for it in items if it["id"] not in existing]
        for user_id in users:
            _extend_user_cache(user_id, [it for it in inserted if it["user_id"] == user_id])


def _extend_user_cache(user_id: str, items: list[dict]) -> None:
    """Append freshly written rows to the cached user memory list.

    Add is append-only, so the cached list stays a valid prefix; dropping it would
    force the next Search to re-read every row (tens of ms on large users).
    """
    global _USER_CACHE_BYTES
    key = (DB_PATH, user_id)
    cached = _USER_CACHE.pop(key, None)
    if cached is None:
        return
    rows, size = cached
    _USER_CACHE_BYTES -= size
    if not items:
        _USER_CACHE[key] = (rows, size)  # idempotent replay: nothing new to append
        _USER_CACHE_BYTES += size
        return
    appended = _cache_rows(items)
    total = size + sum(_row_size(r) for r in appended)
    if total > _USER_CACHE_MAX_BYTES:
        return  # over budget: let the next Search reload and re-cache
    rows.extend(appended)
    _USER_CACHE[key] = (rows, total)
    _USER_CACHE_BYTES += total


def ping() -> bool:
    """轻量健康检查：能读 schema 即视为可用。"""
    try:
        with _LOCK, _conn() as c:
            c.execute("SELECT 1 FROM memories LIMIT 1").fetchall()
        return True
    except Exception:
        return False


def count(user_id: str | None = None) -> int:
    with _LOCK, _conn() as c:
        if user_id is None:
            return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM memories WHERE user_id=?", (user_id,)).fetchone()[0]


# ---------- 读取（严格 user_id 隔离）----------
def fetch_user(user_id: str, with_images: bool = False,
               stats: dict | None = None) -> list[dict]:
    """取某 user 的全部分块；Search 路径复用有界、追加式维护的用户缓存。"""
    global _USER_CACHE_BYTES
    key = (DB_PATH, user_id)
    cached = None
    if not with_images:
        with _LOCK:
            cached = _USER_CACHE.get(key)
            if cached is not None:
                _USER_CACHE.move_to_end(key)
    if cached is not None:
        # Copy outside the store lock: serialising thousands of dict copies behind a
        # global mutex turns concurrent Search into a queue.
        if stats is not None:
            stats["cache_hit"] = True
        return [row.copy() for row in cached[0]]

    with _LOCK:
        cols = "id,text,images,ts,created_at,seq,vec" if with_images else "id,text,ts,created_at,seq,vec"
        with _conn() as c:
            rows = c.execute(
                f"""SELECT {cols} FROM memories
                    WHERE user_id=? ORDER BY seq ASC, id ASC""",
                (user_id,),
            ).fetchall()
        if with_images:
            out = [{"id": r[0], "text": r[1], "images": json.loads(r[2]), "ts": r[3],
                    "created_at": r[4], "seq": r[5], "vec": r[6]} for r in rows]
        else:
            out = [{"id": r[0], "text": r[1], "images": [], "ts": r[2],
                    "created_at": r[3], "seq": r[4], "vec": r[5]} for r in rows]
        if stats is not None:
            stats["cache_hit"] = False
        if not with_images and out and _USER_CACHE_MAX_BYTES > 0:
            size = sum(_row_size(d) for d in out)
            if size <= _USER_CACHE_MAX_BYTES:
                old = _USER_CACHE.pop(key, None)
                if old:
                    _USER_CACHE_BYTES -= old[1]
                while _USER_CACHE and _USER_CACHE_BYTES + size > _USER_CACHE_MAX_BYTES:
                    _, (_, evicted_size) = _USER_CACHE.popitem(last=False)
                    _USER_CACHE_BYTES -= evicted_size
                _USER_CACHE[key] = (out, size)
                _USER_CACHE_BYTES += size
        return out


def fetch_images(ids: list[str], user_id: str) -> dict[str, list[str]]:
    """只为最终候选取图片，避免每次 Search 反序列化全部 Base64。"""
    out: dict[str, list[str]] = {}
    if not ids:
        return out
    with _LOCK, _conn() as c:
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            marks = ",".join("?" * len(chunk))
            rows = c.execute(
                f"SELECT id,images FROM memories WHERE user_id=? AND id IN ({marks})",
                (user_id, *chunk),
            ).fetchall()
            for mid, raw in rows:
                out[mid] = json.loads(raw)
    return out


def new_id() -> str:
    return f"mem_{uuid.uuid4().hex[:20]}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
