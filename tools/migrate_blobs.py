#!/usr/bin/env python3
"""把 memories.images 里的 Base64 图片搬到 blobs 表（一次性迁移）。

为什么要搬：Search 需要读「某用户全部记忆」的行。图片内联在 memories 行里时，SQLite 即便
不 SELECT images 也得跳过这些 overflow 页——实测 6,614 行 / 463 MB 图片的一次读取要 5.3 s，
平台按 16 并发 Search 压就变成 80 s+，并放大内存导致 OOM（2026-11-01 的 SEARCH_HTTP_499）。

迁移后：
  * 新写入：图片直接进 blobs，memories.images 恒为 '[]'（store.add_memories 已改）；
  * 历史行：本脚本把 images 复制进 blobs，再把 memories.images 置 '[]'（行变短、页回收）。

用法：
  python3 tools/migrate_blobs.py            # 迁移（可重复执行，幂等）
  python3 tools/migrate_blobs.py --dry-run  # 只统计
  python3 tools/migrate_blobs.py --vacuum   # 迁移后 VACUUM 收缩文件（需要约一份 DB 的空闲磁盘）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

import dotenv  # noqa: E402

dotenv.load()
DB = os.environ.get("DB_PATH", os.path.join(ROOT, "service", "mem.db"))
if not os.path.isabs(DB):
    DB = os.path.join(ROOT, DB)

BATCH = 200


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS blobs(
        mem_id  TEXT PRIMARY KEY,
        images  TEXT NOT NULL)""")
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description="memories.images → blobs 表迁移")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    conn = connect()
    todo = conn.execute(
        "SELECT count(*), coalesce(sum(length(images)), 0) FROM memories "
        "WHERE images IS NOT NULL AND images != '[]'").fetchone()
    print(f"待迁移 {todo[0]:,} 行 / {todo[1] / 1e9:.2f} GB（DB={DB}）", flush=True)
    if args.dry_run or not todo[0]:
        return 0

    moved = bytes_moved = 0
    started = time.time()
    while True:
        rows = conn.execute(
            "SELECT rowid, id, images FROM memories "
            "WHERE images IS NOT NULL AND images != '[]' LIMIT ?", (args.batch,)).fetchall()
        if not rows:
            break
        payload = [(mid, raw) for _rid, mid, raw in rows]
        conn.executemany("INSERT OR REPLACE INTO blobs(mem_id, images) VALUES(?,?)", payload)
        conn.executemany("UPDATE memories SET images='[]' WHERE rowid=?",
                         [(r[0],) for r in rows])
        conn.commit()
        moved += len(rows)
        bytes_moved += sum(len(p[1]) for p in payload)
        if moved % (args.batch * 10) < args.batch:
            el = time.time() - started
            print(f"  {moved:,}/{todo[0]:,} 行  {bytes_moved / 1e9:.2f} GB  "
                  f"{el:.0f}s（{moved / max(el, 1e-9):.0f} 行/s）", flush=True)

    left, left_bytes = conn.execute(
        "SELECT count(*), coalesce(sum(length(images)),0) FROM memories "
        "WHERE images IS NOT NULL AND images != '[]'").fetchone()
    print(f"完成：迁移 {moved:,} 行 / {bytes_moved / 1e9:.2f} GB，用时 {time.time() - started:.0f}s；"
          f"剩余内联 {left:,} 行 / {left_bytes / 1e6:.1f} MB", flush=True)
    if args.vacuum:
        free = os.statvfs(os.path.dirname(DB))
        avail = free.f_bavail * free.f_frsize
        print(f"VACUUM（空闲 {avail / 1e9:.1f} GB）…", flush=True)
        conn.execute("VACUUM")
        print(f"VACUUM 完成，文件 {os.path.getsize(DB) / 1e9:.2f} GB", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
