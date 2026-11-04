-- MaiLivingWorld 已建表补充中文注释专用脚本（含持续自我身份）
-- 旧库升级请先执行 mysql.sql 创建缺失的身份、成长与工具表，再运行本脚本。
-- 先备份数据库并在低峰期执行。此脚本不删除、插入或更新业务行，
-- 但 ALTER TABLE MODIFY COLUMN 可能获取元数据锁并重建部分表，请勿运行在高流量服务上。
-- 仅适用于由 sql/mysql.sql 创建、字段结构一致的 MySQL 8.4 数据库；
-- 先在测试环境验证，确保与实际 SHOW CREATE TABLE 一致。
-- 如已有自定义列定义/类型/默认值，请不要直接执行，先人工校对。
USE `mai_living_world`;

-- mlw_stories: 外部资讯采集记录；仅代表抓取到公开标题与简介，不等于已完整阅读或观看
ALTER TABLE `mlw_stories`
  COMMENT = '外部资讯采集记录；仅代表抓取到公开标题与简介，不等于已完整阅读或观看',
  MODIFY COLUMN `key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '资讯内容的唯一去重键',
  MODIFY COLUMN `source` VARCHAR(150) NOT NULL COMMENT '资讯来源名称，例如 B站热门',
  MODIFY COLUMN `title` VARCHAR(512) NOT NULL COMMENT '资讯标题',
  MODIFY COLUMN `url` TEXT NOT NULL COMMENT '资讯原始链接',
  MODIFY COLUMN `summary` TEXT NOT NULL COMMENT '公开标题或简介摘要，不代表完整内容',
  MODIFY COLUMN `published_at` DOUBLE NULL COMMENT '资讯原始发布时间，Unix秒；可能为空',
  MODIFY COLUMN `first_seen` DOUBLE NOT NULL COMMENT '插件首次采集此资讯的时间，Unix秒',
  MODIFY COLUMN `memory_saved` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已成功提交到外部长期记忆，1=已保存，0=未保存';

-- mlw_attempts: 按会话统计的主动唤醒尝试，用于频率限制；不代表消息发送成功
ALTER TABLE `mlw_attempts`
  COMMENT = '按会话统计的主动唤醒尝试，用于频率限制；不代表消息发送成功',
  MODIFY COLUMN `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主动唤醒尝试自增ID',
  MODIFY COLUMN `target` VARCHAR(100) NOT NULL COMMENT '联系目标，包含私聊或群聊作用域',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '提交主动唤醒尝试的时间，Unix秒',
  MODIFY COLUMN `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '本次尝试关联的资讯键；可为空',
  MODIFY COLUMN `kind` VARCHAR(24) NOT NULL COMMENT '唤醒类型：explore=分享资讯，history=延续话题';

-- mlw_story_attempts: 记录某条资讯向某个聊天目标的分享尝试，避免重复分享
ALTER TABLE `mlw_story_attempts`
  COMMENT = '记录某条资讯向某个聊天目标的分享尝试，避免重复分享',
  MODIFY COLUMN `target` VARCHAR(100) NOT NULL COMMENT '私聊或群聊目标作用域',
  MODIFY COLUMN `story_key` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '已尝试分享的资讯唯一键',
  MODIFY COLUMN `tried_at` DOUBLE NOT NULL COMMENT '首次对该目标尝试分享此资讯的时间，Unix秒';

-- mlw_brain_runs: BrainBook 每个聊天作用域的深度及快速整理执行状态与去重哈希
ALTER TABLE `mlw_brain_runs`
  COMMENT = 'BrainBook 每个聊天作用域的深度及快速整理执行状态与去重哈希',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '记忆作用域，例如 private:用户ID 或 group:群ID',
  MODIFY COLUMN `history_hash` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '最近一次成功深度整理的聊天历史摘要哈希',
  MODIFY COLUMN `last_attempt` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次深度整理尝试时间，Unix秒',
  MODIFY COLUMN `last_success` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次深度整理成功时间，Unix秒',
  MODIFY COLUMN `cycle_count` BIGINT NOT NULL DEFAULT 0 COMMENT '此作用域累计成功深度整理次数',
  MODIFY COLUMN `quick_hash` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '最近一次成功快速整理的历史摘要哈希',
  MODIFY COLUMN `quick_last_attempt` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次快速整理尝试时间，Unix秒',
  MODIFY COLUMN `quick_last_success` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次快速整理成功时间，Unix秒';

-- mlw_brain_events: BrainBook 从可信消息中提取的情景事件；以聊天作用域隔离
ALTER TABLE `mlw_brain_events`
  COMMENT = 'BrainBook 从可信消息中提取的情景事件；以聊天作用域隔离',
  MODIFY COLUMN `id` VARCHAR(191) NOT NULL COMMENT '情景事件唯一ID',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '事件所属的私聊或群聊记忆作用域',
  MODIFY COLUMN `actor_id` VARCHAR(191) NOT NULL COMMENT '事件主体或陈述者的用户ID',
  MODIFY COLUMN `kind` VARCHAR(191) NOT NULL COMMENT '事件类别：experience经历、achievement成就、difficulty困难、preference偏好、correction纠正',
  MODIFY COLUMN `summary` TEXT NOT NULL COMMENT '经过整理的事件简短摘要，不是原始聊天记录',
  MODIFY COLUMN `importance` BIGINT NOT NULL COMMENT '事件重要程度评分，供回忆筛选使用',
  MODIFY COLUMN `happened` DOUBLE NOT NULL COMMENT '事件被描述发生的时间，Unix秒',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '事件写入数据库的时间，Unix秒';

-- mlw_brain_event_evidence: 情景事件与可信消息摘要哈希的多对多关联；用于追溯证据
ALTER TABLE `mlw_brain_event_evidence`
  COMMENT = '情景事件与可信消息摘要哈希的多对多关联；用于追溯证据',
  MODIFY COLUMN `event_id` VARCHAR(191) NOT NULL COMMENT '所关联的情景事件ID，对应 brain_events.id',
  MODIFY COLUMN `message_digest` VARCHAR(191) NOT NULL COMMENT '可信聊天消息的摘要哈希，不保存消息原文';

-- mlw_brain_facts: BrainBook 结构化事实及审核状态；同义合并和冲突审核在此体现
ALTER TABLE `mlw_brain_facts`
  COMMENT = 'BrainBook 结构化事实及审核状态；同义合并和冲突审核在此体现',
  MODIFY COLUMN `id` VARCHAR(191) NOT NULL COMMENT '结构化事实唯一ID',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '事实生效的私聊或群聊作用域',
  MODIFY COLUMN `subject_id` VARCHAR(191) NOT NULL COMMENT '事实所描述的用户或主体ID',
  MODIFY COLUMN `attribute` VARCHAR(191) NOT NULL COMMENT '事实属性名称，例如学习进度或偏好',
  MODIFY COLUMN `value` TEXT NOT NULL COMMENT '事实具体值，为模型提取结果且须有可信消息证据',
  MODIFY COLUMN `confidence` DOUBLE NOT NULL COMMENT '事实提案置信度，取值0到1',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL DEFAULT 'pending' COMMENT '事实审核状态：pending待证实、active有效、disputed争议、superseded被替代、rejected已拒绝',
  MODIFY COLUMN `seen_count` BIGINT NOT NULL DEFAULT 0 COMMENT '当前事实的独立证据累计数量',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '事实首次创建时间，Unix秒',
  MODIFY COLUMN `updated` DOUBLE NOT NULL COMMENT '事实最近更新时间，Unix秒',
  MODIFY COLUMN `valid_until` DOUBLE COMMENT '事实终止有效时间，Unix秒；为空表示未设置期限',
  MODIFY COLUMN `reviewed_count` BIGINT NOT NULL DEFAULT 0 COMMENT '被拒绝或替代时已经审核过的证据数量，防止旧证据重复计入';

-- mlw_brain_fact_evidence: 结构化事实与可信消息摘要哈希的关联，用于独立证据计数
ALTER TABLE `mlw_brain_fact_evidence`
  COMMENT = '结构化事实与可信消息摘要哈希的关联，用于独立证据计数',
  MODIFY COLUMN `fact_id` VARCHAR(191) NOT NULL COMMENT '所关联的结构化事实ID，对应 brain_facts.id',
  MODIFY COLUMN `message_digest` VARCHAR(191) NOT NULL COMMENT '可信消息摘要哈希，用于去重和审核，不是聊天原文';

-- mlw_brain_goals: BrainBook 主人目标与完成条件；仅有确证后才能完成或放弃
ALTER TABLE `mlw_brain_goals`
  COMMENT = 'BrainBook 主人目标与完成条件；仅有确证后才能完成或放弃',
  MODIFY COLUMN `id` VARCHAR(191) NOT NULL COMMENT '目标唯一ID',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '目标所属的聊天记忆作用域',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '目标发起人或负责人用户ID',
  MODIFY COLUMN `title` TEXT NOT NULL COMMENT '目标简短名称，例如整理数据线',
  MODIFY COLUMN `criterion` TEXT NOT NULL COMMENT '目标完成的明确条件，结案时逐项核验',
  MODIFY COLUMN `next_action` VARCHAR(191) NOT NULL DEFAULT 'wait' COMMENT '下一步内部建议：wait、follow_up、compare_evidence、review；不直接执行外部动作',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL DEFAULT 'open' COMMENT '目标状态：open待完成、done已完成、abandoned已放弃；阶段进展另记于 brain_goal_events',
  MODIFY COLUMN `revision` BIGINT NOT NULL DEFAULT 1 COMMENT '目标修改版本号，便于追踪状态变化',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '目标创建时间，Unix秒',
  MODIFY COLUMN `updated` DOUBLE NOT NULL COMMENT '目标最近更新时间，Unix秒',
  MODIFY COLUMN `closed_at` DOUBLE COMMENT '目标结案时间，Unix秒；未完成时为空';

-- mlw_brain_goal_events: 目标证据与阶段时间线，记录创建、进行中、完成或放弃的依据
ALTER TABLE `mlw_brain_goal_events`
  COMMENT = '目标证据与阶段时间线，记录创建、进行中、完成或放弃的依据',
  MODIFY COLUMN `goal_id` VARCHAR(191) NOT NULL COMMENT '关联的目标ID，对应 brain_goals.id',
  MODIFY COLUMN `message_digest` VARCHAR(191) NOT NULL COMMENT '目标阶段所依据的可信消息摘要哈希',
  MODIFY COLUMN `kind` VARCHAR(191) NOT NULL COMMENT '阶段标记：origin创建、in_progress进行中、done完成、abandoned放弃',
  MODIFY COLUMN `recorded` DOUBLE NOT NULL COMMENT '阶段证据记录时间，Unix秒';

-- mlw_brain_decisions: BrainBook 主动联系决策及发送回执与主人反馈关联记录
ALTER TABLE `mlw_brain_decisions`
  COMMENT = 'BrainBook 主动联系决策及发送回执与主人反馈关联记录',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '决策记录自增ID',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '此决策所属聊天作用域',
  MODIFY COLUMN `goal_id` VARCHAR(191) COMMENT '相关目标ID；未关联具体目标时为空',
  MODIFY COLUMN `feedback_event_id` BIGINT COMMENT '主动发送反馈事件ID；唯一且允许为空',
  MODIFY COLUMN `action` VARCHAR(191) NOT NULL COMMENT '本次计划采取的内部行动类别',
  MODIFY COLUMN `reason` TEXT NOT NULL COMMENT '采取该行动的原因摘要',
  MODIFY COLUMN `outcome` VARCHAR(191) NOT NULL DEFAULT 'submitted' COMMENT '执行结果状态，区别仅提交、确认发送和失败',
  MODIFY COLUMN `user_feedback` VARCHAR(191) NOT NULL DEFAULT 'unrated' COMMENT '主人对该次主动联系的反馈，默认 unrated未评价',
  MODIFY COLUMN `recorded` DOUBLE NOT NULL COMMENT '决策记录时间，Unix秒',
  MODIFY COLUMN `finished` DOUBLE COMMENT '决策得到最终结果的时间，Unix秒；未结束时为空';

-- mlw_brain_cycles: BrainBook 整理周期审计和简短元认知反思；反思需经后续证据核实
ALTER TABLE `mlw_brain_cycles`
  COMMENT = 'BrainBook 整理周期审计和简短元认知反思；反思需经后续证据核实',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '整理周期自增ID',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '整理所针对的聊天作用域',
  MODIFY COLUMN `source_count` BIGINT NOT NULL COMMENT '本周期参与处理的可信消息数量',
  MODIFY COLUMN `added_events` BIGINT NOT NULL COMMENT '本周期新增情景事件数量',
  MODIFY COLUMN `added_facts` BIGINT NOT NULL COMMENT '本周期新增或处理结构化事实数量',
  MODIFY COLUMN `updated_goals` BIGINT NOT NULL COMMENT '本周期新增或更新目标数量',
  MODIFY COLUMN `reflection` TEXT NOT NULL COMMENT '模型产生的简短复盘文本，仅作为待核实线索',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '整理周期完成或记录时间，Unix秒',
  MODIFY COLUMN `cycle_kind` VARCHAR(191) NOT NULL DEFAULT 'deep' COMMENT '整理类型：deep深度周期、quick快速周期';

-- mlw_brain_fact_revisions: 结构化事实的审核状态变更审计，记录前后状态及原因
ALTER TABLE `mlw_brain_fact_revisions`
  COMMENT = '结构化事实的审核状态变更审计，记录前后状态及原因',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '事实状态变更自增ID',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '状态变更所属聊天作用域',
  MODIFY COLUMN `fact_id` VARCHAR(191) NOT NULL COMMENT '被审核或修改的事实ID',
  MODIFY COLUMN `before_status` VARCHAR(191) NOT NULL COMMENT '修改前事实审核状态',
  MODIFY COLUMN `after_status` VARCHAR(191) NOT NULL COMMENT '修改后事实审核状态',
  MODIFY COLUMN `reason` TEXT NOT NULL COMMENT '审核或状态变更原因',
  MODIFY COLUMN `changed` DOUBLE NOT NULL COMMENT '状态变更发生时间，Unix秒';

-- mlw_mind_runs: Mind 主人观察模块的反思周期运行状态与行为目标提示
ALTER TABLE `mlw_mind_runs`
  COMMENT = 'Mind 主人观察模块的反思周期运行状态与行为目标提示',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '被观察的主人用户ID',
  MODIFY COLUMN `last_hash` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '上次成功处理的对话窗口摘要哈希',
  MODIFY COLUMN `last_attempt_at` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次反思尝试时间，Unix秒',
  MODIFY COLUMN `last_success_at` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次反思成功时间，Unix秒',
  MODIFY COLUMN `behavior_goal` VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '从过去反思得到的短期行为目标提示，可为空',
  MODIFY COLUMN `reflection_count` BIGINT NOT NULL DEFAULT 0 COMMENT '已成功完成的反思累计次数';

-- mlw_mind_notes: Mind 对主人偏好、共同经历、边界及未完话题的结构化笔记
ALTER TABLE `mlw_mind_notes`
  COMMENT = 'Mind 对主人偏好、共同经历、边界及未完话题的结构化笔记',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '该笔记所属主人用户ID',
  MODIFY COLUMN `note_key` VARCHAR(191) NOT NULL COMMENT '笔记内容归一化哈希键，与 owner_id 组成联合主键',
  MODIFY COLUMN `kind` VARCHAR(191) NOT NULL COMMENT '笔记类别：preference偏好、shared_experience共同经历、boundary边界、open_topic未完话题',
  MODIFY COLUMN `text` TEXT NOT NULL COMMENT '笔记内容摘要，不是原始聊天全文',
  MODIFY COLUMN `basis` TEXT NOT NULL COMMENT '证据依据类别：user_explicit主人明确表达、conversation对话情境',
  MODIFY COLUMN `confidence` DOUBLE NOT NULL COMMENT '笔记置信度，取值0到1',
  MODIFY COLUMN `seen_count` BIGINT NOT NULL DEFAULT 1 COMMENT '被独立证据支持的累计次数',
  MODIFY COLUMN `first_seen` DOUBLE NOT NULL COMMENT '首次记录此笔记的时间，Unix秒',
  MODIFY COLUMN `last_seen` DOUBLE NOT NULL COMMENT '最近一次证据支持时间，Unix秒',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL DEFAULT 'legacy' COMMENT '笔记状态：legacy旧版待核实、pending待确认、active有效、disputed有争议、rejected已拒绝';

-- mlw_mind_evidence: Mind 笔记与聊天证据的关联；按主人、笔记和证据ID去重
ALTER TABLE `mlw_mind_evidence`
  COMMENT = 'Mind 笔记与聊天证据的关联；按主人、笔记和证据ID去重',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '该证据关联的主人用户ID',
  MODIFY COLUMN `note_key` VARCHAR(191) NOT NULL COMMENT '所支持的 Mind 笔记键',
  MODIFY COLUMN `evidence_id` VARCHAR(191) NOT NULL COMMENT '当前整理窗口内的证据编号',
  MODIFY COLUMN `message_digest` VARCHAR(191) NOT NULL COMMENT '可信消息内容摘要哈希，不保存原文',
  MODIFY COLUMN `role` VARCHAR(191) NOT NULL COMMENT '证据发言者类别，例如 owner主人或 bot机器人';

-- mlw_mind_reviews: Mind 冲突纠正审核请求及人工审核决定
ALTER TABLE `mlw_mind_reviews`
  COMMENT = 'Mind 冲突纠正审核请求及人工审核决定',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '纠正审核记录自增ID',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '待审核笔记所属主人用户ID',
  MODIFY COLUMN `target_key` VARCHAR(191) NOT NULL COMMENT '需要审核或纠正的 Mind 笔记键',
  MODIFY COLUMN `reason` TEXT NOT NULL COMMENT '提出冲突或纠正的理由',
  MODIFY COLUMN `evidence_ids` TEXT NOT NULL COMMENT '关联证据编号列表的序列化文本',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL DEFAULT 'pending' COMMENT '审核流程状态：pending待审核、approve同意、reject驳回等',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '审核请求创建时间，Unix秒',
  MODIFY COLUMN `previous_status` VARCHAR(191) NOT NULL DEFAULT 'active' COMMENT '发起审核前笔记的状态，供拒绝纠正时恢复';

-- mlw_mind_snapshots: Mind 笔记与证据的结构化历史快照，用于审核回滚
ALTER TABLE `mlw_mind_snapshots`
  COMMENT = 'Mind 笔记与证据的结构化历史快照，用于审核回滚',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '历史快照自增ID',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '该快照所属主人用户ID',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '快照生成时间，Unix秒',
  MODIFY COLUMN `reason` TEXT NOT NULL COMMENT '生成快照的原因，如反思或审核',
  MODIFY COLUMN `payload` LONGTEXT NOT NULL COMMENT '结构化笔记及证据的JSON快照，不包含原始聊天全文';

-- mlw_cognitive_cycles: 公开认知模块每次观察整理的执行状态和新增更新统计
ALTER TABLE `mlw_cognitive_cycles`
  COMMENT = '公开认知模块每次观察整理的执行状态和新增更新统计',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '认知周期自增ID',
  MODIFY COLUMN `attempted_at` DOUBLE NOT NULL COMMENT '开始尝试本次公开认知处理的时间，Unix秒',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL COMMENT '周期执行状态，如 started、completed 或 failed',
  MODIFY COLUMN `input_count` BIGINT NOT NULL DEFAULT 0 COMMENT '输入的公开资讯来源数量',
  MODIFY COLUMN `added` BIGINT NOT NULL DEFAULT 0 COMMENT '本次新增的公开认知目标数量',
  MODIFY COLUMN `updated` BIGINT NOT NULL DEFAULT 0 COMMENT '本次更新的既有公开认知目标数量';

-- mlw_cognitive_sources: 公开认知已处理的来源键，用于来源级去重
ALTER TABLE `mlw_cognitive_sources`
  COMMENT = '公开认知已处理的来源键，用于来源级去重',
  MODIFY COLUMN `source_key` VARCHAR(191) NOT NULL COMMENT '公开资讯来源的唯一键',
  MODIFY COLUMN `seen_at` DOUBLE NOT NULL COMMENT '首次处理该来源的时间，Unix秒';

-- mlw_cognitive_goals: 基于公开来源形成的待研究问题与观察目标，不等于私人任务
ALTER TABLE `mlw_cognitive_goals`
  COMMENT = '基于公开来源形成的待研究问题与观察目标，不等于私人任务',
  MODIFY COLUMN `goal_id` VARCHAR(191) NOT NULL COMMENT '公开认知问题目标唯一ID',
  MODIFY COLUMN `topic` TEXT NOT NULL COMMENT '认知主题，例如开源模型',
  MODIFY COLUMN `question` TEXT NOT NULL COMMENT '仍待研究或核实的具体问题',
  MODIFY COLUMN `observation` TEXT NOT NULL COMMENT '目前已从公开来源观察到的内容',
  MODIFY COLUMN `uncertainty` TEXT NOT NULL COMMENT '已知的不确定性和仍缺少的证据',
  MODIFY COLUMN `next_action` VARCHAR(191) NOT NULL COMMENT '下一步内部建议：wait_for_update、compare_public_sources、ask_when_relevant',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL DEFAULT 'open' COMMENT '认知问题状态，如 open待跟进、resolved已解决、discarded不再跟进',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '创建认知问题的时间，Unix秒',
  MODIFY COLUMN `updated_at` DOUBLE NOT NULL COMMENT '最近一次观察或状态变更时间，Unix秒',
  MODIFY COLUMN `revision` BIGINT NOT NULL DEFAULT 1 COMMENT '认知问题版本号，更新时递增';

-- mlw_cognitive_evidence: 公开认知问题与资讯来源的对应证据；保留标题和来源链接
ALTER TABLE `mlw_cognitive_evidence`
  COMMENT = '公开认知问题与资讯来源的对应证据；保留标题和来源链接',
  MODIFY COLUMN `goal_id` VARCHAR(191) NOT NULL COMMENT '所关联的公开认知问题ID',
  MODIFY COLUMN `source_key` VARCHAR(191) NOT NULL COMMENT '来源唯一键，与 goal_id 联合去重',
  MODIFY COLUMN `source` TEXT NOT NULL COMMENT '来源名称，例如 B站热门',
  MODIFY COLUMN `title` TEXT NOT NULL COMMENT '来源标题',
  MODIFY COLUMN `url` TEXT NOT NULL COMMENT '来源网页或视频链接',
  MODIFY COLUMN `observed_at` DOUBLE NOT NULL COMMENT '本次记录来源证据的时间，Unix秒';

-- mlw_cognitive_events: 公开认知问题的创建、更新与状态变化时间线
ALTER TABLE `mlw_cognitive_events`
  COMMENT = '公开认知问题的创建、更新与状态变化时间线',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '认知事件自增ID',
  MODIFY COLUMN `goal_id` VARCHAR(191) NOT NULL COMMENT '关联的公开认知问题ID',
  MODIFY COLUMN `kind` VARCHAR(191) NOT NULL COMMENT '认知事件类型，如创建、更新或审核状态变化',
  MODIFY COLUMN `note` TEXT NOT NULL COMMENT '本次变化的简短说明',
  MODIFY COLUMN `source_key` VARCHAR(191) COMMENT '关联的公开资讯来源键；可为空',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '事件记录时间，Unix秒';

-- mlw_experience_actions: 主动联系后的经验闭环行动，记录计划、发送状态和反馈结果
ALTER TABLE `mlw_experience_actions`
  COMMENT = '主动联系后的经验闭环行动，记录计划、发送状态和反馈结果',
  MODIFY COLUMN `event_id` BIGINT NOT NULL COMMENT '关联主动发送反馈模块的事件ID，兼任主键',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '此次尝试针对的主人用户ID',
  MODIFY COLUMN `session_id` VARCHAR(191) NOT NULL COMMENT '对应的真实聊天会话ID',
  MODIFY COLUMN `kind` VARCHAR(191) NOT NULL CHECK(kind IN ('explore','history')) COMMENT '行动类型：explore资讯分享、history旧话题延续',
  MODIFY COLUMN `topic_digest` VARCHAR(191) NOT NULL COMMENT '话题摘要哈希，避免记录敏感原文',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '行动创建时间，Unix秒',
  MODIFY COLUMN `sent_at` DOUBLE COMMENT '平台确认发送成功的时间，Unix秒；未成功则为空',
  MODIFY COLUMN `outcome` VARCHAR(191) NOT NULL DEFAULT 'submitted' COMMENT '行动状态，如 submitted、reply_planned、send_confirmed、send_failed 等',
  MODIFY COLUMN `feedback` VARCHAR(191) NOT NULL DEFAULT 'unrated' COMMENT '接收的用户反馈，如 unrated、positive、negative、reply_only、opt_out',
  MODIFY COLUMN `feedback_at` DOUBLE COMMENT '最近一次可信用户反馈时间，Unix秒；尚无反馈时为空';

-- mlw_experience_evidence: 经验反馈的去重证据与解析出的反馈信号，不保存消息原文
ALTER TABLE `mlw_experience_evidence`
  COMMENT = '经验反馈的去重证据与解析出的反馈信号，不保存消息原文',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '反馈证据自增ID',
  MODIFY COLUMN `event_id` BIGINT NOT NULL COMMENT '对应的经验行动事件ID',
  MODIFY COLUMN `evidence_digest` VARCHAR(191) NOT NULL COMMENT '反馈消息摘要哈希，防止重复统计',
  MODIFY COLUMN `signal` VARCHAR(191) NOT NULL COMMENT '从反馈文本识别的信号，例如 positive、negative 或 opt_out',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '证据被记录的时间，Unix秒';

-- mlw_experience_policy: 根据明确负面反馈形成的主动联系冷却限制
ALTER TABLE `mlw_experience_policy`
  COMMENT = '根据明确负面反馈形成的主动联系冷却限制',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '受该冷却策略约束的主人用户ID',
  MODIFY COLUMN `kind` VARCHAR(191) NOT NULL COMMENT '受限行动类型：explore或history',
  MODIFY COLUMN `avoid_until` DOUBLE NOT NULL DEFAULT 0 COMMENT '此类主动行动禁止发送的截止时间，Unix秒；0表示无冷却';

-- mlw_experience_opt_out: 主人明确拒绝主动联系后的持久化停止标记
ALTER TABLE `mlw_experience_opt_out`
  COMMENT = '主人明确拒绝主动联系后的持久化停止标记',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '明确要求停止主动联系的主人用户ID',
  MODIFY COLUMN `paused_at` DOUBLE NOT NULL COMMENT '接收到停止主动联系要求的时间，Unix秒';

-- mlw_proactive_targets: 按聊天目标维护主动发送停止次数、退避时间与最后成功时间
ALTER TABLE `mlw_proactive_targets`
  COMMENT = '按聊天目标维护主动发送停止次数、退避时间与最后成功时间',
  MODIFY COLUMN `target` VARCHAR(191) NOT NULL COMMENT '主动联系目标的私聊或群聊作用域',
  MODIFY COLUMN `stop_streak` BIGINT NOT NULL DEFAULT 0 COMMENT '连续被规划器停止的次数，用于退避',
  MODIFY COLUMN `backoff_until` DOUBLE NOT NULL DEFAULT 0 COMMENT '下一次可重新尝试的时间下限，Unix秒',
  MODIFY COLUMN `last_sent_at` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次确认发送成功的时间，Unix秒';

-- mlw_proactive_events: 主动唤醒与回复规划器回执事件流水，区分计划和真实发送
ALTER TABLE `mlw_proactive_events`
  COMMENT = '主动唤醒与回复规划器回执事件流水，区分计划和真实发送',
  MODIFY COLUMN `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主动发送反馈事件自增ID',
  MODIFY COLUMN `target` VARCHAR(191) NOT NULL COMMENT '本次事件关联的私聊或群聊目标',
  MODIFY COLUMN `session_id` VARCHAR(191) NOT NULL COMMENT '对应的会话ID，用于核对发送回执',
  MODIFY COLUMN `created_at` DOUBLE NOT NULL COMMENT '事件创建时间，Unix秒',
  MODIFY COLUMN `updated_at` DOUBLE NOT NULL COMMENT '事件结果最近更新时间，Unix秒',
  MODIFY COLUMN `outcome` VARCHAR(191) NOT NULL COMMENT '发送阶段或结果，如 submitted、planner_stop、reply_planned、send_confirmed、send_failed、unknown';

