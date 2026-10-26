#!/usr/bin/env python3
"""SQLite 在线热备 —— 评测中途库坏了能救回来。

用 sqlite3 的 backup API，不需要停服务：WAL 模式下也能得到一致快照。
Full 只有一次机会，Add 阶段跑完先存一份，出问题就不用从零重来。

用法：
  python3 tools/backup.py                          # 备份 DB_PATH 到 <db>.<时间>.bak
  python3 tools/backup.py --db /data/mem.db --out /data/snap.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite 在线热备")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "data/mem.db"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    dst = args.out or f"{args.db}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    src = sqlite3.connect(args.db, timeout=30)
    out = sqlite3.connect(dst)
    try:
        with out:
            src.backup(out)          # 在线热备：并发写入下仍是一致快照
        n = out.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        src.close()
        out.close()
    print(f"备份完成：{args.db} → {dst}"
          f"（{os.path.getsize(dst) / 1024 / 1024:.1f} MiB，{n} 条记忆）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
