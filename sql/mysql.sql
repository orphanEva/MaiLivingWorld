-- MaiLivingWorld MySQL 8.4 十八套数据共 72 表（含持续自我、成长、工具、陪伴、聊天连续性、相处习惯、任务回告、自主项目与共同阅读）
-- 全部表及字段附中文COMMENT；新库直接执行本文件。
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

-- self_state.py；只建空表，首次运行创建身份，迁移时保留原 agent_id。
CREATE TABLE IF NOT EXISTS `mlw_self_identity` (
    slot BIGINT PRIMARY KEY CHECK(slot=1) COMMENT '单例位置，恒为1；一个数据库命名空间只有一个智能体',
    agent_id VARCHAR(191) NOT NULL UNIQUE COMMENT '持久智能体UUID，跨重启和聊天窗口保持不变',
    owner_id VARCHAR(191) NOT NULL DEFAULT '' COMMENT '管理配置绑定的唯一主人平台用户ID，空值表示尚未绑定',
    owner_platform VARCHAR(191) NOT NULL DEFAULT '' COMMENT '主人的可信身份平台，当前为qq',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '主人绑定修订号，首次绑定及显式更换时递增',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '思考预算当天起始时间，Unix秒',
    thought_count BIGINT NOT NULL DEFAULT 0 COMMENT '当天已预留的思考次数，失败调用也计数',
    last_thought DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次预留思考时间，Unix秒，用于跨重启冷却',
    created DOUBLE NOT NULL COMMENT '身份创建时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近一次主人绑定变更时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='同一个智能体的稳定身份与唯一主人绑定，不按聊天会话分裂';

CREATE TABLE IF NOT EXISTS `mlw_self_owner_history` (
    revision BIGINT PRIMARY KEY COMMENT '对应主人绑定修订号，防止同一版本重复记录',
    agent_id VARCHAR(191) NOT NULL COMMENT '发生绑定变更的持久智能体UUID',
    previous_owner_id VARCHAR(191) NOT NULL COMMENT '变更前的主人平台用户ID，首次绑定为空',
    owner_id VARCHAR(191) NOT NULL COMMENT '显式配置的新主人平台用户ID',
    owner_platform VARCHAR(191) NOT NULL COMMENT '绑定的平台，当前为qq',
    changed DOUBLE NOT NULL COMMENT '管理配置生效时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='唯一主人配置的修订审计，仅供管理端读取，不写入公开模型提示';

-- growth_store.py；只创建空表，迁移时不生成或覆盖成长状态。
CREATE TABLE IF NOT EXISTS `mlw_growth_state` (
    slot BIGINT PRIMARY KEY CHECK(slot=1) COMMENT '单例位置，恒为1，对应当前智能体',
    payload LONGTEXT NOT NULL COMMENT '有界兴趣观点知识问题性格情绪及变化额度的JSON状态',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '成长状态版本，成长回滚和显式重建时递增',
    updated DOUBLE NOT NULL COMMENT '最近一次持久成长变更时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='持续成长的当前结构化档案，读取时计算情绪衰减且不改写原始状态';

CREATE TABLE IF NOT EXISTS `mlw_growth_jobs` (
    id VARCHAR(191) PRIMARY KEY COMMENT '来源指纹及观察模式的稳定任务哈希，防止重复排队',
    payload LONGTEXT NOT NULL COMMENT '来源ID指纹场景及保密属性的JSON引用，不存聊天原文',
    status VARCHAR(191) NOT NULL COMMENT '任务状态：pending、running、done或failed',
    mode VARCHAR(191) NOT NULL COMMENT '任务创建时模式：observe仅观察或active可应用',
    attempts BIGINT NOT NULL DEFAULT 0 COMMENT '已消费的实际模型失败次数，达到三次停止',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前领取者的随机租约令牌',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '租约过期时间，Unix秒，过期可重新领取',
    next_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '下次允许重试的时间，Unix秒',
    created DOUBLE NOT NULL COMMENT '首次排队时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近任务状态变化时间，Unix秒',
    error_code VARCHAR(191) NOT NULL DEFAULT '' COMMENT '固定错误码，不保存模型或聊天原文',
    KEY `idx_growth_jobs_ready` (status,next_attempt,lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='跨重启恢复的学习任务队列，采用租约和有界失败退避';

CREATE TABLE IF NOT EXISTS `mlw_growth_history` (
    revision BIGINT PRIMARY KEY COMMENT '成长变更对应的全局版本号',
    job_id VARCHAR(191) NOT NULL UNIQUE COMMENT '已提交任务或管理操作的唯一标识',
    payload LONGTEXT NOT NULL COMMENT '变更前后状态及来源依赖的JSON审计记录',
    created DOUBLE NOT NULL COMMENT '变更完成时间，Unix秒',
    rolled_back BIGINT NOT NULL DEFAULT 0 COMMENT '是否已回滚或属于回滚记录，1表示是'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='可追溯的成长与管理操作历史，回滚保留源记忆和消费记录';

CREATE TABLE IF NOT EXISTS `mlw_growth_consumed` (
    source_id VARCHAR(191) PRIMARY KEY COMMENT '已用于兴趣性格或情绪变化的原始记录ID，改版也不重复累计',
    job_id VARCHAR(191) NOT NULL COMMENT '首次消费该记录的成长任务ID',
    consumed DOUBLE NOT NULL COMMENT '首次应用变化的时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='经历消费去重账本，回滚及重建不会补充已消费的成长次数';

CREATE TABLE IF NOT EXISTS `mlw_tool_observations` (
    id VARCHAR(191) PRIMARY KEY COMMENT '提供者与规范资源键的稳定观察ID',
    provider VARCHAR(191) NOT NULL COMMENT '受信工具提供者标识',
    resource_key VARCHAR(191) NOT NULL COMMENT '提供者内的规范资源键，不含凭据',
    text TEXT NOT NULL COMMENT '最多600字符的工具原始元数据观察，不是模型总结',
    scope VARCHAR(191) NOT NULL COMMENT '来源场合，跨场合保守合并且不用于内部检索隔离',
    subject_id VARCHAR(191) NOT NULL COMMENT '可信来源关联用户，混合来源时可为空',
    sensitivity VARCHAR(191) NOT NULL COMMENT '保守合并的保密级别，不能由重复公开读取降低',
    url TEXT NOT NULL COMMENT '可选公开来源链接，禁止凭据及查询参数',
    origin_key VARCHAR(191) NOT NULL COMMENT '提供者和原始资源的规范哈希，用于独立证据去重',
    timestamp DOUBLE NOT NULL COMMENT '内容或约束实际变化时间，重复读取不更新',
    active BIGINT NOT NULL DEFAULT 1 COMMENT '1为可用观察，0为已撤回',
    created DOUBLE NOT NULL COMMENT '首次观察时间，Unix秒',
    KEY `idx_tool_observations_active` (active,timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='受信工具的原始观察记忆，更新或撤回会使旧派生结论失效';

CREATE TABLE IF NOT EXISTS `mlw_tool_calls` (
    id VARCHAR(191) PRIMARY KEY COMMENT '提供者工具参数摘要与可信消息上下文的幂等调用ID',
    provider VARCHAR(191) NOT NULL COMMENT '工具提供者标识',
    tool VARCHAR(191) NOT NULL COMMENT '已批准的固定工具名称',
    args_digest VARCHAR(191) NOT NULL COMMENT '调用参数SHA256摘要，不保存参数原文',
    context_digest VARCHAR(191) NOT NULL COMMENT '可信消息ID场合和主体的摘要，不保存原请求键',
    status VARCHAR(191) NOT NULL COMMENT 'reserved、succeeded、failed或uncertain，未知结果禁止重放',
    scope VARCHAR(191) NOT NULL COMMENT '调用发起场合，用于授权追溯',
    subject_id VARCHAR(191) NOT NULL COMMENT '可信调用发起主体',
    budget_day DOUBLE NOT NULL COMMENT '独立全局工具日预算起点，失败和未知调用也计数',
    payload LONGTEXT NOT NULL COMMENT '适配器生成的有界结果状态摘要，不保存密钥或原始响应',
    created DOUBLE NOT NULL COMMENT '原子预留调用时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '调用状态最近变化时间，Unix秒',
    KEY `idx_tool_calls_day` (budget_day,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='跨重启幂等调用及全局工具预算，独立于模型调用预算';

CREATE TABLE IF NOT EXISTS `mlw_tool_tasks` (
    id VARCHAR(191) PRIMARY KEY COMMENT '提供者与任务ID的稳定哈希',
    provider VARCHAR(191) NOT NULL COMMENT '任务所属工具提供者',
    task_id VARCHAR(191) NOT NULL COMMENT '提供者返回的任务ID，不是URL或文件路径',
    scope VARCHAR(191) NOT NULL COMMENT '任务首次创建的可信场合，禁止重新绑定',
    subject_id VARCHAR(191) NOT NULL COMMENT '任务首次创建的可信主体，禁止重新绑定',
    operation VARCHAR(191) NOT NULL COMMENT '原始工具操作名，查询或重试时重新检查权限',
    created DOUBLE NOT NULL COMMENT '首次绑定时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '绑定记录时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='后台工具任务的主体场合绑定，不保存路径或签名下载地址';

CREATE TABLE IF NOT EXISTS `mlw_tool_research` (
    question_key VARCHAR(191) PRIMARY KEY COMMENT '待研究问题键或内部控制行标识，不保存问题正文',
    status VARCHAR(191) NOT NULL DEFAULT 'idle' COMMENT 'idle、running、succeeded或failed',
    attempts BIGINT NOT NULL DEFAULT 0 COMMENT '研究领取次数，控制行记录全局次数',
    failures BIGINT NOT NULL DEFAULT 0 COMMENT '连续失败次数，用于指数退避',
    last_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '最近领取时间，重启保留冷却和轮转顺序',
    next_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '允许下次研究该问题的时间，Unix秒',
    last_finish DOUBLE NOT NULL DEFAULT 0 COMMENT '最近完成时间，Unix秒',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '研究租约令牌，本地轮转控制行保存上一问题键',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '研究租约截止时间，过期允许恢复',
    interval_seconds BIGINT NOT NULL DEFAULT 0 COMMENT '领取时的研究冷却秒数',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '控制行的独立研究日预算起点',
    daily_count BIGINT NOT NULL DEFAULT 0 COMMENT '控制行的当日研究预留次数，失败也计数',
    updated DOUBLE NOT NULL DEFAULT 0 COMMENT '最近调度状态变化时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='跨重启研究轮转租约退避及独立预算，本地检索轮转不消耗网络额度';

CREATE TABLE IF NOT EXISTS `mlw_companion_control` (
    slot BIGINT PRIMARY KEY CHECK(slot=1) COMMENT '单例控制位置，恒为1',
    updated DOUBLE NOT NULL DEFAULT 0 COMMENT '最近阅读预算预留时间，Unix秒',
    owner_id VARCHAR(191) NOT NULL DEFAULT '' COMMENT '最后一次受信阅读预留的主人标识，不迁移旧计划',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '独立阅读预算的日边界，Unix秒',
    daily_count BIGINT NOT NULL DEFAULT 0 COMMENT '当日已预留阅读周期数，失败也占用',
    last_cycle DOUBLE NOT NULL DEFAULT 0 COMMENT '最近预留阅读周期时间，重启保留冷却'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='陪伴账本事务互斥与独立阅读调度预算，构造时不生成控制行';

CREATE TABLE IF NOT EXISTS `mlw_companion_readings` (
    id VARCHAR(191) PRIMARY KEY COMMENT '主人与提供者资源版本及根来源指纹绑定的稳定阅读ID',
    owner_id VARCHAR(191) NOT NULL COMMENT '发起阅读的可信主人标识，不随主人配置变更迁移',
    scope VARCHAR(191) NOT NULL COMMENT '首次发起场合，仅作来源及表达约束',
    provider VARCHAR(191) NOT NULL COMMENT '固定提供者cap或pixiv',
    resource_type VARCHAR(191) NOT NULL COMMENT '资源类型pdf、novel或illust',
    resource_id VARCHAR(191) NOT NULL COMMENT '提供者内资源标识，不是路径或签名URL',
    version VARCHAR(191) NOT NULL COMMENT '可信资源版本锚，不自动替换',
    status VARCHAR(191) NOT NULL COMMENT 'active、paused或completed',
    cursor_pos BIGINT NOT NULL DEFAULT 0 COMMENT '连续已读位置，文本为Unicode字符数，图片及PDF为页数',
    total_units BIGINT NOT NULL DEFAULT 0 COMMENT '已验证资源总字符或页数，0表示未知不能声称读完',
    revision BIGINT NOT NULL COMMENT '阅读任务与派生笔记变更的乐观锁版本',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前读取任务随机租约令牌',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '当前读取租约截止时间，Unix秒',
    payload LONGTEXT NOT NULL COMMENT '标题固定版本来源引用与暂停错误码的JSON',
    created DOUBLE NOT NULL COMMENT '首次开启阅读时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近进度任务或笔记变化时间，Unix秒',
    KEY `idx_companion_readings_owner` (owner_id,status,updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='固定资源版本的连续阅读会话，来源失效则暂停且保留已读进度';

CREATE TABLE IF NOT EXISTS `mlw_companion_units` (
    id VARCHAR(191) PRIMARY KEY COMMENT '阅读ID与连续单元起点的稳定哈希',
    reading_id VARCHAR(191) NOT NULL COMMENT '所属固定版本阅读会话',
    unit_index BIGINT NOT NULL COMMENT '实际读取页码或原文Unicode字符起点，始于0',
    next_cursor BIGINT NOT NULL COMMENT '本单元真实内容对应的下一个连续游标',
    payload LONGTEXT NOT NULL COMMENT '实际原文图片摘要单元来源及pending或reviewed派生笔记的JSON',
    created DOUBLE NOT NULL COMMENT '首次成功提交该读取单元的时间，Unix秒',
    UNIQUE KEY `uq_companion_units_position` (reading_id,unit_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='实际读取单元的原文或图片摘要及独立派生笔记，禁止跨页跳过';

CREATE TABLE IF NOT EXISTS `mlw_companion_plans` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信主人提议上下文生成的稳定计划ID',
    owner_id VARCHAR(191) NOT NULL COMMENT '计划原主人标识，禁止迁移给新主人',
    scope VARCHAR(191) NOT NULL COMMENT '首次提议场合，供授权追溯及表达判断',
    status VARCHAR(191) NOT NULL COMMENT 'proposed、active、paused、completed或cancelled',
    revision BIGINT NOT NULL COMMENT '主人命令变更版本，提醒不等于同意修改计划',
    payload LONGTEXT NOT NULL COMMENT '标题下一步到期时间及独立提醒次数游标的JSON',
    created DOUBLE NOT NULL COMMENT '首次草稿创建时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近主人命令修改时间，Unix秒',
    KEY `idx_companion_plans_owner` (owner_id,status,updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人共同计划，模型提议为草稿且状态切换必须来自可信主人命令';

CREATE TABLE IF NOT EXISTS `mlw_companion_audit` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信主人场合与消息ID生成的幂等命令标识',
    entity_id VARCHAR(191) NOT NULL COMMENT '关联计划ID',
    owner_id VARCHAR(191) NOT NULL COMMENT '实际命令所属可信主人标识',
    action VARCHAR(191) NOT NULL COMMENT '提议接受暂停恢复编辑完成或取消操作',
    message_digest VARCHAR(191) NOT NULL COMMENT '可信消息ID与场合的SHA256摘要',
    summary_hash VARCHAR(191) NOT NULL COMMENT '可信原消息摘要的SHA256，不保存原文',
    payload LONGTEXT NOT NULL COMMENT '命令签名及操作前后状态的JSON',
    created DOUBLE NOT NULL COMMENT '命令事务提交时间，Unix秒',
    KEY `idx_companion_audit_entity` (entity_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人计划命令的幂等和变更审计，不保存原始消息正文';

CREATE TABLE IF NOT EXISTS `mlw_bond_profiles` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '相处约定所属的可信主人标识，不随主人配置变更迁移',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '主人约定修改的乐观锁版本，不表示好感或关系等级',
    payload LONGTEXT NOT NULL COMMENT '主人明确设置的称呼亲昵表达偏好与相处边界JSON',
    created DOUBLE NOT NULL COMMENT '首次明确设置约定的时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近约定变更时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='可信主人明确设置的相处约定与节奏，不生成亲密度分数';

CREATE TABLE IF NOT EXISTS `mlw_bond_moments` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信主人命令生成的稳定纪念日或回忆ID',
    owner_id VARCHAR(191) NOT NULL COMMENT '该条回忆或纪念日原主人标识，禁止自动迁移给新主人',
    scope VARCHAR(191) NOT NULL COMMENT '首次记录场合，供来源追溯和表达判断',
    kind VARCHAR(191) NOT NULL COMMENT '明确记录的纪念日或共同回忆类型',
    status VARCHAR(191) NOT NULL COMMENT '当前记录状态，不由模型生成内容自行认定为真实经历',
    revision BIGINT NOT NULL COMMENT '主人命令修改记录的乐观锁版本',
    payload LONGTEXT NOT NULL COMMENT '主人确认的标题内容日期和来源摘要JSON，不保存隐藏推理',
    created DOUBLE NOT NULL COMMENT '首次明确记下该事项的时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近内容或状态变化时间，Unix秒',
    KEY `idx_bond_moments_owner` (owner_id,status,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人通过明确命令留下的纪念日和共同回忆，模型提议不等于已发生';

CREATE TABLE IF NOT EXISTS `mlw_bond_audit` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信主人场合消息与命令生成的幂等审计ID',
    owner_id VARCHAR(191) NOT NULL COMMENT '实际命令所属可信主人标识',
    entity_id VARCHAR(191) NOT NULL COMMENT '关联相处约定或纪念事项的标识',
    action VARCHAR(191) NOT NULL COMMENT '实际执行的约定修改记录或撤回操作',
    message_digest VARCHAR(191) NOT NULL COMMENT '可信消息与场合的SHA256摘要',
    body_digest VARCHAR(191) NOT NULL COMMENT '原命令正文的SHA256摘要，不保存命令原文',
    payload LONGTEXT NOT NULL COMMENT '命令签名及修改前后结构化状态JSON',
    created DOUBLE NOT NULL COMMENT '命令事务提交时间，Unix秒',
    KEY `idx_bond_audit_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='相处约定与纪念事项的幂等和修改审计，保留原主人归属';

CREATE TABLE IF NOT EXISTS `mlw_continuity_control` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '可信主人标识，旧主人状态不会自动转移',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '控制状态与待续话题修改的乐观锁版本',
    payload LONGTEXT NOT NULL COMMENT '明确联系暂停状态及支持方式偏好JSON',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '提取额度所属本地日期起点，Unix秒',
    cycle_count BIGINT NOT NULL DEFAULT 0 COMMENT '当日已预留的连续性提取模型次数',
    last_cycle DOUBLE NOT NULL DEFAULT 0 COMMENT '最近预留模型次数的时间，Unix秒',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '单主人提取批次独占租约令牌',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '提取租约失效时间，Unix秒',
    lease_id VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前提取批次起始输入标识',
    created DOUBLE NOT NULL COMMENT '首次接收可信主人操作的时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近控制状态修改时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人聊天连续性控制与明确联系节奏，不把未回消息当负面反馈';

CREATE TABLE IF NOT EXISTS `mlw_continuity_inputs` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信主人场合与消息ID的稳定摘要',
    owner_id VARCHAR(191) NOT NULL COMMENT '原始输入所属的可信主人标识',
    body_digest VARCHAR(191) NOT NULL COMMENT '原始正文SHA256摘要，用于来源变更校验',
    status VARCHAR(191) NOT NULL COMMENT '待处理运行中已处理过期或撤回状态',
    attempts BIGINT NOT NULL DEFAULT 0 COMMENT '有界重试次数',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前提取批次租约令牌',
    next_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '下次允许提取时间，Unix秒',
    expires DOUBLE NOT NULL COMMENT '未处理原文队列的到期时间，Unix秒',
    payload LONGTEXT NOT NULL COMMENT '有界待处理原文与输入元数据JSON，完成或过期后清除原文',
    created DOUBLE NOT NULL COMMENT '首次采集时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近输入状态变化时间，Unix秒',
    KEY `idx_continuity_inputs_owner` (owner_id,status,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='可信主人私聊的有界提取队列与来源摘要，完成后清除待处理原文';

CREATE TABLE IF NOT EXISTS `mlw_continuity_threads` (
    id VARCHAR(191) PRIMARY KEY COMMENT '主人话题的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '话题原主人标识，不迁移给新主人',
    kind VARCHAR(191) NOT NULL COMMENT '事件关心或愿望类型，不等于确认事实',
    status VARCHAR(191) NOT NULL COMMENT 'open待续resolved已解决或dismissed不再追问',
    revision BIGINT NOT NULL COMMENT '话题内容和状态的修改版本',
    payload LONGTEXT NOT NULL COMMENT '标题摘要与可信输入引用JSON，不保存隐藏推理',
    created DOUBLE NOT NULL COMMENT '首次提取出话题的时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近话题内容或状态变化时间，Unix秒',
    KEY `idx_continuity_threads_owner` (owner_id,status,updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='有主人原话证据的待续话题，保留已解决与不再追问状态而非关系分';

CREATE TABLE IF NOT EXISTS `mlw_continuity_audit` (
    id VARCHAR(191) PRIMARY KEY COMMENT '操作来源和批次的幂等审计标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '实际操作所属的可信主人标识',
    entity_id VARCHAR(191) NOT NULL COMMENT '关联控制状态话题或输入标识',
    action VARCHAR(191) NOT NULL COMMENT '实际完成的提取或明确状态修改动作',
    body_digest VARCHAR(191) NOT NULL COMMENT '可信来源正文或批次摘要',
    payload LONGTEXT NOT NULL COMMENT '修改前后结构化状态及来源引用JSON',
    created DOUBLE NOT NULL COMMENT '审计提交时间，Unix秒',
    KEY `idx_continuity_audit_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='连续性提取与明确联系节奏修改审计，不证明机器人已经问候';

CREATE TABLE IF NOT EXISTS `mlw_attention_control` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '可信主人标识，原主人状态不随配置变更迁移',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '注意安排控制状态的乐观锁版本',
    payload LONGTEXT NOT NULL COMMENT '关注事项对话活动来源摘要及连续状态JSON，不保存聊天正文',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '自主活动额度所属日期起点，Unix秒',
    cycle_count BIGINT NOT NULL DEFAULT 0 COMMENT '当日已预留的活动批次次数',
    last_started DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次活动批次开始时间，Unix秒',
    engaged_until DOUBLE NOT NULL DEFAULT 0 COMMENT '主人当前互动保留窗口截止时间，Unix秒',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前独占活动批次租约令牌',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '活动批次租约失效时间，Unix秒',
    lease_revision BIGINT NOT NULL DEFAULT 0 COMMENT '领取活动时控制状态版本，防止旧任务提交',
    last_finished DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次活动批次完成时间，Unix秒',
    last_outcome VARCHAR(191) NOT NULL DEFAULT '' COMMENT '最近活动结果，不据此推断消息已成功送达',
    created DOUBLE NOT NULL COMMENT '首次接收可信互动或活动安排时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近控制状态变化时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人互动与自主活动的持久注意安排，尝试不等于执行或发送成功';

CREATE TABLE IF NOT EXISTS `mlw_attention_receipts` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信入站消息与场合的稳定摘要',
    owner_id VARCHAR(191) NOT NULL COMMENT '实际消息所属的可信主人标识',
    body_digest VARCHAR(191) NOT NULL COMMENT '原始正文SHA256摘要，用于重放冲突检测',
    revision BIGINT NOT NULL COMMENT '该消息首次更新注意安排时的控制版本',
    mode_hash VARCHAR(191) NOT NULL DEFAULT '' COMMENT '该消息应用时模式配置的摘要',
    created DOUBLE NOT NULL COMMENT '首次采集该可信消息的时间，Unix秒',
    KEY `idx_attention_receipts_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='可信主人入站互动去重凭据，只保存摘要而非聊天正文';

CREATE TABLE IF NOT EXISTS `mlw_attention_events` (
    id VARCHAR(191) PRIMARY KEY COMMENT '稳定活动或状态变更审计标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '该条活动审计所属的可信主人标识',
    action VARCHAR(191) NOT NULL COMMENT '实际已发生的控制变更或批次结果',
    payload LONGTEXT NOT NULL COMMENT '有界结构化活动结果JSON，不保存隐藏推理或聊天正文',
    created DOUBLE NOT NULL COMMENT '事件提交时间，Unix秒',
    KEY `idx_attention_events_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='注意安排与活动结果审计，不存正文且不冒称已经关心';

CREATE TABLE IF NOT EXISTS `mlw_recollection_control` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '可信主人标识，原主人状态不随配置变更迁移',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '回忆修正控制状态的乐观锁版本',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '提取预算所属日期起点，Unix秒',
    cycle_count BIGINT NOT NULL DEFAULT 0 COMMENT '当日已预留的回忆修正提取批次',
    last_cycle DOUBLE NOT NULL DEFAULT 0 COMMENT '最近预留提取批次的时间，Unix秒',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前提取批次的独占租约令牌',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '提取租约失效时间，Unix秒',
    lease_id VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前提取批次的输入标识',
    created DOUBLE NOT NULL COMMENT '首次控制状态建立时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近控制状态修改时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人回忆修正的持久租约与提取预算，不覆盖原始事实';

CREATE TABLE IF NOT EXISTS `mlw_recollection_inputs` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信场合消息与输入类型的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '输入所属可信主人标识',
    kind VARCHAR(191) NOT NULL COMMENT '原话反馈或其他可信输入的类型',
    body_digest VARCHAR(191) NOT NULL COMMENT '原始正文SHA256摘要，用于重复和变更核验',
    status VARCHAR(191) NOT NULL COMMENT '输入等待运行完成过期或撤回状态',
    attempts BIGINT NOT NULL DEFAULT 0 COMMENT '有界重试次数',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前处理租约令牌',
    next_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '下次允许处理时间，Unix秒',
    expires DOUBLE NOT NULL COMMENT '未处理输入的到期时间，Unix秒',
    payload LONGTEXT NOT NULL COMMENT '有界输入及可信引用JSON，不保存隐藏推理',
    created DOUBLE NOT NULL COMMENT '首次采集时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近输入状态变化时间，Unix秒',
    KEY `idx_recollection_inputs_owner` (owner_id,status,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人原话与发送回执相关的有界回忆修正输入队列';

CREATE TABLE IF NOT EXISTS `mlw_recollection_revisions` (
    id VARCHAR(191) PRIMARY KEY COMMENT '回忆修正记录的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '修正记录原主人标识，不转移给新主人',
    target_id VARCHAR(191) NOT NULL COMMENT '被补充或修正的原始来源标识',
    target_digest VARCHAR(191) NOT NULL COMMENT '原始来源版本摘要，失配后不继续应用',
    input_id VARCHAR(191) NOT NULL COMMENT '本次修正的可信输入标识',
    relation VARCHAR(191) NOT NULL COMMENT '补充澄清纠正或反馈关系类型',
    status VARCHAR(191) NOT NULL COMMENT '修正记录当前审核及有效状态',
    revision BIGINT NOT NULL COMMENT '记录变更版本',
    payload LONGTEXT NOT NULL COMMENT '有界修正内容与证据引用JSON，不作为自证事实',
    created DOUBLE NOT NULL COMMENT '首次记录修正的时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近修正内容或状态变化时间，Unix秒',
    KEY `idx_recollection_revisions_owner` (owner_id,status,created),
    KEY `idx_recollection_revisions_target` (owner_id,target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='带可信输入和原来源版本的回忆修正建议，不改写原始证据';

CREATE TABLE IF NOT EXISTS `mlw_recollection_deliveries` (
    id VARCHAR(191) PRIMARY KEY COMMENT '真实发送回执的稳定幂等标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '成功回执对应的可信主人标识',
    scope VARCHAR(191) NOT NULL COMMENT '经核验的实际发送场合',
    text_digest VARCHAR(191) NOT NULL COMMENT '已发送文本SHA256摘要',
    payload LONGTEXT NOT NULL COMMENT '有界回执信息与使用来源引用JSON',
    created DOUBLE NOT NULL COMMENT '确认成功发送的时间，Unix秒',
    KEY `idx_recollection_deliveries_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='真实发送成功回执与来源引用，尝试和生成不等于已经说过';

CREATE TABLE IF NOT EXISTS `mlw_habits_control` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '可信主人标识，原主人状态不随配置变更迁移',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '相处习惯学习控制状态的乐观锁版本',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '学习预算所属日期起点，Unix秒',
    cycle_count BIGINT NOT NULL DEFAULT 0 COMMENT '当日已预留的相处习惯学习学习批次',
    last_cycle DOUBLE NOT NULL DEFAULT 0 COMMENT '最近预留学习批次的时间，Unix秒',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前学习批次的独占租约令牌',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '学习租约失效时间，Unix秒',
    lease_id VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前学习批次的输入标识',
    created DOUBLE NOT NULL COMMENT '首次控制状态建立时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近控制状态修改时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人相处习惯学习的持久租约与学习预算，不覆盖原始事实';

CREATE TABLE IF NOT EXISTS `mlw_habits_inputs` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信场合消息与输入类型的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '输入所属可信主人标识',
    kind VARCHAR(191) NOT NULL COMMENT '输入类型：input反馈、command命令、evidence正文摘要去重收据',
    body_digest VARCHAR(191) NOT NULL COMMENT '原始正文SHA256摘要，用于重复和变更核验',
    status VARCHAR(191) NOT NULL COMMENT '输入处理状态；receipt表示仅用于去重的摘要收据',
    attempts BIGINT NOT NULL DEFAULT 0 COMMENT '有界重试次数',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前处理租约令牌',
    next_attempt DOUBLE NOT NULL DEFAULT 0 COMMENT '下次允许处理时间，Unix秒',
    expires DOUBLE NOT NULL COMMENT '未处理输入的到期时间，Unix秒',
    payload LONGTEXT NOT NULL COMMENT '有界输入及可信引用JSON，不保存隐藏推理',
    created DOUBLE NOT NULL COMMENT '首次采集时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近输入状态变化时间，Unix秒',
    KEY `idx_habits_inputs_owner` (owner_id,status,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人反馈与明确习惯命令相关的有界相处习惯学习输入队列';

CREATE TABLE IF NOT EXISTS `mlw_habits_preferences` (
    id VARCHAR(191) PRIMARY KEY COMMENT '交流习惯槽位的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '偏好所属的可信主人标识，不随主人配置变更迁移',
    situation VARCHAR(191) NOT NULL COMMENT '固定交流场景枚举',
    dimension VARCHAR(191) NOT NULL COMMENT '固定交流偏好维度枚举',
    status VARCHAR(191) NOT NULL COMMENT 'active已生效pending待确认或cleared已清除状态',
    revision BIGINT NOT NULL COMMENT '偏好记录变更版本',
    payload LONGTEXT NOT NULL COMMENT '有界偏好枚举与可信来源引用JSON，不保存隐藏推理',
    created DOUBLE NOT NULL COMMENT '首次建立偏好记录时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近偏好状态变化时间，Unix秒',
    UNIQUE KEY `idx_habits_preferences_slot` (owner_id,situation,dimension)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人明确确认或经反馈复核的有限交流习惯，不构建关系分数';

CREATE TABLE IF NOT EXISTS `mlw_habits_audit` (
    id VARCHAR(191) PRIMARY KEY COMMENT '习惯操作审计的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '实际操作所属的可信主人标识',
    preference_id VARCHAR(191) NOT NULL COMMENT '关联交流习惯槽位标识',
    input_id VARCHAR(191) NOT NULL COMMENT '关联可信输入或命令标识',
    action VARCHAR(191) NOT NULL COMMENT '实际执行的有限习惯操作类型',
    revision BIGINT NOT NULL COMMENT '操作完成时的偏好版本',
    payload LONGTEXT NOT NULL COMMENT '有界枚举及来源摘要JSON，不保存原始聊天正文',
    created DOUBLE NOT NULL COMMENT '审计提交时间，Unix秒',
    KEY `idx_habits_audit_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人相处习惯的幂等审计，仅保存受限枚举与摘要';

CREATE TABLE IF NOT EXISTS `mlw_followthrough_control` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '可信主人标识，原主人状态不转移给新主人',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '回告控制状态的乐观锁版本',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '复查预算所属日期起点，Unix秒',
    daily_checks BIGINT NOT NULL DEFAULT 0 COMMENT '当日已预留的任务状态复查次数',
    last_check DOUBLE NOT NULL DEFAULT 0 COMMENT '最近预留状态复查时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近控制状态更新时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人承诺回告的持久复查预算，不增加原任务权限';

CREATE TABLE IF NOT EXISTS `mlw_followthrough_items` (
    id VARCHAR(191) PRIMARY KEY COMMENT '跟进任务的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '承诺事项所属的可信主人标识',
    status VARCHAR(191) NOT NULL COMMENT '跟进事项当前执行与回告状态',
    revision BIGINT NOT NULL COMMENT '事项状态变化版本',
    payload LONGTEXT NOT NULL COMMENT '既有任务引用与核验状态JSON，不存任意操作指令',
    created DOUBLE NOT NULL COMMENT '首次绑定跟进事项时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近事项状态变化时间，Unix秒',
    KEY `idx_followthrough_items_owner` (owner_id,status,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='绑定真实既有任务的主人承诺回告，尝试和生成不等于完成';

CREATE TABLE IF NOT EXISTS `mlw_followthrough_receipts` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信操作或真实回执的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '实际回执所属的可信主人标识',
    item_id VARCHAR(191) NOT NULL COMMENT '关联承诺回告事项标识',
    kind VARCHAR(191) NOT NULL COMMENT '可信操作或回执类型',
    body_digest VARCHAR(191) NOT NULL COMMENT '实际操作或消息内容摘要，用于幂等与冲突校验',
    created DOUBLE NOT NULL COMMENT '回执确认时间，Unix秒',
    KEY `idx_followthrough_receipts_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='真实任务绑定与回告回执的幂等记录，不存私聊正文';

CREATE TABLE IF NOT EXISTS `mlw_followthrough_sources` (
    id VARCHAR(191) PRIMARY KEY COMMENT '真实创建任务的可信来源绑定标识',
    provider VARCHAR(191) NOT NULL COMMENT '创建任务的固定MCP服务类型',
    task_id VARCHAR(191) NOT NULL COMMENT '由真实任务创建结果返回的标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '创建任务时经核验的可信主人标识',
    scope VARCHAR(191) NOT NULL COMMENT '创建任务时经核验的主人私聊场合',
    operation VARCHAR(191) NOT NULL COMMENT '实际已执行的许可任务创建操作',
    source_signature VARCHAR(191) NOT NULL COMMENT '任务创建时MCP服务配置签名，服务变更使旧任务不可复用',
    created DOUBLE NOT NULL COMMENT '可信创建绑定确认时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='真实任务创建时的主人与MCP服务绑定，防止服务切换复用旧任务标识';

CREATE TABLE IF NOT EXISTS `mlw_projects_control` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '项目所属可信主人，原主人状态不转移给新主人',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '项目控制状态的乐观锁版本',
    budget_day DOUBLE NOT NULL DEFAULT 0 COMMENT '项目动作预算所属日期起点，Unix秒',
    daily_actions BIGINT NOT NULL DEFAULT 0 COMMENT '当日已预留的项目推进次数',
    last_action DOUBLE NOT NULL DEFAULT 0 COMMENT '最近预留项目动作时间，Unix秒',
    lease_token VARCHAR(191) NOT NULL DEFAULT '' COMMENT '项目调度的独占租约令牌',
    lease_until DOUBLE NOT NULL DEFAULT 0 COMMENT '项目调度租约失效时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近控制状态更新时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='有限自主项目的持久动作预算与调度租约，不增加外部操作权限';

CREATE TABLE IF NOT EXISTS `mlw_projects_items` (
    id VARCHAR(191) PRIMARY KEY COMMENT '自主项目的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '项目所属可信主人',
    status VARCHAR(191) NOT NULL COMMENT '项目当前执行状态，完成不等于问题已解决',
    stage VARCHAR(191) NOT NULL COMMENT '有限研究流程所处的阶段',
    revision BIGINT NOT NULL COMMENT '项目内容和状态版本',
    payload LONGTEXT NOT NULL COMMENT '项目种子、证据引用、有限成果与租约JSON，不保存隐藏推理',
    created DOUBLE NOT NULL COMMENT '首次建立项目时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近项目状态变化时间，Unix秒',
    KEY `idx_projects_items_owner` (owner_id,status,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='根据有效兴趣和未解问题推进的有限自主项目，保留原始来源引用';

CREATE TABLE IF NOT EXISTS `mlw_projects_audit` (
    id VARCHAR(191) PRIMARY KEY COMMENT '项目操作审计的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '项目操作所属可信主人',
    item_id VARCHAR(191) NOT NULL COMMENT '关联的自主项目标识',
    action VARCHAR(191) NOT NULL COMMENT '实际发生的项目阶段或主人控制操作',
    body_digest VARCHAR(191) NOT NULL COMMENT '输入或阶段结果摘要，校验幂等与冲突',
    payload LONGTEXT NOT NULL COMMENT '有界项目操作记录和版本引用JSON，不保存隐藏推理',
    created DOUBLE NOT NULL COMMENT '审计提交时间，Unix秒',
    KEY `idx_projects_audit_item` (item_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='自主项目状态和可信主人控制的幂等审计';

CREATE TABLE IF NOT EXISTS `mlw_shared_reading_control` (
    owner_id VARCHAR(191) PRIMARY KEY COMMENT '共读控制所属可信主人',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '共同阅读控制状态版本',
    updated DOUBLE NOT NULL DEFAULT 0 COMMENT '最近控制状态更新时间，Unix秒'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='主人共同阅读记录的事务互斥与状态版本';

CREATE TABLE IF NOT EXISTS `mlw_shared_reading_sessions` (
    id VARCHAR(191) PRIMARY KEY COMMENT '共同阅读记录的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '共读进度所属可信主人',
    reading_id VARCHAR(191) NOT NULL COMMENT '绑定既有阅读及来源版本的记录标识',
    status VARCHAR(191) NOT NULL COMMENT '共同阅读当前有效或停止状态',
    revision BIGINT NOT NULL COMMENT '共读边界与讨论状态的乐观锁版本',
    payload LONGTEXT NOT NULL COMMENT '真实阅读引用、主人确认边界和成功送达讨论进度JSON',
    created DOUBLE NOT NULL COMMENT '首次建立共同阅读时间，Unix秒',
    updated DOUBLE NOT NULL COMMENT '最近共读状态变化时间，Unix秒',
    UNIQUE (owner_id,reading_id),
    KEY `idx_shared_reading_sessions_owner` (owner_id,status,updated)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='区分她的阅读游标、主人确认边界与实际送达讨论的共读记录';

CREATE TABLE IF NOT EXISTS `mlw_shared_reading_receipts` (
    id VARCHAR(191) PRIMARY KEY COMMENT '可信共读命令或成功送达回执的稳定标识',
    owner_id VARCHAR(191) NOT NULL COMMENT '实际操作或回执所属可信主人',
    session_id VARCHAR(191) NOT NULL COMMENT '关联共同阅读记录标识',
    kind VARCHAR(191) NOT NULL COMMENT '可信操作或实际送达回执类型',
    body_digest VARCHAR(191) NOT NULL COMMENT '命令或已发送正文摘要，仅供幂等与冲突核验',
    created DOUBLE NOT NULL COMMENT '操作或成功回执确认时间，Unix秒',
    KEY `idx_shared_reading_receipts_owner` (owner_id,created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin COMMENT='共同阅读边界和讨论送达的幂等记录，工具返回不算已讨论';