-- mlw_self_identity: 智能体身份与唯一主人绑定
ALTER TABLE `mlw_self_identity`
  COMMENT = '同一个智能体的稳定身份与唯一主人绑定，不按聊天会话分裂',
  MODIFY COLUMN `slot` BIGINT NOT NULL COMMENT '单例位置，恒为1；一个数据库命名空间只有一个智能体',
  MODIFY COLUMN `agent_id` VARCHAR(191) NOT NULL COMMENT '持久智能体UUID，跨重启和聊天窗口保持不变',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '管理配置绑定的唯一主人平台用户ID，空值表示尚未绑定',
  MODIFY COLUMN `owner_platform` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '主人的可信身份平台，当前为qq',
  MODIFY COLUMN `revision` BIGINT NOT NULL DEFAULT 0 COMMENT '主人绑定修订号，首次绑定及显式更换时递增',
  MODIFY COLUMN `budget_day` DOUBLE NOT NULL DEFAULT 0 COMMENT '思考预算当天起始时间，Unix秒',
  MODIFY COLUMN `thought_count` BIGINT NOT NULL DEFAULT 0 COMMENT '当天已预留的思考次数，失败调用也计数',
  MODIFY COLUMN `last_thought` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近一次预留思考时间，Unix秒，用于跨重启冷却',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '身份创建时间，Unix秒',
  MODIFY COLUMN `updated` DOUBLE NOT NULL COMMENT '最近一次主人绑定变更时间，Unix秒';

