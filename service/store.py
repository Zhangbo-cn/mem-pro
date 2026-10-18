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

import dotenv

dotenv.load()

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "mem.db"))
if not os.path.isabs(DB_PATH):  # 相对路径统一相对仓库根，避免 cwd 不同写错位置
    DB_PATH = str(dotenv.ROOT / DB_PATH)
_LOCK = threading.Lock()

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
def add_memories(items: list[dict]) -> None:
    """items: {id,user_id,session_id,request_id,seq,text,images(vec? list[str]),ts,vec(bytes)}"""
    if not items:
        return
    with _LOCK, _conn() as c:
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
        c.executemany(
            """INSERT OR IGNORE INTO memories
               (id,user_id,session_id,request_id,seq,text,images,ts,created_at,vec)
               VALUES(:id,:user_id,:session_id,:request_id,:seq,:text,:images,:ts,:created_at,:vec)""",
            [
                {
                    **it,
                    "images": json.dumps(it.get("images") or [], ensure_ascii=False),
                    "created_at": it.get("created_at") or _now(),
                }
                for it in items
            ],
        )


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
def fetch_user(user_id: str, with_images: bool = False) -> list[dict]:
    """取某 user 的全部分块。默认不读图片列：大 Base64 只在最终证据上取。"""
    cols = "id,text,images,ts,created_at,seq,vec" if with_images else "id,text,ts,created_at,seq,vec"
    with _LOCK, _conn() as c:
        rows = c.execute(
            f"""SELECT {cols} FROM memories
                WHERE user_id=? ORDER BY seq ASC, id ASC""",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        if with_images:
            out.append({"id": r[0], "text": r[1], "images": json.loads(r[2]), "ts": r[3],
                        "created_at": r[4], "seq": r[5], "vec": r[6]})
        else:
            out.append({"id": r[0], "text": r[1], "images": [], "ts": r[2],
                        "created_at": r[3], "seq": r[4], "vec": r[5]})
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
