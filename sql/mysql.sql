-- MaiLivingWorld v0.10.0 (MySQL 8.4 已验证) 六套数据共 29 表
-- 全部29张表及字段附中文COMMENT；新库直接执行本文件。
-- 已建表不会因 IF NOT EXISTS 更新注释，请使用 sql/mysql_comments_existing.sql。
-- 建议为插件单独创建数据库与低权限账户；不要使用 root，也不要把密码提交到 GitHub。

CREATE DATABASE IF NOT EXISTS `mai_living_world`
    DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `mai_living_world`;

CREATE TABLE IF NOT EXISTS `mlw_stories` (
    `key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '资讯内容的唯一去重键',
    `source` VARCHAR(150) NOT NULL COMMENT '资讯来源名称，例如 B站热门',
    `title` VARCHAR(512) NOT NULL COMMENT '资讯标题',
    `url` TEXT NOT NULL COMMENT '资讯原始链接',
    `summary` TEXT NOT NULL COMMENT '公开标题或简介摘要，不代表完整内容',
    `published_at` DOUBLE NULL COMMENT '资讯原始发布时间，Unix秒；可能为空',
    `first_seen` DOUBLE NOT NULL COMMENT '插件首次采集此资讯的时间，Unix秒',
    `memory_saved` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已成功提交到外部长期记忆，1=已保存，0=未保存',
    PRIMARY KEY (`key`),
    KEY `idx_first_seen` (`first_seen`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='外部资讯采集记录；仅代表抓取到公开标题与简介，不等于已完整阅读或观看';

CREATE TABLE IF NOT EXISTS `mlw_attempts` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主动唤醒尝试自增ID',
    `target` VARCHAR(100) NOT NULL COMMENT '联系目标，包含私聊或群聊作用域',
    `created_at` DOUBLE NOT NULL COMMENT '提交主动唤醒尝试的时间，Unix秒',
    `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '本次尝试关联的资讯键；可为空',
    `kind` VARCHAR(24) NOT NULL COMMENT '唤醒类型：explore=分享资讯，history=延续话题',
    PRIMARY KEY (`id`),
    KEY `idx_target_time` (`target`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='按会话统计的主动唤醒尝试，用于频率限制；不代表消息发送成功';

CREATE TABLE IF NOT EXISTS `mlw_story_attempts` (
    `target` VARCHAR(100) NOT NULL COMMENT '私聊或群聊目标作用域',
    `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '已尝试分享的资讯唯一键',
    `tried_at` DOUBLE NOT NULL COMMENT '首次对该目标尝试分享此资讯的时间，Unix秒',
    PRIMARY KEY (`target`, `story_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='记录某条资讯向某个聊天目标的分享尝试，避免重复分享';

-- 可选：创建专用用户；请自行替换强密码与来源地址。
-- CREATE USER 'mai_living_world'@'172.22.%' IDENTIFIED BY '换成独立强密码';
-- GRANT SELECT, INSERT, UPDATE, DELETE, CREATE ON `mai_living_world`.* TO 'mai_living_world'@'172.22.%';
-- 备注：若将来需要数据库迁移和升级，可能需要额外授予 ALTER/INDEX 权限。


-- ================================================================
-- 六库一体化：其余五套独立账本（26 张表）；表与字段均附中文注释
-- 由 SQLite 正式架构生成；可重复执行 CREATE TABLE IF NOT EXISTS。
-- 所有数据请通过迁移脚本导入，禁止手动 COPY/REPLACE 覆盖历史。
-- 现有业务表沿用 mlw_ 前缀，记忆表采用二进制校对规则防止 ID 折叠。
-- ================================================================

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_runs` (
          scope VARCHAR(191) PRIMARY KEY COMMENT '记忆作用域，例如 private:用户ID 或 group:群ID', history_hash VARCHAR(191) NOT NULL DEFAULT '' COMMENT '最近一次成功深度整理的聊天历史摘要哈希',
          last_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次深度整理尝试时间，Unix秒', last_success DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次深度整理成功时间，Unix秒',
          cycle_count BIGINT NOT NULL DEFAULT 0 COMMENT '此作用域累计成功深度整理次数',
          quick_hash VARCHAR(191) NOT NULL DEFAULT '' COMMENT '最近一次成功快速整理的历史摘要哈希', quick_last_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次快速整理尝试时间，Unix秒',
          quick_last_success DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次快速整理成功时间，Unix秒') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='BrainBook 每个聊天作用域的深度及快速整理执行状态与去重哈希';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_events` (
          id VARCHAR(191) PRIMARY KEY COMMENT '情景事件唯一ID', scope VARCHAR(191) NOT NULL COMMENT '事件所属的私聊或群聊记忆作用域', actor_id VARCHAR(191) NOT NULL COMMENT '事件主体或陈述者的用户ID',
          kind VARCHAR(191) NOT NULL COMMENT '事件类别：experience经历、achievement成就、difficulty困难、preference偏好、correction纠正', summary TEXT NOT NULL COMMENT '经过整理的事件简短摘要，不是原始聊天记录', importance BIGINT NOT NULL COMMENT '事件重要程度评分，供回忆筛选使用',
          happened DOUBLE NOT NULL COMMENT '事件被描述发生的时间，Unix秒', created DOUBLE NOT NULL COMMENT '事件写入数据库的时间，Unix秒',
  KEY `idx_brain_events_scope_time` (scope, happened DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='BrainBook 从可信消息中提取的情景事件；以聊天作用域隔离';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_event_evidence` (
          event_id VARCHAR(191) NOT NULL COMMENT '所关联的情景事件ID，对应 brain_events.id', message_digest VARCHAR(191) NOT NULL COMMENT '可信聊天消息的摘要哈希，不保存消息原文',
          PRIMARY KEY(event_id,message_digest),
          FOREIGN KEY(event_id) REFERENCES `mlw_brain_events`(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='情景事件与可信消息摘要哈希的多对多关联；用于追溯证据';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_facts` (
          id VARCHAR(191) PRIMARY KEY COMMENT '结构化事实唯一ID', scope VARCHAR(191) NOT NULL COMMENT '事实生效的私聊或群聊作用域', subject_id VARCHAR(191) NOT NULL COMMENT '事实所描述的用户或主体ID',
          attribute VARCHAR(191) NOT NULL COMMENT '事实属性名称，例如学习进度或偏好', value TEXT NOT NULL COMMENT '事实具体值，为模型提取结果且须有可信消息证据', confidence DOUBLE NOT NULL COMMENT '事实提案置信度，取值0到1',
          status VARCHAR(191) NOT NULL DEFAULT 'pending' COMMENT '事实审核状态：pending待证实、active有效、disputed争议、superseded被替代、rejected已拒绝', seen_count BIGINT NOT NULL DEFAULT 0 COMMENT '当前事实的独立证据累计数量',
          created DOUBLE NOT NULL COMMENT '事实首次创建时间，Unix秒', updated DOUBLE NOT NULL COMMENT '事实最近更新时间，Unix秒', valid_until DOUBLE COMMENT '事实终止有效时间，Unix秒；为空表示未设置期限',
          reviewed_count BIGINT NOT NULL DEFAULT 0 COMMENT '被拒绝或替代时已经审核过的证据数量，防止旧证据重复计入',
  KEY `idx_brain_facts_scope_subject` (scope,subject_id,attribute,status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='BrainBook 结构化事实及审核状态；同义合并和冲突审核在此体现';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_fact_evidence` (
          fact_id VARCHAR(191) NOT NULL COMMENT '所关联的结构化事实ID，对应 brain_facts.id', message_digest VARCHAR(191) NOT NULL COMMENT '可信消息摘要哈希，用于去重和审核，不是聊天原文',
          PRIMARY KEY(fact_id,message_digest),
          FOREIGN KEY(fact_id) REFERENCES `mlw_brain_facts`(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='结构化事实与可信消息摘要哈希的关联，用于独立证据计数';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_goals` (
          id VARCHAR(191) PRIMARY KEY COMMENT '目标唯一ID', scope VARCHAR(191) NOT NULL COMMENT '目标所属的聊天记忆作用域', owner_id VARCHAR(191) NOT NULL COMMENT '目标发起人或负责人用户ID',
          title TEXT NOT NULL COMMENT '目标简短名称，例如整理数据线', criterion TEXT NOT NULL COMMENT '目标完成的明确条件，结案时逐项核验',
          next_action VARCHAR(191) NOT NULL DEFAULT 'wait' COMMENT '下一步内部建议：wait、follow_up、compare_evidence、review；不直接执行外部动作',
          status VARCHAR(191) NOT NULL DEFAULT 'open' COMMENT '目标状态：open待完成、done已完成、abandoned已放弃；阶段进展另记于 brain_goal_events', revision BIGINT NOT NULL DEFAULT 1 COMMENT '目标修改版本号，便于追踪状态变化',
          created DOUBLE NOT NULL COMMENT '目标创建时间，Unix秒', updated DOUBLE NOT NULL COMMENT '目标最近更新时间，Unix秒', closed_at DOUBLE COMMENT '目标结案时间，Unix秒；未完成时为空') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='BrainBook 主人目标与完成条件；仅有确证后才能完成或放弃';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_goal_events` (
          goal_id VARCHAR(191) NOT NULL COMMENT '关联的目标ID，对应 brain_goals.id', message_digest VARCHAR(191) NOT NULL COMMENT '目标阶段所依据的可信消息摘要哈希',
          kind VARCHAR(191) NOT NULL COMMENT '阶段标记：origin创建、in_progress进行中、done完成、abandoned放弃', recorded DOUBLE NOT NULL COMMENT '阶段证据记录时间，Unix秒',
          PRIMARY KEY(goal_id,message_digest,kind),
          FOREIGN KEY(goal_id) REFERENCES `mlw_brain_goals`(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='目标证据与阶段时间线，记录创建、进行中、完成或放弃的依据';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_decisions` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '决策记录自增ID', scope VARCHAR(191) NOT NULL COMMENT '此决策所属聊天作用域', goal_id VARCHAR(191) COMMENT '相关目标ID；未关联具体目标时为空',
          feedback_event_id BIGINT UNIQUE COMMENT '主动发送反馈事件ID；唯一且允许为空', action VARCHAR(191) NOT NULL COMMENT '本次计划采取的内部行动类别',
          reason TEXT NOT NULL COMMENT '采取该行动的原因摘要', outcome VARCHAR(191) NOT NULL DEFAULT 'submitted' COMMENT '执行结果状态，区别仅提交、确认发送和失败',
          user_feedback VARCHAR(191) NOT NULL DEFAULT 'unrated' COMMENT '主人对该次主动联系的反馈，默认 unrated未评价',
          recorded DOUBLE NOT NULL COMMENT '决策记录时间，Unix秒', finished DOUBLE COMMENT '决策得到最终结果的时间，Unix秒；未结束时为空') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='BrainBook 主动联系决策及发送回执与主人反馈关联记录';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_cycles` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '整理周期自增ID', scope VARCHAR(191) NOT NULL COMMENT '整理所针对的聊天作用域',
          source_count BIGINT NOT NULL COMMENT '本周期参与处理的可信消息数量', added_events BIGINT NOT NULL COMMENT '本周期新增情景事件数量',
          added_facts BIGINT NOT NULL COMMENT '本周期新增或处理结构化事实数量', updated_goals BIGINT NOT NULL COMMENT '本周期新增或更新目标数量',
          reflection TEXT NOT NULL COMMENT '模型产生的简短复盘文本，仅作为待核实线索', created DOUBLE NOT NULL COMMENT '整理周期完成或记录时间，Unix秒',
          cycle_kind VARCHAR(191) NOT NULL DEFAULT 'deep' COMMENT '整理类型：deep深度周期、quick快速周期') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='BrainBook 整理周期审计和简短元认知反思；反思需经后续证据核实';

-- brain_core.py
CREATE TABLE IF NOT EXISTS `mlw_brain_fact_revisions` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '事实状态变更自增ID', scope VARCHAR(191) NOT NULL COMMENT '状态变更所属聊天作用域', fact_id VARCHAR(191) NOT NULL COMMENT '被审核或修改的事实ID',
          before_status VARCHAR(191) NOT NULL COMMENT '修改前事实审核状态', after_status VARCHAR(191) NOT NULL COMMENT '修改后事实审核状态',
          reason TEXT NOT NULL COMMENT '审核或状态变更原因', changed DOUBLE NOT NULL COMMENT '状态变更发生时间，Unix秒',
  KEY `idx_brain_fact_revisions_scope` (scope,fact_id,id DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='结构化事实的审核状态变更审计，记录前后状态及原因';

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_runs` (
            owner_id VARCHAR(191) PRIMARY KEY COMMENT '被观察的主人用户ID', last_hash VARCHAR(191) NOT NULL DEFAULT '' COMMENT '上次成功处理的对话窗口摘要哈希',
            last_attempt_at DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次反思尝试时间，Unix秒', last_success_at DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次反思成功时间，Unix秒',
            behavior_goal VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '从过去反思得到的短期行为目标提示，可为空', reflection_count BIGINT NOT NULL DEFAULT 0 COMMENT '已成功完成的反思累计次数') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='Mind 主人观察模块的反思周期运行状态与行为目标提示';

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_notes` (
            owner_id VARCHAR(191) NOT NULL COMMENT '该笔记所属主人用户ID', note_key VARCHAR(191) NOT NULL COMMENT '笔记内容归一化哈希键，与 owner_id 组成联合主键', kind VARCHAR(191) NOT NULL COMMENT '笔记类别：preference偏好、shared_experience共同经历、boundary边界、open_topic未完话题',
            text TEXT NOT NULL COMMENT '笔记内容摘要，不是原始聊天全文', basis TEXT NOT NULL COMMENT '证据依据类别：user_explicit主人明确表达、conversation对话情境', confidence DOUBLE NOT NULL COMMENT '笔记置信度，取值0到1',
            seen_count BIGINT NOT NULL DEFAULT 1 COMMENT '被独立证据支持的累计次数', first_seen DOUBLE NOT NULL COMMENT '首次记录此笔记的时间，Unix秒',
            last_seen DOUBLE NOT NULL COMMENT '最近一次证据支持时间，Unix秒', status VARCHAR(191) NOT NULL DEFAULT 'legacy' COMMENT '笔记状态：legacy旧版待核实、pending待确认、active有效、disputed有争议、rejected已拒绝',
            PRIMARY KEY (owner_id,note_key)
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='Mind 对主人偏好、共同经历、边界及未完话题的结构化笔记';

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_evidence` (
            owner_id VARCHAR(191) NOT NULL COMMENT '该证据关联的主人用户ID', note_key VARCHAR(191) NOT NULL COMMENT '所支持的 Mind 笔记键', evidence_id VARCHAR(191) NOT NULL COMMENT '当前整理窗口内的证据编号',
            message_digest VARCHAR(191) NOT NULL COMMENT '可信消息内容摘要哈希，不保存原文', role VARCHAR(191) NOT NULL COMMENT '证据发言者类别，例如 owner主人或 bot机器人',
            PRIMARY KEY (owner_id,note_key,evidence_id)
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='Mind 笔记与聊天证据的关联；按主人、笔记和证据ID去重';

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_reviews` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '纠正审核记录自增ID', owner_id VARCHAR(191) NOT NULL COMMENT '待审核笔记所属主人用户ID',
            target_key VARCHAR(191) NOT NULL COMMENT '需要审核或纠正的 Mind 笔记键', reason TEXT NOT NULL COMMENT '提出冲突或纠正的理由',
            evidence_ids TEXT NOT NULL COMMENT '关联证据编号列表的序列化文本', status VARCHAR(191) NOT NULL DEFAULT 'pending' COMMENT '审核流程状态：pending待审核、approve同意、reject驳回等',
            created_at DOUBLE NOT NULL COMMENT '审核请求创建时间，Unix秒', previous_status VARCHAR(191) NOT NULL DEFAULT 'active' COMMENT '发起审核前笔记的状态，供拒绝纠正时恢复') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='Mind 冲突纠正审核请求及人工审核决定';

-- mind.py
CREATE TABLE IF NOT EXISTS `mlw_mind_snapshots` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '历史快照自增ID', owner_id VARCHAR(191) NOT NULL COMMENT '该快照所属主人用户ID',
            created_at DOUBLE NOT NULL COMMENT '快照生成时间，Unix秒', reason TEXT NOT NULL COMMENT '生成快照的原因，如反思或审核', payload LONGTEXT NOT NULL COMMENT '结构化笔记及证据的JSON快照，不包含原始聊天全文') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='Mind 笔记与证据的结构化历史快照，用于审核回滚';

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_cycles` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '认知周期自增ID',
              attempted_at DOUBLE NOT NULL COMMENT '开始尝试本次公开认知处理的时间，Unix秒', status VARCHAR(191) NOT NULL COMMENT '周期执行状态，如 started、completed 或 failed',
              input_count BIGINT NOT NULL DEFAULT 0 COMMENT '输入的公开资讯来源数量', added BIGINT NOT NULL DEFAULT 0 COMMENT '本次新增的公开认知目标数量',
              updated BIGINT NOT NULL DEFAULT 0 COMMENT '本次更新的既有公开认知目标数量') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='公开认知模块每次观察整理的执行状态和新增更新统计';

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_sources` (
              source_key VARCHAR(191) PRIMARY KEY COMMENT '公开资讯来源的唯一键', seen_at DOUBLE NOT NULL COMMENT '首次处理该来源的时间，Unix秒') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='公开认知已处理的来源键，用于来源级去重';

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_goals` (
              goal_id VARCHAR(191) PRIMARY KEY COMMENT '公开认知问题目标唯一ID', topic TEXT NOT NULL COMMENT '认知主题，例如开源模型', question TEXT NOT NULL COMMENT '仍待研究或核实的具体问题',
              observation TEXT NOT NULL COMMENT '目前已从公开来源观察到的内容', uncertainty TEXT NOT NULL COMMENT '已知的不确定性和仍缺少的证据',
              next_action VARCHAR(191) NOT NULL COMMENT '下一步内部建议：wait_for_update、compare_public_sources、ask_when_relevant', status VARCHAR(191) NOT NULL DEFAULT 'open' COMMENT '认知问题状态，如 open待跟进、resolved已解决、discarded不再跟进',
              created_at DOUBLE NOT NULL COMMENT '创建认知问题的时间，Unix秒', updated_at DOUBLE NOT NULL COMMENT '最近一次观察或状态变更时间，Unix秒',
              revision BIGINT NOT NULL DEFAULT 1 COMMENT '认知问题版本号，更新时递增') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='基于公开来源形成的待研究问题与观察目标，不等于私人任务';

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_evidence` (
              goal_id VARCHAR(191) NOT NULL COMMENT '所关联的公开认知问题ID', source_key VARCHAR(191) NOT NULL COMMENT '来源唯一键，与 goal_id 联合去重', source TEXT NOT NULL COMMENT '来源名称，例如 B站热门',
              title TEXT NOT NULL COMMENT '来源标题', url TEXT NOT NULL COMMENT '来源网页或视频链接', observed_at DOUBLE NOT NULL COMMENT '本次记录来源证据的时间，Unix秒',
              PRIMARY KEY(goal_id,source_key)
          ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='公开认知问题与资讯来源的对应证据；保留标题和来源链接';

-- cognition.py
CREATE TABLE IF NOT EXISTS `mlw_cognitive_events` (
              id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '认知事件自增ID',
              goal_id VARCHAR(191) NOT NULL COMMENT '关联的公开认知问题ID', kind VARCHAR(191) NOT NULL COMMENT '认知事件类型，如创建、更新或审核状态变化',
              note TEXT NOT NULL COMMENT '本次变化的简短说明', source_key VARCHAR(191) COMMENT '关联的公开资讯来源键；可为空',
              created_at DOUBLE NOT NULL COMMENT '事件记录时间，Unix秒') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='公开认知问题的创建、更新与状态变化时间线';

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_actions` (
            event_id BIGINT PRIMARY KEY COMMENT '关联主动发送反馈模块的事件ID，兼任主键',
            owner_id VARCHAR(191) NOT NULL COMMENT '此次尝试针对的主人用户ID',
            session_id VARCHAR(191) NOT NULL COMMENT '对应的真实聊天会话ID',
            kind VARCHAR(191) NOT NULL CHECK(kind IN ('explore','history')) COMMENT '行动类型：explore资讯分享、history旧话题延续',
            topic_digest VARCHAR(191) NOT NULL COMMENT '话题摘要哈希，避免记录敏感原文',
            created_at DOUBLE NOT NULL COMMENT '行动创建时间，Unix秒',
            sent_at DOUBLE COMMENT '平台确认发送成功的时间，Unix秒；未成功则为空',
            outcome VARCHAR(191) NOT NULL DEFAULT 'submitted' COMMENT '行动状态，如 submitted、reply_planned、send_confirmed、send_failed 等',
            feedback VARCHAR(191) NOT NULL DEFAULT 'unrated' COMMENT '接收的用户反馈，如 unrated、positive、negative、reply_only、opt_out',
            feedback_at DOUBLE COMMENT '最近一次可信用户反馈时间，Unix秒；尚无反馈时为空',
  KEY `idx_experience_owner_time` (owner_id,session_id,sent_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主动联系后的经验闭环行动，记录计划、发送状态和反馈结果';

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_evidence` (
            id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '反馈证据自增ID',
            event_id BIGINT NOT NULL COMMENT '对应的经验行动事件ID',
            evidence_digest VARCHAR(191) NOT NULL COMMENT '反馈消息摘要哈希，防止重复统计',
            `signal` VARCHAR(191) NOT NULL COMMENT '从反馈文本识别的信号，例如 positive、negative 或 opt_out',
            created_at DOUBLE NOT NULL COMMENT '证据被记录的时间，Unix秒',
            UNIQUE(event_id,evidence_digest)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='经验反馈的去重证据与解析出的反馈信号，不保存消息原文';

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_policy` (
            owner_id VARCHAR(191) NOT NULL COMMENT '受该冷却策略约束的主人用户ID',
            kind VARCHAR(191) NOT NULL COMMENT '受限行动类型：explore或history',
            avoid_until DOUBLE NOT NULL DEFAULT 0 COMMENT '此类主动行动禁止发送的截止时间，Unix秒；0表示无冷却',
            PRIMARY KEY(owner_id,kind)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='根据明确负面反馈形成的主动联系冷却限制';

-- experience_loop.py
CREATE TABLE IF NOT EXISTS `mlw_experience_opt_out` (
            owner_id VARCHAR(191) PRIMARY KEY COMMENT '明确要求停止主动联系的主人用户ID',
            paused_at DOUBLE NOT NULL COMMENT '接收到停止主动联系要求的时间，Unix秒') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人明确拒绝主动联系后的持久化停止标记';

-- proactive_feedback.py
CREATE TABLE IF NOT EXISTS `mlw_proactive_targets` (
          target VARCHAR(191) PRIMARY KEY COMMENT '主动联系目标的私聊或群聊作用域',
          stop_streak BIGINT NOT NULL DEFAULT 0 COMMENT '连续被规划器停止的次数，用于退避',
          backoff_until DOUBLE NOT NULL DEFAULT 0 COMMENT '下一次可重新尝试的时间下限，Unix秒',
          last_sent_at DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次确认发送成功的时间，Unix秒') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='按聊天目标维护主动发送停止次数、退避时间与最后成功时间';

-- proactive_feedback.py
CREATE TABLE IF NOT EXISTS `mlw_proactive_events` (
          id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主动发送反馈事件自增ID',
          target VARCHAR(191) NOT NULL COMMENT '本次事件关联的私聊或群聊目标',
          session_id VARCHAR(191) NOT NULL COMMENT '对应的会话ID，用于核对发送回执',
          created_at DOUBLE NOT NULL COMMENT '事件创建时间，Unix秒',
          updated_at DOUBLE NOT NULL COMMENT '事件结果最近更新时间，Unix秒',
          outcome VARCHAR(191) NOT NULL COMMENT '发送阶段或结果，如 submitted、planner_stop、reply_planned、send_confirmed、send_failed、unknown',
  KEY `idx_proactive_events_time` (target,created_at,outcome)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主动唤醒与回复规划器回执事件流水，区分计划和真实发送';