-- mlw_self_owner_history: 主人绑定配置审计
ALTER TABLE `mlw_self_owner_history`
  COMMENT = '唯一主人配置的修订审计，仅供管理端读取，不写入公开模型提示',
  MODIFY COLUMN `revision` BIGINT NOT NULL COMMENT '对应主人绑定修订号，防止同一版本重复记录',
  MODIFY COLUMN `agent_id` VARCHAR(191) NOT NULL COMMENT '发生绑定变更的持久智能体UUID',
  MODIFY COLUMN `previous_owner_id` VARCHAR(191) NOT NULL COMMENT '变更前的主人平台用户ID，首次绑定为空',
  MODIFY COLUMN `owner_id` VARCHAR(191) NOT NULL COMMENT '显式配置的新主人平台用户ID',
  MODIFY COLUMN `owner_platform` VARCHAR(191) NOT NULL COMMENT '绑定的平台，当前为qq',
  MODIFY COLUMN `changed` DOUBLE NOT NULL COMMENT '管理配置生效时间，Unix秒';

ALTER TABLE `mlw_growth_state`
  COMMENT = '持续成长的当前结构化档案，读取时计算情绪衰减且不改写原始状态',
  MODIFY COLUMN `slot` BIGINT NOT NULL COMMENT '单例位置，恒为1，对应当前智能体',
  MODIFY COLUMN `payload` LONGTEXT NOT NULL COMMENT '有界兴趣观点知识问题性格情绪及变化额度的JSON状态',
  MODIFY COLUMN `revision` BIGINT NOT NULL DEFAULT 0 COMMENT '成长状态版本，成长回滚和显式重建时递增',
  MODIFY COLUMN `updated` DOUBLE NOT NULL COMMENT '最近一次持久成长变更时间，Unix秒';

