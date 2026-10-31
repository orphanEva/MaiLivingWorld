#!/usr/bin/env python3
"""迁移六套 SQLite 账本到全新空 MySQL。默认只读预览；切换之前停掉插件。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MaiLivingWorld.mysql_compat import TABLES
from MaiLivingWorld.mysql_store import MySQLStore
from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.experience_loop import ExperienceLoop
from MaiLivingWorld.proactive_feedback import ProactiveFeedback


LEDGERS = {
    "living_world.sqlite3": ("stories","attempts","story_attempts"),
    "brain_book.sqlite3": tuple(t for t in TABLES if t.startswith("brain_")),
    "mind.sqlite3": tuple(t for t in TABLES if t.startswith("mind_")),
    "cognition.sqlite3": tuple(t for t in TABLES if t.startswith("cognitive_")),
    "experience_loop.sqlite3": tuple(t for t in TABLES if t.startswith("experience_")),
    "proactive_feedback.sqlite3": tuple(t for t in TABLES if t.startswith("proactive_")),
}
ALL_TABLES = tuple(t for group in LEDGERS.values() for t in group)


def snapshot(directory: Path) -> tuple[dict[str,list[dict]], list[str]]:
    """读取固定名称的只读备份；源损坏直接报错，不执行任何迁移。"""
    result, missing = {}, []
    for file, names in LEDGERS.items():
        source=directory/file
        if not source.is_file():
            missing.append(file)
            for table in names:
                result[table]=[]
            continue
        db=sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True)
        db.row_factory=sqlite3.Row
        try:
            integrity=db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity!="ok":
                raise RuntimeError(f"{file} SQLite 完整性校验失败")
            actual={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in names:
                if name not in actual:
                    result[name]=[]
                    continue
                result[name]=[dict(row) for row in db.execute('SELECT * FROM "'+name+'"')]
        finally:
            db.close()
    return result,missing


def signature(rows: list[dict]) -> str:
    encoded=sorted(json.dumps(row,ensure_ascii=False,sort_keys=True,
                              separators=(",",":"),allow_nan=False)
                   for row in rows)
    return hashlib.sha256("\n".join(encoded).encode("utf-8")).hexdigest()


def ensure_schema(config: dict, workdir: Path) -> None:
    """与插件正式加载相同的建表路径。"""
    top=MySQLStore(**config)
    top.close()
    for name,cls in (("brain_book",BrainBook),("mind",MindStore),
                     ("cognition",CognitiveStore),("experience_loop",ExperienceLoop),
                     ("proactive_feedback",ProactiveFeedback)):
        db=cls(workdir/(name+".sqlite3"),mysql_settings=config)
        db.close()


def apply(source_rows: dict[str,list[dict]], config: dict) -> dict[str,int]:
    import pymysql
    prefix=config["table_prefix"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}",prefix):
        raise ValueError("非法 MySQL 表前缀")
    with tempfile.TemporaryDirectory() as temp:
        ensure_schema(config,Path(temp))
    con=pymysql.connect(
        host=config["host"],port=int(config["port"]),user=config["user"],
        password=config["password"],database=config["database"],
        charset="utf8mb4",autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
    quote=chr(96)
    try:
        # 所有目标表必须为空。DDL 已提前准备，跨表写入统一事务。
        with con.cursor() as cur:
            dest_columns={}
            for table in ALL_TABLES:
                qname=quote+prefix+table+quote
                cur.execute("SHOW COLUMNS FROM "+qname)
                dest_columns[table]=[col["Field"] for col in cur.fetchall()]
                cur.execute("SELECT COUNT(*) AS n FROM "+qname)
                count=int(cur.fetchone()["n"])
                if count:
                    raise ValueError(f"目标表非空，拒绝增量或覆盖：{table} ({count})")
                for src in source_rows[table]:
                    unknown=set(src)-set(dest_columns[table])
                    missing=set(dest_columns[table])-set(src)
                    if unknown or missing:
                        raise ValueError(f"{table} 源库/目标库列不匹配："
                                         f"unexpected={sorted(unknown)}, missing={sorted(missing)}")
        try:
            con.begin()
            with con.cursor() as cur:
                for table in ALL_TABLES:
                    rows=source_rows[table]
                    if not rows:
                        continue
                    fields=dest_columns[table]
                    qname=quote+prefix+table+quote
                    sql=("INSERT INTO "+qname+" ("+
                         ",".join(quote+field+quote for field in fields)+
                         ") VALUES ("+",".join(["%s"]*len(fields))+")")
                    for row in rows:
                        cur.execute(sql,tuple(row[col] for col in fields))
                # 在事务内部逐表校验数量和逐行内容摘要，不是只比较数量。
                for table in ALL_TABLES:
                    cur.execute("SELECT * FROM "+quote+prefix+table+quote)
                    actual=cur.fetchall()
                    expected=source_rows[table]
                    if len(actual)!=len(expected) or signature(actual)!=signature(expected):
                        raise RuntimeError(f"迁移校验失败：{table} 内容摘要不匹配")
            con.commit()
        except BaseException:
            con.rollback()
            raise
        return {table:len(source_rows[table]) for table in ALL_TABLES}
    finally:
        con.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description="一次性迁移 MaiLivingWorld 六个 SQLite 库（29张表）")
    parser.add_argument("--directory",required=True,type=Path,help="只读备份目录")
    parser.add_argument("--apply",action="store_true",help="实际写入空 MySQL")
    parser.add_argument("--confirm",default="",help="必须为 MIGRATE_ALL_EMPTY_TARGET")
    ns=parser.parse_args(argv)
    directory=ns.directory.resolve()
    if not directory.is_dir():
        parser.error("备份目录不存在")
    rows,missing=snapshot(directory)
    print("SQLite 备份目录:",directory)
    print("缺失文件（视为未启用模块）:",",".join(missing) or "无")
    print("记录数:",{table:len(items) for table,items in rows.items()})
    print("总记录:",sum(map(len,rows.values())))
    if not ns.apply:
        print("DRY RUN：未连接 MySQL，也未修改任何源数据库")
        return 0
    if ns.confirm!="MIGRATE_ALL_EMPTY_TARGET":
        parser.error("正式导入需要 --confirm MIGRATE_ALL_EMPTY_TARGET")
    mandatory=["MLW_MYSQL_HOST","MLW_MYSQL_USER","MLW_MYSQL_PASSWORD","MLW_MYSQL_DATABASE"]
    if any(key not in os.environ for key in mandatory):
        parser.error("MySQL 配置必须通过环境变量提供："+",".join(mandatory))
    cfg={"host":os.environ["MLW_MYSQL_HOST"],
         "port":int(os.environ.get("MLW_MYSQL_PORT","3306")),
         "user":os.environ["MLW_MYSQL_USER"],
         "password":os.environ["MLW_MYSQL_PASSWORD"],
         "database":os.environ["MLW_MYSQL_DATABASE"],
         "table_prefix":os.environ.get("MLW_MYSQL_PREFIX","mlw_")}
    counts=apply(rows,cfg)
    print("迁移完成：所有表通过数量和内容 SHA-256 校验，总记录:",sum(counts.values()))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
