# 成长面板与状态备份

## 成长面板

启用插件后，在支持 WebUI v2 `custom_pages` 的 MaiBot Host 与 Dashboard 中打开「成长手记」。页面使用管理员登录态调用 `living_world_dashboard` v1；不启动额外 HTTP 服务，不向页面提供数据库、平台凭据或来源原文。`public=False` 阻止跨插件调用，访问认证由 Host 的管理页面与 API 路由负责。

面板展示当前兴趣、观点、知识候选、未解问题、性格与情绪、四种调用额度，以及当前主人的共同计划和阅读进度。「我们的相处方式」显示主人明确设置的称呼、亲昵表达偏好和边界；「留下的回忆与纪念日」只显示当前主人私聊明确记录、尚未撤回的事项，区分主人记录与纪念日，不提供好感分或关系等级。刷新后读取真实状态，搜索框筛选已经过滤的摘要。主人标识只显示「已绑定」，智能体 ID 显示不可逆短标记。管理员也不会看到第三方私人来源的原文；有私人或已失效依赖的条目显示保护或失效占位。来源删除、改版或过期时暂停展示结论。兴趣、观点与知识候选属于推断，不是已经证实的事实。

WebUI v2 要求 Host 与 Dashboard 配套支持；只升级 Python SDK 不足以启用页面。兼容旧宿主的方式是从已认证的管理员 API 导出 `living_world_dashboard` 响应 JSON，再生成可直接打开、可搜索的独立 HTML：

```sh
python dashboard.py --input dashboard-export.json --output growth-snapshot.html
```

导出只接受面板格式，重新筛选允许字段，不会把额外字段或被保护的内容嵌进 HTML。目标已存在则拒绝覆盖。HTML 不加载网络资源，离线状态不会伪装成实时面板。导出的主人计划和可展示摘要仍属于个人数据，请自行保管。

## 备份范围与一致性

`scripts/backup_state.py` 支持 SQLite 的十二个固定账本和 MySQL 同结构专用库的 51 张表：

| SQLite 文件 | 内容 |
| --- | --- |
| `living_world.sqlite3` | 资讯、尝试和去重 |
| `brain_book.sqlite3` | 事实、事件、目标与证据 |
| `mind.sqlite3` | 长期笔记与反思 |
| `cognition.sqlite3` | 观察与认知目标 |
| `experience_loop.sqlite3` | 经历与行动策略 |
| `proactive_feedback.sqlite3` | 主动联系反馈 |
| `self_state.sqlite3` | 自我 ID、主人绑定与模型额度 |
| `growth.sqlite3` | 兴趣、性格、观点、问题、审计与任务 |
| `tool_memory.sqlite3` | 工具观察、调用、任务归属与研究额度 |
| `companion.sqlite3` | 阅读、已读单元、共同计划、审计与阅读额度 |
| `bond.sqlite3` | 主人相处约定、明确记录的回忆与纪念日、撤回和命令审计 |
| `continuity.sqlite3` | 主人聊天中待续的话题、明确忙碌或休息状态、提取去重与审计 |

备份仅包含这些状态账本。插件配置、环境变量、cap 网关缓存卷、PDF 文件、Pixiv 后端数据库和图片库需要分别备份。来源文件丢失或版本变化后，即使恢复了阅读进度，相关来源仍须重新校验；恢复工具不会伪造原始内容。

**所有实际备份和恢复均要求停止写入者。** 对 Docker 部署，先停止所有挂载同一插件状态目录或连接同一插件数据库的 MaiBot 实例，以及迁移或维护任务。其他后台进程如果会直接写这些账本，也必须停止。只关闭成长开关不等于停写，消息采集和调用预算仍可能更新。

SQLite 使用数据库 backup API，包括已经提交到 WAL 的内容；十二库之间的一致性来自停写前提。MySQL 使用 InnoDB 的一致性只读事务快照；仍要求停写，避免 DDL、表清单和自增计数器在备份期间变化。`--writers-stopped` 是操作员声明，工具不能自动证明所有外部进程都已停写。不要把仍在写入的多个 SQLite 文件直接打包后当作一致备份。

