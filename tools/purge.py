#!/usr/bin/env python3
"""按用户清理评测数据 —— 赛事规则要求「任务完成后 30 天内删除评测数据及其派生副本」。

规则原文（research/06 §八）：评测数据只能用于完成当前任务，不得用于模型训练、
微调、产品分析、数据集重建或对外传播；完成后 30 天内删除。

本工具删三处派生数据：
  1. memories / idem —— 原始消息与幂等响应（含内联图片与向量）
  2. captions       —— 图片 caption 缓存（key 含图片 sha256，但不绑用户，
                       所以只能整体清或按「本次任务用到的图片」清；默认不动，用 --captions 显式开）
  3. WAL 文件       —— 删除后 checkpoint + VACUUM，避免已删数据残留在 .db-wal

用法：
  python3 tools/purge.py --db /data/mem.db --user-prefix "aml:" --dry-run
  python3 tools/purge.py --db /data/mem.db --user-prefix "aml:" --captions
  python3 tools/purge.py --db /data/mem.db --all          # 清空全部记忆（保留表结构）

注意：本工具不碰 /data 之外的任何文件；dry-run 只统计不写。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="按用户前缀清理评测数据（合规用）")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "data/mem.db"))
    ap.add_argument("--user-prefix", default="", help="只删 user_id 以该前缀开头的行")
    ap.add_argument("--user-id", default="", help="只删这一个 user_id")
    ap.add_argument("--all", action="store_true", help="删掉全部记忆（表结构保留）")
    ap.add_argument("--captions", action="store_true", help="同时清空 caption 缓存")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    if not (args.all or args.user_prefix or args.user_id):
        print("必须给 --user-prefix / --user-id / --all 之一（防止误删整库）", file=sys.stderr)
        return 2

    c = sqlite3.connect(args.db, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    where, params = [], []
    if not args.all:
        # /v2/* 的记忆存在 "\\x1fv2\\x1f<user_id>" 下（见 server.V2_NS），按前缀/ID 删时一并删
        if args.user_prefix:
            pat = args.user_prefix.replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(user_id LIKE ? ESCAPE '\\' OR user_id LIKE ? ESCAPE '\\')")
            params += [pat, "\x1fv2\x1f" + pat]
        if args.user_id:
            where.append("user_id IN (?, ?)")
            params += [args.user_id, "\x1fv2\x1f" + args.user_id]
        clause = " AND ".join(where)
        eq = " WHERE " + clause
    else:
        clause, params, eq = "1", [], ""

    mem = c.execute(f"SELECT COUNT(*) FROM memories{(' WHERE ' + clause) if clause else ''}",
                    params).fetchone()[0]
    idem = c.execute(f"SELECT COUNT(*) FROM idem{(' WHERE ' + clause) if clause else ''}",
                     params).fetchone()[0]
    caps = c.execute("SELECT COUNT(*) FROM captions").fetchone()[0] if args.captions else 0
    users = c.execute(f"SELECT COUNT(DISTINCT user_id) FROM memories{(' WHERE ' + clause) if clause else ''}",
                      params).fetchone()[0]
    print(f"将删除：memories {mem} 行 / idem {idem} 行 / 涉及 {users} 个 user_id"
          + (f" / captions {caps} 行" if args.captions else ""))
    if args.dry_run:
        print("dry-run：未写入")
        return 0
    if not mem and not idem and not caps:
        print("没有匹配的数据")
        return 0

    with c:
        c.execute(f"DELETE FROM memories{eq}", params)
        c.execute(f"DELETE FROM idem{eq}", params)
        if args.captions:
            c.execute("DELETE FROM captions")
    # 已删页仍可能留在 -wal 里：先 checkpoint 再 VACUUM，最后截断 WAL
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("VACUUM")
    c.close()
    left = sqlite3.connect(args.db).execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    print(f"完成。剩余 memories {left} 行；WAL 已 checkpoint，库已 VACUUM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
