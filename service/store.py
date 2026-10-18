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

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "mem.db"))
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


def count(user_id: str | None = None) -> int:
    with _LOCK, _conn() as c:
        if user_id is None:
            return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM memories WHERE user_id=?", (user_id,)).fetchone()[0]


# ---------- 读取（严格 user_id 隔离）----------
def fetch_user(user_id: str) -> list[dict]:
    with _LOCK, _conn() as c:
        rows = c.execute(
            """SELECT id,text,images,ts,created_at,seq,vec FROM memories
               WHERE user_id=? ORDER BY seq ASC, id ASC""",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "text": r[1],
            "images": json.loads(r[2]),
            "ts": r[3],
            "created_at": r[4],
            "seq": r[5],
            "vec": r[6],
        }
        for r in rows
    ]


def new_id() -> str:
    return f"mem_{uuid.uuid4().hex[:20]}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
