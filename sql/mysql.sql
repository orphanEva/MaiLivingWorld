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
