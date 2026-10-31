-- MaiLivingWorld v0.2.0 (MySQL 5.7+/8.x) 参考建库及建表语句
-- 注意：插件切换到 MySQL 后会执行 CREATE TABLE IF NOT EXISTS，因此无需重复运行建表部分。
-- 建议为插件单独创建数据库与低权限账户；不要使用 root，也不要把密码提交到 GitHub。

CREATE DATABASE IF NOT EXISTS `mai_living_world`
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `mai_living_world`;

CREATE TABLE IF NOT EXISTS `mlw_stories` (
    `key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '资讯唯一标识',
    `source` VARCHAR(150) NOT NULL COMMENT '信息源',
    `title` VARCHAR(512) NOT NULL COMMENT '资讯标题',
    `url` TEXT NOT NULL COMMENT '资讯链接',
    `summary` TEXT NOT NULL COMMENT '资讯简介，不代表已经完整看过视频',
    `published_at` DOUBLE NULL COMMENT '发布时间（Unix秒）',
    `first_seen` DOUBLE NOT NULL COMMENT '首次发现时间（Unix秒）',
    `memory_saved` TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'A_Memorix已接受记忆写入',
    PRIMARY KEY (`key`),
    KEY `idx_first_seen` (`first_seen`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='外部资讯采集历史';

CREATE TABLE IF NOT EXISTS `mlw_attempts` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `target` VARCHAR(100) NOT NULL COMMENT 'QQ群或私聊标识',
    `created_at` DOUBLE NOT NULL COMMENT '主动唤醒尝试时间（Unix秒）',
    `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '关联的资讯唯一标识',
    `kind` VARCHAR(24) NOT NULL COMMENT 'explore：资讯分享；history：延续旧话题',
    PRIMARY KEY (`id`),
    KEY `idx_target_time` (`target`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='主动唤醒与每日配额记录';

CREATE TABLE IF NOT EXISTS `mlw_story_attempts` (
    `target` VARCHAR(100) NOT NULL COMMENT 'QQ群或私聊标识',
    `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '资讯唯一标识',
    `tried_at` DOUBLE NOT NULL COMMENT '首次尝试分享时间（Unix秒）',
    PRIMARY KEY (`target`, `story_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='按目标去重的资讯分享记录';

-- 可选：创建专用用户；请自行替换强密码与来源地址。
-- CREATE USER 'mai_living_world'@'172.22.%' IDENTIFIED BY '换成独立强密码';
-- GRANT SELECT, INSERT, UPDATE, DELETE, CREATE ON `mai_living_world`.* TO 'mai_living_world'@'172.22.%';
-- 备注：若将来需要数据库迁移和升级，可能需要额外授予 ALTER/INDEX 权限。


-- ================================================================
-- 六库一体化：其余五套独立账本（26 张表）
-- 由 SQLite 正式架构生成；可重复执行 CREATE TABLE IF NOT EXISTS。
-- 所有数据请通过迁移脚本导入，禁止手动 COPY/REPLACE 覆盖历史。
-- 现有业务表沿用 mlw_ 前缀，记忆表采用二进制校对规则防止 ID 折叠。
-- ================================================================

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_runs` (
          scope VARCHAR(191) PRIMARY KEY, history_hash VARCHAR(191) NOT NULL DEFAULT '',
          last_attempt DOUBLE NOT NULL DEFAULT 0, last_success DOUBLE NOT NULL DEFAULT 0,
          cycle_count BIGINT NOT NULL DEFAULT 0,
          quick_hash VARCHAR(191) NOT NULL DEFAULT '', quick_last_attempt DOUBLE NOT NULL DEFAULT 0,
          quick_last_success DOUBLE NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_events` (
          id VARCHAR(191) PRIMARY KEY, scope VARCHAR(191) NOT NULL, actor_id VARCHAR(191) NOT NULL,
          kind VARCHAR(191) NOT NULL, summary TEXT NOT NULL, importance BIGINT NOT NULL,
          happened DOUBLE NOT NULL, created DOUBLE NOT NULL,
  KEY `idx_brain_events_scope_time` (scope, happened DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_event_evidence` (
          event_id VARCHAR(191) NOT NULL, message_digest VARCHAR(191) NOT NULL,
          PRIMARY KEY(event_id,message_digest),
          FOREIGN KEY(event_id) REFERENCES brain_events(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_facts` (
          id VARCHAR(191) PRIMARY KEY, scope VARCHAR(191) NOT NULL, subject_id VARCHAR(191) NOT NULL,
          attribute VARCHAR(191) NOT NULL, value TEXT NOT NULL, confidence DOUBLE NOT NULL,
          status VARCHAR(191) NOT NULL DEFAULT 'pending', seen_count BIGINT NOT NULL DEFAULT 0,
          created DOUBLE NOT NULL, updated DOUBLE NOT NULL, valid_until DOUBLE,
          reviewed_count BIGINT NOT NULL DEFAULT 0,
  KEY `idx_brain_facts_scope_subject` (scope,subject_id,attribute,status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_fact_evidence` (
          fact_id VARCHAR(191) NOT NULL, message_digest VARCHAR(191) NOT NULL,
          PRIMARY KEY(fact_id,message_digest),
          FOREIGN KEY(fact_id) REFERENCES brain_facts(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_goals` (
          id VARCHAR(191) PRIMARY KEY, scope VARCHAR(191) NOT NULL, owner_id VARCHAR(191) NOT NULL,
          title TEXT NOT NULL, criterion TEXT NOT NULL,
          next_action VARCHAR(191) NOT NULL DEFAULT 'wait',
          status VARCHAR(191) NOT NULL DEFAULT 'open', revision BIGINT NOT NULL DEFAULT 1,
          created DOUBLE NOT NULL, updated DOUBLE NOT NULL, closed_at DOUBLE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_goal_events` (
          goal_id VARCHAR(191) NOT NULL, message_digest VARCHAR(191) NOT NULL,
          kind VARCHAR(191) NOT NULL, recorded DOUBLE NOT NULL,
          PRIMARY KEY(goal_id,message_digest,kind),
          FOREIGN KEY(goal_id) REFERENCES brain_goals(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_decisions` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT, scope VARCHAR(191) NOT NULL, goal_id VARCHAR(191),
          feedback_event_id BIGINT UNIQUE, action VARCHAR(191) NOT NULL,
          reason TEXT NOT NULL, outcome VARCHAR(191) NOT NULL DEFAULT 'submitted',
          user_feedback VARCHAR(191) NOT NULL DEFAULT 'unrated',
          recorded DOUBLE NOT NULL, finished DOUBLE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_cycles` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT, scope VARCHAR(191) NOT NULL,
          source_count BIGINT NOT NULL, added_events BIGINT NOT NULL,
          added_facts BIGINT NOT NULL, updated_goals BIGINT NOT NULL,
          reflection TEXT NOT NULL, created DOUBLE NOT NULL,
          cycle_kind VARCHAR(191) NOT NULL DEFAULT 'deep'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_fact_revisions` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT, scope VARCHAR(191) NOT NULL, fact_id VARCHAR(191) NOT NULL,
          before_status VARCHAR(191) NOT NULL, after_status VARCHAR(191) NOT NULL,
          reason TEXT NOT NULL, changed DOUBLE NOT NULL,
  KEY `idx_brain_fact_revisions_scope` (scope,fact_id,id DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_runs` (
            owner_id VARCHAR(191) PRIMARY KEY, last_hash VARCHAR(191) NOT NULL DEFAULT '',
            last_attempt_at DOUBLE NOT NULL DEFAULT 0, last_success_at DOUBLE NOT NULL DEFAULT 0,
            behavior_goal VARCHAR(1024) NOT NULL DEFAULT '', reflection_count BIGINT NOT NULL DEFAULT 0
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_notes` (
            owner_id VARCHAR(191) NOT NULL, note_key VARCHAR(191) NOT NULL, kind VARCHAR(191) NOT NULL,
            text TEXT NOT NULL, basis TEXT NOT NULL, confidence DOUBLE NOT NULL,
            seen_count BIGINT NOT NULL DEFAULT 1, first_seen DOUBLE NOT NULL,
            last_seen DOUBLE NOT NULL, status VARCHAR(191) NOT NULL DEFAULT 'legacy',
            PRIMARY KEY (owner_id,note_key)
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_evidence` (
            owner_id VARCHAR(191) NOT NULL, note_key VARCHAR(191) NOT NULL, evidence_id VARCHAR(191) NOT NULL,
            message_digest VARCHAR(191) NOT NULL, role VARCHAR(191) NOT NULL,
            PRIMARY KEY (owner_id,note_key,evidence_id)
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_reviews` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, owner_id VARCHAR(191) NOT NULL,
            target_key VARCHAR(191) NOT NULL, reason TEXT NOT NULL,
            evidence_ids TEXT NOT NULL, status VARCHAR(191) NOT NULL DEFAULT 'pending',
            created_at DOUBLE NOT NULL, previous_status VARCHAR(191) NOT NULL DEFAULT 'active'
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_snapshots` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT, owner_id VARCHAR(191) NOT NULL,
            created_at DOUBLE NOT NULL, reason TEXT NOT NULL, payload LONGTEXT NOT NULL
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_cycles` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT,
              attempted_at DOUBLE NOT NULL, status VARCHAR(191) NOT NULL,
              input_count BIGINT NOT NULL DEFAULT 0, added BIGINT NOT NULL DEFAULT 0,
              updated BIGINT NOT NULL DEFAULT 0
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_sources` (
              source_key VARCHAR(191) PRIMARY KEY, seen_at DOUBLE NOT NULL
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_goals` (
              goal_id VARCHAR(191) PRIMARY KEY, topic TEXT NOT NULL, question TEXT NOT NULL,
              observation TEXT NOT NULL, uncertainty TEXT NOT NULL,
              next_action VARCHAR(191) NOT NULL, status VARCHAR(191) NOT NULL DEFAULT 'open',
              created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
              revision BIGINT NOT NULL DEFAULT 1
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_evidence` (
              goal_id VARCHAR(191) NOT NULL, source_key VARCHAR(191) NOT NULL, source TEXT NOT NULL,
              title TEXT NOT NULL, url TEXT NOT NULL, observed_at DOUBLE NOT NULL,
              PRIMARY KEY(goal_id,source_key)
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_events` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT,
              goal_id VARCHAR(191) NOT NULL, kind VARCHAR(191) NOT NULL,
              note TEXT NOT NULL, source_key VARCHAR(191),
              created_at DOUBLE NOT NULL
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_actions` (
            event_id BIGINT PRIMARY KEY,
            owner_id VARCHAR(191) NOT NULL,
            session_id VARCHAR(191) NOT NULL,
            kind VARCHAR(191) NOT NULL CHECK(kind IN ('explore','history')),
            topic_digest VARCHAR(191) NOT NULL,
            created_at DOUBLE NOT NULL,
            sent_at DOUBLE,
            outcome VARCHAR(191) NOT NULL DEFAULT 'submitted',
            feedback VARCHAR(191) NOT NULL DEFAULT 'unrated',
            feedback_at DOUBLE,
  KEY `idx_experience_owner_time` (owner_id,session_id,sent_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_evidence` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            event_id BIGINT NOT NULL,
            evidence_digest VARCHAR(191) NOT NULL,
            signal VARCHAR(191) NOT NULL,
            created_at DOUBLE NOT NULL,
            UNIQUE(event_id,evidence_digest)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_policy` (
            owner_id VARCHAR(191) NOT NULL,
            kind VARCHAR(191) NOT NULL,
            avoid_until DOUBLE NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_id,kind)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_opt_out` (
            owner_id VARCHAR(191) PRIMARY KEY,
            paused_at DOUBLE NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- proactive_feedback.py
CREATE TABLE IF NOT EXISTS `mlw_proactive_targets` (
          target VARCHAR(191) PRIMARY KEY,
          stop_streak BIGINT NOT NULL DEFAULT 0,
          backoff_until DOUBLE NOT NULL DEFAULT 0,
          last_sent_at DOUBLE NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- proactive_feedback.py
CREATE TABLE IF NOT EXISTS `mlw_proactive_events` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          target VARCHAR(191) NOT NULL,
          session_id VARCHAR(191) NOT NULL,
          created_at DOUBLE NOT NULL,
          updated_at DOUBLE NOT NULL,
          outcome VARCHAR(191) NOT NULL,
  KEY `idx_proactive_events_time` (target,created_at,outcome)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