ALTER TABLE `mlw_growth_jobs`
  COMMENT = '跨重启恢复的学习任务队列，采用租约和有界失败退避',
  MODIFY COLUMN `id` VARCHAR(191) NOT NULL COMMENT '来源指纹及观察模式的稳定任务哈希，防止重复排队',
  MODIFY COLUMN `payload` LONGTEXT NOT NULL COMMENT '来源ID指纹场景及保密属性的JSON引用，不存聊天原文',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL COMMENT '任务状态：pending、running、done或failed',
  MODIFY COLUMN `mode` VARCHAR(191) NOT NULL COMMENT '任务创建时模式：observe仅观察或active可应用',
  MODIFY COLUMN `attempts` BIGINT NOT NULL DEFAULT 0 COMMENT '已消费的实际模型失败次数，达到三次停止',
  MODIFY COLUMN `lease_token` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '当前领取者的随机租约令牌',
  MODIFY COLUMN `lease_until` DOUBLE NOT NULL DEFAULT 0 COMMENT '租约过期时间，Unix秒，过期可重新领取',
  MODIFY COLUMN `next_attempt` DOUBLE NOT NULL DEFAULT 0 COMMENT '下次允许重试的时间，Unix秒',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '首次排队时间，Unix秒',
  MODIFY COLUMN `updated` DOUBLE NOT NULL COMMENT '最近任务状态变化时间，Unix秒',
  MODIFY COLUMN `error_code` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '固定错误码，不保存模型或聊天原文';

