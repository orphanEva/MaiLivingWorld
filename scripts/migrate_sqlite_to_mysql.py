#!/usr/bin/env python3
"""将 MaiLivingWorld 外部资讯/唤醒历史从 SQLite 一次性迁移至 MySQL。

默认只读预览；需停止旧插件、备份 SQLite 并显式 --apply --confirm。
其他独立 brain/mind/cognition/experience/proactive 库不在本次范围内。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import types
from pathlib import Path


def _store_class():
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("MaiLivingWorld")
    package.__path__ = [str(root)]
    package.__package__ = "MaiLivingWorld"
    package.__file__ = str(root / "__init__.py")
    sys.modules.setdefault("MaiLivingWorld", package)
    from MaiLivingWorld.mysql_store import MySQLStore
    return MySQLStore


TABLES = {
    "stories": ("key", "source", "title", "url", "summary", "published_at", "first_seen", "memory_saved"),
    "attempts": ("id", "target", "created_at", "story_key", "kind"),
    "story_attempts": ("target", "story_key", "tried_at"),
}


def source_snapshot(path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"源数据库不存在: {path}")
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = {}
        for table, fields in TABLES.items():
            columns = ", ".join('"' + col + '"' for col in fields)
            rows[table] = [tuple(r) for r in con.execute(f'SELECT {columns} FROM "{table}"')]
        return rows
    finally:
        con.close()


def migrate(rows, db) -> dict[str, int]:
    # MySQLStore 已初始化 DDL。先验证所有目标业务表为空，拒绝追加至活跃数据库。
    db._ping()
    counts={}
    with db.con.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f"SELECT COUNT(*) AS n FROM {db.tables[table]}")
            counts[table]=int(cursor.fetchone()["n"])
    if any(counts.values()):
        raise ValueError(f"目标表非空，拒绝合并导入: {counts}")

    try:
        db.con.begin()
        with db.con.cursor() as cursor:
            for table, fields in TABLES.items():
                columns=", ".join(chr(96)+c+chr(96) for c in fields)
                placeholders=", ".join(["%s"]*len(fields))
                sql=f"INSERT INTO {db.tables[table]} ({columns}) VALUES ({placeholders})"
                for row in rows[table]:
                    cursor.execute(sql, row)
            for table, source_rows in rows.items():
                cursor.execute(f"SELECT COUNT(*) AS n FROM {db.tables[table]}")
                if int(cursor.fetchone()["n"]) != len(source_rows):
                    raise RuntimeError(f"{table} 迁移后记录数与源库不一致")
            cursor.execute(f"SELECT COALESCE(SUM(memory_saved),0) AS n FROM {db.tables['stories']}")
            actual=int(cursor.fetchone()["n"])
            saved=sum(int(row[-1]) for row in rows["stories"])
            if actual != saved:
                raise RuntimeError("memory_saved 汇总校验失败")
        db.con.commit()
        return {table:len(source_rows) for table,source_rows in rows.items()}
    except BaseException:
        db.con.rollback()
        raise


def main(argv=None):
    p=argparse.ArgumentParser(description="迁移外部资讯、主动尝试与分享去重（不含五套独立账本）")
    p.add_argument("--sqlite",type=Path,required=True,help="living_world.sqlite3 路径")
    p.add_argument("--host",default=os.getenv("MLW_MYSQL_HOST","mysql"))
    p.add_argument("--port",type=int,default=int(os.getenv("MLW_MYSQL_PORT","3306")))
    p.add_argument("--user",default=os.getenv("MLW_MYSQL_USER","mai_living_world"))
    p.add_argument("--database",default=os.getenv("MLW_MYSQL_DATABASE","mai_living_world"))
    p.add_argument("--prefix",default=os.getenv("MLW_MYSQL_PREFIX","mlw_"))
    p.add_argument("--apply",action="store_true",help="真正写入（默认仅预览）")
    p.add_argument("--confirm",default="",help="必须精确填写 MIGRATE_EMPTY_TARGET")
    args=p.parse_args(argv)
    rows=source_snapshot(args.sqlite)
    stats={k:len(v) for k,v in rows.items()}
    print("源库记录数:",stats,"已保存长期记忆:",sum(int(v[-1]) for v in rows["stories"]))
    if not args.apply:
        print("DRY RUN: 未连接 MySQL，也未写入任何数据")
        return 0
    if args.confirm!="MIGRATE_EMPTY_TARGET":
        p.error("正式执行还需要 --confirm MIGRATE_EMPTY_TARGET")
    password=os.environ.get("MLW_MYSQL_PASSWORD")
    if password is None:
        p.error("请通过环境变量 MLW_MYSQL_PASSWORD 传入密码（不要把密码写进命令参数）")
    db=_store_class()(host=args.host,port=args.port,user=args.user,
                      password=password,database=args.database,table_prefix=args.prefix)
    try:
        print("迁移成功:",migrate(rows,db))
        return 0
    finally:
        db.close()


if __name__=="__main__":
    raise SystemExit(main())