归档内有固定文件清单、大小、SHA-256、行数及结构元数据。恢复前检查所有文件，拒绝额外文件、重复名称、路径穿越、符号链接、过大文件和哈希不符。SQLite 拒绝非预期表、视图与触发器。MySQL 要求专用 InnoDB schema，拒绝额外业务表或触发器；不会执行归档提供的 SQL。

这些哈希用于发现损坏，不是数字签名。归档保存真实长期状态，**没有加密**，可能包含聊天和私人经历。使用受限目录及你已有的加密备份设施保存；只恢复可信来源的归档。

## SQLite 备份与恢复

以下目录均需替换成实际路径。容器停止后，可在宿主机或只挂载相应卷的临时维护容器中运行。归档放在状态目录之外；命令默认 dry-run，不创建归档或恢复目录。

```sh
# 预检：不创建归档
python scripts/backup_state.py backup-sqlite --source /srv/maibot/plugin-state --archive /srv/backups/mlw-state.zip

# 停写后执行；已有同名归档不会被覆盖
python scripts/backup_state.py backup-sqlite --source /srv/maibot/plugin-state --archive /srv/backups/mlw-state.zip --apply --writers-stopped

# 校验归档，不触碰运行目录
python scripts/backup_state.py verify --archive /srv/backups/mlw-state.zip

# 先演练恢复预检，目标必须不存在或为空
python scripts/backup_state.py restore-sqlite --archive /srv/backups/mlw-state.zip --target /srv/restore-check/mlw-state

# 真正恢复到空目录
python scripts/backup_state.py restore-sqlite --archive /srv/backups/mlw-state.zip --target /srv/restore-check/mlw-state --apply --writers-stopped --confirm RESTORE_EMPTY_TARGET
```

缺省库会明确列在输出 `missing` 中，不会静默补建新身份。检查清单是否符合你的运行版本；若应存在的库缺失，先找到正确的源目录。目标目录有任何文件就拒绝恢复，绝不递归删除目标。数据先恢复到同文件系统的临时目录，全部成功后再移入空目标。

新归档使用 `mailivingworld.state-backup.v2` 格式和明确的 `catalog: v0.16` 清单。旧 v0.14 的 v1 归档固定按原十库清单校验，v0.15 的 v2 归档固定按原十一库清单校验。恢复 v0.14 时另外报告 `bond.sqlite3` 和 `continuity.sqlite3` 未提供，恢复 v0.15 时报告 `continuity.sqlite3` 未提供，不凭空生成约定、回忆或待续话题。每种清单都要求归档文件与明确的缺失列表完整且不重叠，不能用旧格式夹带新库、未知表或隐瞒漏项。请使用支持对应清单的新脚本校验和恢复。

恢复后先在隔离环境检查面板中的主人绑定、成长修订、计划、阅读进度和额度，再让停止的实例指向已验收目录。切换前保留原目录。不要同时启动原数据和副本连接同一 QQ 或执行同一计划。

## MySQL 备份与恢复

独立备份脚本要求 MySQL 8.0+，真实集成验证使用 MySQL 8.4。读取计数器时在当前连接设置 `information_schema_stats_expiry=0`，再通过 `SHOW CREATE TABLE` 让 InnoDB 打开表并初始化原生计数器，随后单表重读 `SHOW TABLE STATUS`。只关闭统计缓存不足以处理尚未打开的新表。此过程不执行 `ANALYZE TABLE`，不修改来源数据。脚本根据列结构验证自增计数器，缺少可信计数时拒绝备份，不会把空值当作 1 或从 `MAX(id)` 推算删除前的高水位。这项要求只针对备份脚本，没有修改插件运行时的数据库兼容代码。

安装项目声明的 PyMySQL 依赖。凭据只从当前维护进程环境读取，不自动搜索 `.env`，不写入归档或错误输出：