ALTER TABLE `mlw_growth_history`
  COMMENT = '可追溯的成长与管理操作历史，回滚保留源记忆和消费记录',
  MODIFY COLUMN `revision` BIGINT NOT NULL COMMENT '成长变更对应的全局版本号',
  MODIFY COLUMN `job_id` VARCHAR(191) NOT NULL COMMENT '已提交任务或管理操作的唯一标识',
  MODIFY COLUMN `payload` LONGTEXT NOT NULL COMMENT '变更前后状态及来源依赖的JSON审计记录',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '变更完成时间，Unix秒',
  MODIFY COLUMN `rolled_back` BIGINT NOT NULL DEFAULT 0 COMMENT '是否已回滚或属于回滚记录，1表示是';

ALTER TABLE `mlw_growth_consumed`
  COMMENT = '经历消费去重账本，回滚及重建不会补充已消费的成长次数',
  MODIFY COLUMN `source_id` VARCHAR(191) NOT NULL COMMENT '已用于兴趣性格或情绪变化的原始记录ID，改版也不重复累计',
  MODIFY COLUMN `job_id` VARCHAR(191) NOT NULL COMMENT '首次消费该记录的成长任务ID',
  MODIFY COLUMN `consumed` DOUBLE NOT NULL COMMENT '首次应用变化的时间，Unix秒';

