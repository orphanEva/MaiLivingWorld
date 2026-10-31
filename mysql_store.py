"""MySQL 持久化：与 SQLite Store 保持同一组接口。

重要：数据库连接故障时抛异常，不静默切回 SQLite，防止重复唤醒。
数据库名称与表前缀由管理员配置；所有内容值都采用参数化查询。
"""
from __future__ import annotations

import re
import time

from .sql_logging import SQLAudit, SQLMySQLCursor
from typing import Any


class MySQLStore:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        table_prefix: str = "mlw_",
        connect_timeout: int = 5,
        *,
        logger=None,
        sql_debug: bool = False,
    ) -> None:
        # 标识符不能使用 SQL 值占位符；白名单校验后才允许拼进 SQL。
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}", table_prefix):
            raise ValueError("MySQL 表前缀必须以英文字母开头，且仅包含字母、数字和下划线（最多21位）")
        if not database or not user or not host:
            raise ValueError("MySQL 模式必须填写 host、user 和 database")
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError("MySQL 模式依赖 PyMySQL，请检查插件依赖是否安装成功") from exc
        self._sql_audit = SQLAudit(logger=logger, enabled=sql_debug, backend="mysql")
        self.tables = {name: f"`{table_prefix}{name}`" for name in ("stories", "attempts", "story_attempts")}
        self.con = pymysql.connect(
            host=host, port=int(port), user=user, password=password,
            database=database, charset="utf8mb4", autocommit=True,
            connect_timeout=int(connect_timeout), read_timeout=10, write_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            self._init_schema()
        except BaseException:
            self.con.close()
            raise

    def _ping(self) -> None:
        # 仅在每项操作开始前重连；事务执行一半失败时绝不自动重试写操作。
        self.con.ping(reconnect=True)

    def _init_schema(self) -> None:
        s, a, sa = (self.tables[x] for x in ("stories", "attempts", "story_attempts"))
        ddl = [
            f"""CREATE TABLE IF NOT EXISTS {s} (
                `key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '资讯唯一标识（由URL摘要生成）',
                `source` VARCHAR(150) NOT NULL COMMENT '信息源',
                `title` VARCHAR(512) NOT NULL COMMENT '资讯标题',
                `url` TEXT NOT NULL COMMENT '资讯链接',
                `summary` TEXT NOT NULL COMMENT '资讯简介（不等于视频全文）',
                `published_at` DOUBLE NULL COMMENT '发布时间，Unix秒',
                `first_seen` DOUBLE NOT NULL COMMENT '首次发现时间，Unix秒',
                `memory_saved` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已写入A_Memorix',
                PRIMARY KEY (`key`),
                KEY `idx_first_seen` (`first_seen`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='大肥鱼发现过的外部资讯'""",
            f"""CREATE TABLE IF NOT EXISTS {a} (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `target` VARCHAR(100) NOT NULL COMMENT '聊天目标，如qq:private:123',
                `created_at` DOUBLE NOT NULL COMMENT '主动唤醒尝试时间，Unix秒',
                `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '关联资讯',
                `kind` VARCHAR(24) NOT NULL COMMENT 'explore或history',
                PRIMARY KEY (`id`),
                KEY `idx_target_time` (`target`, `created_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='主动唤醒尝试及每日限额'""",
            f"""CREATE TABLE IF NOT EXISTS {sa} (
                `target` VARCHAR(100) NOT NULL COMMENT '聊天目标',
                `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '资讯唯一标识',
                `tried_at` DOUBLE NOT NULL COMMENT '首次尝试分享时间',
                PRIMARY KEY (`target`, `story_key`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每个聊天目标对每条资讯的分享去重'""",
        ]
        try:
            with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
                for stmt in ddl:
                    cur.execute(stmt)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def close(self) -> None:
        self.con.close()

    def save_stories(self, stories: list[Any], max_rows: int = 700) -> int:
        self._ping()
        s = self.tables["stories"]
        added = 0
        try:
            self.con.begin()
            with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
                now = time.time()
                for story in stories:
                    added += cur.execute(
                        f"INSERT IGNORE INTO {s} (`key`,source,title,url,summary,published_at,first_seen) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (story.key, story.source, story.title, story.url, story.summary, story.published_at, now),
                    )
                cur.execute(f"SELECT COUNT(*) AS n FROM {s}")
                excess = max(0, int(cur.fetchone()["n"]) - max(1, int(max_rows)))
                if excess:
                    cur.execute(f"DELETE FROM {s} ORDER BY first_seen ASC, `key` ASC LIMIT %s", (excess,))
            self.con.commit()
            return added
        except Exception:
            self.con.rollback()
            raise

    def candidates(self, target: str, now: float, max_age_hours: float, limit: int = 12) -> list[dict[str, Any]]:
        self._ping()
        s, sa = self.tables["stories"], self.tables["story_attempts"]
        cutoff = now - max_age_hours * 3600
        with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
            cur.execute(
                f"SELECT s.* FROM {s} s LEFT JOIN {sa} a ON s.`key`=a.story_key AND a.target=%s "
                "WHERE a.story_key IS NULL AND s.first_seen >= %s "
                "AND (s.published_at IS NULL OR (s.published_at >= %s AND s.published_at <= %s)) "
                "ORDER BY s.first_seen DESC LIMIT %s",
                (target, cutoff, cutoff, now + 21600, limit),
            )
            return list(cur.fetchall())

    def may_contact(self, target: str, now: float, day_start: float, min_gap_seconds: float, max_daily: int) -> tuple[bool, str]:
        self._ping()
        a = self.tables["attempts"]
        with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
            cur.execute(
                f"SELECT MAX(created_at) AS last_at, "
                f"SUM(CASE WHEN created_at >= %s THEN 1 ELSE 0 END) AS day_count FROM {a} WHERE target=%s",
                (day_start, target),
            )
            result = cur.fetchone()
        last = result["last_at"] if result else None
        count = int((result or {}).get("day_count") or 0)
        if count >= max_daily:
            return False, "daily_limit"
        if last is not None and now - float(last) < min_gap_seconds:
            return False, "cooldown"
        return True, "ok"

    def record_attempt(self, target: str, now: float, kind: str, story_key: str | None) -> None:
        self._ping()
        a, sa = self.tables["attempts"], self.tables["story_attempts"]
        try:
            self.con.begin()
            with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
                cur.execute(f"INSERT INTO {a}(target,created_at,story_key,kind) VALUES(%s,%s,%s,%s)",
                            (target, now, story_key, kind))
                if story_key:
                    cur.execute(f"INSERT IGNORE INTO {sa}(target,story_key,tried_at) VALUES(%s,%s,%s)",
                                (target, story_key, now))
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def mark_memory_saved(self, key: str) -> None:
        self._ping()
        try:
            self.con.begin()
            with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
                cur.execute(f"UPDATE {self.tables['stories']} SET memory_saved=1 WHERE `key`=%s", (key,))
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def statistics(self) -> dict[str, int]:
        self._ping()
        with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM {self.tables['stories']}")
            stories = cur.fetchone()["n"]
            cur.execute(f"SELECT COUNT(*) AS n FROM {self.tables['attempts']}")
            attempts = cur.fetchone()["n"]
            cur.execute(f"SELECT COUNT(*) AS n FROM {self.tables['stories']} WHERE memory_saved=1")
            memory = cur.fetchone()["n"]
        return {"stories": int(stories), "wake_attempts": int(attempts), "memory_saved": int(memory)}

    def recent(self, limit: int = 15) -> list[dict[str, Any]]:
        self._ping()
        with SQLMySQLCursor(self.con.cursor(), self._sql_audit) as cur:
            cur.execute(
                f"SELECT `key`,source,title,url,first_seen,memory_saved FROM {self.tables['stories']} "
                "ORDER BY first_seen DESC LIMIT %s", (limit,),
            )
            return list(cur.fetchall())
