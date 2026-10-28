#!/usr/bin/env python3
"""一次性迁移 MaiLivingWorld SQLite 到全新的空 MySQL 表。

先停止/禁用插件，保证没有新写入。MySQL 配置从环境变量读取，避免密码进入 shell 历史和源码。
若 MySQL 已有记录则直接拒绝迁移，防止覆盖或重复合并；请先备份两个数据库。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MaiLivingWorld.mysql_store import MySQLStore


def migrate(sqlite_file: Path) -> tuple[int, int, int]:
    if not sqlite_file.is_file():
        raise FileNotFoundError(f"SQLite 数据库不存在：{sqlite_file}")
    mysql = MySQLStore(
        host=os.environ["MLW_MYSQL_HOST"],
        port=int(os.environ.get("MLW_MYSQL_PORT", "3306")),
        user=os.environ["MLW_MYSQL_USER"],
        password=os.environ["MLW_MYSQL_PASSWORD"],
        database=os.environ["MLW_MYSQL_DATABASE"],
        table_prefix=os.environ.get("MLW_MYSQL_PREFIX", "mlw_"),
    )
    src = sqlite3.connect(f"file:{sqlite_file}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        if any(mysql.statistics().values()):
            raise RuntimeError("目标 MySQL 中已有数据，为安全起见拒绝迁移；请使用空数据库/表前缀")
        cursor = mysql.con.cursor()
        t = mysql.tables
        try:
            mysql.con.begin()
            stories = list(src.execute("SELECT * FROM stories"))
            attempts = list(src.execute("SELECT * FROM attempts"))
            dedups = list(src.execute("SELECT * FROM story_attempts"))
            for row in stories:
                cursor.execute(
                    f"INSERT INTO {t['stories']} (`key`,source,title,url,summary,published_at,first_seen,memory_saved) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    tuple(row[k] for k in ("key","source","title","url","summary","published_at","first_seen","memory_saved")),
                )
            for row in attempts:
                cursor.execute(
                    f"INSERT INTO {t['attempts']} (id,target,created_at,story_key,kind) VALUES (%s,%s,%s,%s,%s)",
                    tuple(row[k] for k in ("id","target","created_at","story_key","kind")),
                )
            for row in dedups:
                cursor.execute(
                    f"INSERT INTO {t['story_attempts']} (target,story_key,tried_at) VALUES (%s,%s,%s)",
                    tuple(row[k] for k in ("target","story_key","tried_at")),
                )
            mysql.con.commit()
        except BaseException:
            mysql.con.rollback()
            raise
        finally:
            cursor.close()
        return len(stories), len(attempts), len(dedups)
    finally:
        src.close()
        mysql.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="安全迁移 MaiLivingWorld 本地 SQLite 到空 MySQL")
    parser.add_argument("--sqlite", required=True, type=Path, help="原 living_world.sqlite3 的路径")
    args = parser.parse_args()
    stories, attempts, dedups = migrate(args.sqlite.resolve())
    print(f"迁移成功：资讯 {stories}，主动唤醒 {attempts}，分享去重 {dedups}。请核对目标数据库后再切换后端。")


if __name__ == "__main__":
    main()