ALTER TABLE `mlw_tool_observations`
  COMMENT = '受信工具的原始观察记忆，更新或撤回会使旧派生结论失效',
  MODIFY COLUMN `id` VARCHAR(191) NOT NULL COMMENT '提供者与规范资源键的稳定观察ID',
  MODIFY COLUMN `provider` VARCHAR(191) NOT NULL COMMENT '受信工具提供者标识',
  MODIFY COLUMN `resource_key` VARCHAR(191) NOT NULL COMMENT '提供者内的规范资源键，不含凭据',
  MODIFY COLUMN `text` TEXT NOT NULL COMMENT '最多600字符的工具原始元数据观察，不是模型总结',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '来源场合，跨场合保守合并且不用于内部检索隔离',
  MODIFY COLUMN `subject_id` VARCHAR(191) NOT NULL COMMENT '可信来源关联用户，混合来源时可为空',
  MODIFY COLUMN `sensitivity` VARCHAR(191) NOT NULL COMMENT '保守合并的保密级别，不能由重复公开读取降低',
  MODIFY COLUMN `url` TEXT NOT NULL COMMENT '可选公开来源链接，禁止凭据及查询参数',
  MODIFY COLUMN `origin_key` VARCHAR(191) NOT NULL COMMENT '提供者和原始资源的规范哈希，用于独立证据去重',
  MODIFY COLUMN `timestamp` DOUBLE NOT NULL COMMENT '内容或约束实际变化时间，重复读取不更新',
  MODIFY COLUMN `active` BIGINT NOT NULL DEFAULT 1 COMMENT '1为可用观察，0为已撤回',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '首次观察时间，Unix秒';