| 环境变量 | 含义 |
| --- | --- |
| `MLW_MYSQL_HOST` | 数据库主机，默认 `127.0.0.1` |
| `MLW_MYSQL_PORT` | 端口，默认 `3306` |
| `MLW_MYSQL_USER` | 维护用户 |
| `MLW_MYSQL_PASSWORD` | 维护用户密码 |
| `MLW_MYSQL_DATABASE` | 插件专用数据库 |
| `MLW_MYSQL_PREFIX` | 表前缀，默认 `mlw_` |

通过部署的秘密管理方式向维护进程注入值，不要把实际账号和密码粘贴进项目文件。备份账号需要读表及查看表结构、触发器信息的权限；恢复账号还需要插入、锁定读和调整自增计数器的权限。

```sh
python scripts/backup_state.py backup-mysql --archive /srv/backups/mlw-mysql.zip
python scripts/backup_state.py backup-mysql --archive /srv/backups/mlw-mysql.zip --apply --writers-stopped
python scripts/backup_state.py verify --archive /srv/backups/mlw-mysql.zip
```

恢复目标必须是专用的空库，且已用**与来源相同的插件版本和建表路径**初始化结构；仅建表，不启动插件生成新身份。来源若由迁移程序的 `ensure_schema` 建表，恢复目标也使用该路径；来源若由正式 `sql/mysql.sql` 建表，则沿用对应 SQL。两种路径的结构、注释或排序规则可能不同，不能假设可互换。SQL 文件默认数据库为 `mai_living_world`，表前缀为 `mlw_`；执行前检查其中的 `CREATE DATABASE`、`USE` 和表前缀，明确改为你的空恢复目标，避免指向运行库。脚本不会从备份执行 DDL，也不会擅自创建数据库。裸空库会返回 `mysql_initialized_schema_required`。

把维护进程的环境变量改为目标库，再运行：

```sh
python scripts/backup_state.py restore-mysql --archive /srv/backups/mlw-mysql.zip
python scripts/backup_state.py restore-mysql --archive /srv/backups/mlw-mysql.zip --apply --writers-stopped --confirm RESTORE_EMPTY_TARGET
```

脚本比对表清单、列、索引、约束、引擎与排序规则，并验证所有目标表为空。自增计数器只向上预置，避免删除过的来源 ID 在恢复后被新记录重用；随后重新确认空表并加锁，在一个事务中插入全部状态，再逐表验证内容摘要后提交。失败时数据行全部回滚；空表的自增计数器可能已升高，不会因此删除或覆盖已有行。

这是同后端、同结构的恢复，不负责跨版本降级或 SQLite/MySQL 格式转换。跨后端迁移使用项目已有的 `scripts/migrate_all_sqlite_to_mysql.py`，先在副本上升级和验收。

旧 v0.14 的 44 表与 v0.15 的 47 表 MySQL 归档仍可验证，并恢复到匹配的空旧结构；直接恢复到新增连续性账本的 51 表结构会返回 `mysql_catalog_requires_matching_schema_then_upgrade`。请先按原版本和建表路径还原并验收，再运行新版本的正式非破坏性建表迁移。工具不会忽略新表、丢弃已有约定或混合执行旧归档的 DDL。

## 验证范围

自动离线演练覆盖十二库 51 表、真实存储类建表、身份与主人恢复、模型额度、成长状态、共同计划、相处约定与撤回回忆、连续性状态、SQLite WAL、旧十库及十一库清单兼容、损坏归档、路径攻击和非空目标保护。模拟 MySQL 验证一致性事务、旧 44/47 表与新 51 表结构差异拒绝、失败回滚与计数器保留。

真实 MySQL 测试只在 `MLW_LIVE_MYSQL=1` 且指定独立的 `MLW_BACKUP_MYSQL_DATABASE=mlw_backup_test...` 时运行。该测试要求专用库最初完全为空，用当前正式结构演练备份和恢复，结束后删除测试自己创建的表。本机离线测试不能替代这个 CI 检查，也不能替代用户的实际 Docker、存储卷与账号验收。