ALTER TABLE `mlw_tool_calls`
  COMMENT = '跨重启幂等调用及全局工具预算，独立于模型调用预算',
  MODIFY COLUMN `id` VARCHAR(191) NOT NULL COMMENT '提供者工具参数摘要与可信消息上下文的幂等调用ID',
  MODIFY COLUMN `provider` VARCHAR(191) NOT NULL COMMENT '工具提供者标识',
  MODIFY COLUMN `tool` VARCHAR(191) NOT NULL COMMENT '已批准的固定工具名称',
  MODIFY COLUMN `args_digest` VARCHAR(191) NOT NULL COMMENT '调用参数SHA256摘要，不保存参数原文',
  MODIFY COLUMN `context_digest` VARCHAR(191) NOT NULL COMMENT '可信消息ID场合和主体的摘要，不保存原请求键',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL COMMENT 'reserved、succeeded、failed或uncertain，未知结果禁止重放',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '调用发起场合，用于授权追溯',
  MODIFY COLUMN `subject_id` VARCHAR(191) NOT NULL COMMENT '可信调用发起主体',
  MODIFY COLUMN `budget_day` DOUBLE NOT NULL COMMENT '独立全局工具日预算起点，失败和未知调用也计数',
  MODIFY COLUMN `payload` LONGTEXT NOT NULL COMMENT '适配器生成的有界结果状态摘要，不保存密钥或原始响应',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '原子预留调用时间，Unix秒',
  MODIFY COLUMN `updated` DOUBLE NOT NULL COMMENT '调用状态最近变化时间，Unix秒';

ALTER TABLE `mlw_tool_tasks`
  COMMENT = '后台工具任务的主体场合绑定，不保存路径或签名下载地址',
  MODIFY COLUMN `id` VARCHAR(191) NOT NULL COMMENT '提供者与任务ID的稳定哈希',
  MODIFY COLUMN `provider` VARCHAR(191) NOT NULL COMMENT '任务所属工具提供者',
  MODIFY COLUMN `task_id` VARCHAR(191) NOT NULL COMMENT '提供者返回的任务ID，不是URL或文件路径',
  MODIFY COLUMN `scope` VARCHAR(191) NOT NULL COMMENT '任务首次创建的可信场合，禁止重新绑定',
  MODIFY COLUMN `subject_id` VARCHAR(191) NOT NULL COMMENT '任务首次创建的可信主体，禁止重新绑定',
  MODIFY COLUMN `operation` VARCHAR(191) NOT NULL COMMENT '原始工具操作名，查询或重试时重新检查权限',
  MODIFY COLUMN `created` DOUBLE NOT NULL COMMENT '首次绑定时间，Unix秒',
  MODIFY COLUMN `updated` DOUBLE NOT NULL COMMENT '绑定记录时间，Unix秒';

ALTER TABLE `mlw_tool_research`
  COMMENT = '跨重启研究轮转租约退避及独立预算，本地检索轮转不消耗网络额度',
  MODIFY COLUMN `question_key` VARCHAR(191) NOT NULL COMMENT '待研究问题键或内部控制行标识，不保存问题正文',
  MODIFY COLUMN `status` VARCHAR(191) NOT NULL DEFAULT 'idle' COMMENT 'idle、running、succeeded或failed',
  MODIFY COLUMN `attempts` BIGINT NOT NULL DEFAULT 0 COMMENT '研究领取次数，控制行记录全局次数',
  MODIFY COLUMN `failures` BIGINT NOT NULL DEFAULT 0 COMMENT '连续失败次数，用于指数退避',
  MODIFY COLUMN `last_attempt` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近领取时间，重启保留冷却和轮转顺序',
  MODIFY COLUMN `next_attempt` DOUBLE NOT NULL DEFAULT 0 COMMENT '允许下次研究该问题的时间，Unix秒',
  MODIFY COLUMN `last_finish` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近完成时间，Unix秒',
  MODIFY COLUMN `lease_token` VARCHAR(191) NOT NULL DEFAULT '' COMMENT '研究租约令牌，本地轮转控制行保存上一问题键',
  MODIFY COLUMN `lease_until` DOUBLE NOT NULL DEFAULT 0 COMMENT '研究租约截止时间，过期允许恢复',
  MODIFY COLUMN `interval_seconds` BIGINT NOT NULL DEFAULT 0 COMMENT '领取时的研究冷却秒数',
  MODIFY COLUMN `budget_day` DOUBLE NOT NULL DEFAULT 0 COMMENT '控制行的独立研究日预算起点',
  MODIFY COLUMN `daily_count` BIGINT NOT NULL DEFAULT 0 COMMENT '控制行的当日研究预留次数，失败也计数',
  MODIFY COLUMN `updated` DOUBLE NOT NULL DEFAULT 0 COMMENT '最近调度状态变化时间，Unix秒';
