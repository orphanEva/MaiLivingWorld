# MaiLivingWorld · 麦麦的小世界 v0.6.0（可信记忆与反思闭环）

> 一份 ZIP，MaiBot WebUI 可直接上传安装。独立插件，既不改 MaiBot 本体，也不需要新的 Docker 容器。**初装关闭，打开后仍默认为演练模式。**

## 功能与边界

| 能力 | v0.6.0 状态 |
| --- | --- |
| 统一主动聊天调度 | ✅ QQ 私聊/群聊白名单，既存会话，静默期、免打扰、冷却、日配额 |
| B 站自主发现素材 | ✅ 尝试读取 B 站公开热门 API 的**标题、简介、链接** |
| 小黑盒或其他信息源 | ✅ RSS / Atom，默认示例 RSSHub 小黑盒新闻；必须能在你的 NAS 网络访问 |
| 筛选有趣素材 | ✅ 调用 MaiBot `utils` 任务选择一个话题，也允许拒绝 |
| 记住外部探索 | 🔧 可选启用 WebUI 文本导入桥（本机管理凭证，默认关闭） |
| SQLite / MySQL 去重存储 | ✅ WebUI 配置切换；MySQL 需可达的服务与专用数据库 |
| QQ 主人识别 | ✅ 经适配器提供的真实 QQ ID 验证，群聊/私聊临时身份提示；不授予管理员权限 |
| 现有 QQ 表情包 | ✅ 不覆盖、不接管。Planner 可继续调用你已安装的表情选择插件 |
| 聊天时查看已发现资讯 | ✅ `get_living_discoveries` Tool，仅返回公开素材 |
| 登录 B 站、刷推荐算法 | ❌ 未实现 |
| 真正读取 B 站字幕、音轨、视频画面或评论 | ❌ 未实现，下一阶段扩展；**不能声称“看完视频”** |
| 小黑盒登录、回帖、B 站评论/点赞/投币/发布 | ❌ 不支持，第一版只读 |
| 自动充值 Token / 访问支付 | ❌ 不支持 |
| 人格长期成长、日程/睡眠模拟 | ❌ 尚未实现，暂不复制其他项目的模拟成长逻辑 |

## 标准 [plugin] 配置节（v0.5.0 修复）

SDK Runner 校验插件配置时必须存在 `[plugin]`，其中要有 `config_version`。
本版通过 `config_model` 的标准 `PluginSection` 自动生成：

```toml
[plugin]
enabled = true
config_version = "0.4.4"

[general]
enabled = false
dry_run = true
```

- `plugin.enabled` 是插件总开关；`general.enabled` 只控制主动聊天，默认关闭。外部探索由 `explorer.enabled` 控制，公开认知还需 `cognition.enabled=true` 与 `auto_think=true`；主人身份识别由 `owner.enabled` 单独控制。
- Runner 会自动生成 `config.toml`；发行 ZIP 不包含本地配置，避免覆盖主人 QQ 或数据库密码。
- 已安装旧版并提示缺少 `[plugin]` 时，请先备份原始配置；可以在 WebUI 的原始 TOML 顶部补上上述 `[plugin]` 节，再安装修复版。不要删除插件数据目录中的 SQLite 数据库。

## 中文注释版配置模板（v0.5.0）

仓库根目录的 `config.example.toml` 包含 **8 个分组、56 个变量**，每个变量都附有中文用途、限制和注意事项。WebUI ZIP 也附带这份示例文件作为本地备忘。

**不要直接覆盖现有 `config.toml`。** MaiBot Runner 根据 `Config(PluginConfigBase)` 自动生成运行时配置，示例模板并不会自动加载。已经调通的配置（包括主人 QQ、私聊白名单、数据库、dry_run）应该先备份，并在 WebUI「原始 TOML」中对照添加中文注释即可。

下面给出其中的部分注释：

```toml
[audience]
# 同一目标两次决策的最小间隔为 5 分钟，不能设置成 1 分钟
min_decision_gap_minutes = 120

[memory]
# 开启记忆桥后还必须选 webui_import 并在容器环境变量中配置 MLW_WEBUI_TOKEN
enabled = false
backend = "off"
```

这只是参数说明与注释维护更新，**不改变插件运行逻辑**，不需要重新配置主人识别或主动聊天。

## 安装：WebUI 一键导入 ZIP

1. 确保 MaiBot `>=1.3.10`，插件 SDK `>=2.12.0`。
2. 打开 MaiBot WebUI：**插件扩展 → 更多操作 → 从 ZIP 安装插件**。
3. 选择 `MaiLivingWorld-v0.5.0-WebUI.zip`（不需要手工解压）。
4. 安装完成后到插件管理打开 **MaiLivingWorld · 麦麦的小世界**，在 WebUI 插件设置里调整配置。
5. **如果之前安装了 `MaiProactive` 或其他会主动唤醒的插件，先禁用它们的主动发送功能。** 否则不同插件的频率限制互不知晓，可能重复打扰。
6. ZIP 安装不会覆盖同 ID 已安装插件。升级本插件前先在 WebUI 备份配置和数据并按官方方式卸载或发布新版；勿直接覆盖数据库记录。

ZIP 内部是单一 `MaiLivingWorld/` 文件夹，直接包含 `_manifest.json` 和 `plugin.py`，符合官方 ZIP 结构要求。


## QQ 主人识别与关系模板（v0.5.0）

在插件 WebUI 配置面板中找到 **03 / 主人识别**：

```toml
[owner]
enabled = true
qq = "填写你的QQ号"
title = "主人"
recognize_in_groups = true
recognize_in_private = true
add_to_private_targets = false
relationship_enabled = true
relationship_mode = "banter"        # banter / gentle / tsundere / custom
private_template = ""              # 留空使用预设
group_template = ""                # 留空使用预设
```

身份判断**只依据 QQ 适配器提供的结构化发送者 ID**；昵称、头像、聊天正文中的自称都不是认证依据。只有实际收到主人消息后、且模型请求明确携带同一会话 ID 时，才会临时给 Planner / Replyer 注入识别提示；同群其他人发言会清除匹配状态。提示 90 秒过期，配置变更或插件卸载也会清除。若 Hook 缺失可靠会话 ID，安全起见不进行识别。

此能力只影响聊天关系与称呼，不会绕过 MaiBot 原有权限系统或其他插件的管理命令。**识别主人并不等于取得管理员权限。** 选配 `add_to_private_targets=true` 才会将主人加入本插件的自主私聊白名单，仍受免打扰、每日次数与演练模式限制。

不要把真实 QQ 号写入公开 GitHub 源码；仅在自己 MaiBot 实例的插件设置中填写。

### 主人专属关系模板（不替换原人格）

普通群友仍使用 MaiBot 原本的人格和回复风格，只有可信 QQ ID 验证成功的主人消息才会触发关系提示。

| 模式 | 私聊风格 | 群聊风格 |
| --- | --- | --- |
| `banter` | 损友亲近、互怼接梗 | 比普通群友熟络，但不围着主人转 |
| `gentle` | 温柔偏心、有主见 | 熟悉而克制 |
| `tsundere` | 嘴硬心软，不套用固定口癖 | 偶尔互怼 |
| `custom` | 填写 `private_template` | 填写 `group_template` |

每段自定义文字最多取 1600 字符，填写后优先于预设。可以只自定义一段。群聊始终附加“不主动泄露私聊内容、隐私或私人记忆”的约束。定时主动私聊主人时，也会应用私聊风格；其他 QQ 用户、群聊都不会使用私聊模板。

主人识别**不会改变管理员授权**。不要把真实 QQ 号、私人模板或数据库密码提交到公开仓库。

## 什么是“去重”？为何增加 MySQL？

不是去重 QQ 消息，而是保存：

- `stories`：公开资讯发现历史（来源、标题、URL、描述、首次发现时间、记忆写入状态），按 `key` 避免重复采集。
- `story_attempts`：同一资讯对同一个私聊/群的**分享尝试**；避免反复推送相同资讯。
- `attempts`：每个聊天流的**主动唤醒尝试**，用于冷却和每日上限。**记录唤醒尝试不等于 QQ 消息发送成功**。

SQLite 和 MySQL 实现相同的资讯与尝试记录接口；SQLite 零配置单机更简单，MySQL 方便与已有数据库集中维护，但**不提升语言模型的理解能力**。

**备份注意：MySQL 并未覆盖所有状态。** 即便设为 `storage.backend="mysql"`，`mind.sqlite3`（主人观察）、`cognition.sqlite3`（公开认知）和 `proactive_feedback.sqlite3`（发送反馈/退避）仍位于插件 `data_dir`。备份、迁移时必须同时保留这些文件（建议停止插件后备份 SQLite WAL 数据）和 MySQL 数据，否则可能丢失记忆审核状态或重置冷却。

### 如何切换到 MySQL

1. 确认 MaiBot 所在 Docker 容器能访问 MySQL。两个容器同一个自定义 Docker 网络时，`mysql_host` 可填写数据库服务名；不同机器请填**私有网络地址**，不要将 MySQL 3306 暴露公网。
2. 用 `sql/mysql.sql` 的建库语句先创建 `mai_living_world` 数据库，给插件一个只允许访问该库的独立账号。插件启动时会自动创建表；首次运行账号需要 `CREATE` 权限。
3. 在 WebUI 插件设置中找到 **「07 / 数据持久化」**，填写以下参数：

```toml
[storage]
backend = "mysql"               # sqlite / mysql，默认 sqlite
mysql_host = "mysql"             # 同 Docker 网络里 MySQL 的服务名；按实际环境修改
mysql_port = 3306
mysql_user = "mai_living_world"
mysql_password = "填写自己的强密码" # 不要上传日志或提交到 GitHub
mysql_database = "mai_living_world"
mysql_table_prefix = "mlw_"
mysql_connect_timeout = 5
```

4. **保存并重载插件/重启 MaiBot**，查看日志出现 `storage=mysql`。只有当前后端被选中；不会双写。
5. 没有运行 MySQL 时，保持 `backend = "sqlite"`，ZIP 仍可安装。MySQL 连接失败会报错而不会自动退回 SQLite，防止记录错乱。

**数据迁移：** 切换后不会自动把旧 SQLite 的内容写入 MySQL。首次测试若 SQLite 已有真实记录，请先停止插件、备份数据库，再使用源码仓库中的 `scripts/migrate_sqlite_to_mysql.py` 进行一次性迁移；只支持**空的目标 MySQL 表**，以避免错误覆盖。迁移前从环境变量传入 `MLW_MYSQL_HOST`、`MLW_MYSQL_PORT`、`MLW_MYSQL_USER`、`MLW_MYSQL_PASSWORD`、`MLW_MYSQL_DATABASE`，以及可选的 `MLW_MYSQL_PREFIX`，执行：

```bash
python scripts/migrate_sqlite_to_mysql.py --sqlite /path/to/living_world.sqlite3
```

当前 MySQL 接入通过模拟连接测试，**尚未连接实际数据库做端到端验收**。如果你使用现有的 cap MySQL，请用单独的 `mai_living_world` 库，或至少不同表前缀，防止和 cap 的表混用。配置文件可能保存明文数据库密码，请限制文件权限并避免提交到仓库。

## GitHub 公开仓库与发布 ZIP

仓库源码根目录应包含 `_manifest.json`、`plugin.py`、各 Python 模块、`tests/`、`sql/` 和 `.github/workflows/tests.yml`。发布给最终用户的 ZIP 则要保持单层 `MaiLivingWorld/` 插件目录，避免套多层文件夹。

本地可执行 `python scripts/build_release.py` 生成发行 ZIP。仓库内 `.github/workflows/release.yml` 会在推送 `v*` 标签时自动构建并作为 GitHub Release 附件发布；普通源码仓库 ZIP 不应冒充 WebUI 安装包。

源码采用 MIT；仅参考并注明社区项目的设计，**没有直接复制 GPL/AGPL 组件代码**。

## 第一阶段：只演练、不发消息

WebUI 找到以下分组。保存设置后确认插件已启用：

- **01 / 基础与安全**：`enabled = true`、`dry_run = true`。
- **02 / 允许联系的 QQ**：`private_users = ["你的QQ号"]`，`group_ids = []`。必须先和麦麦有 QQ 私聊消息。
- **04 / 外部世界**：`enabled = true`，`bilibili_popular_enabled = true`。
- `rss_feed_urls = ["https://rsshub.app/xiaoheihe/news"]` 是示例；小黑盒源失效或 RSSHub 拒绝访问可改为 `[]`，不影响 B 站源。
- **05 / 自主决策**：`model_task = "utils"`，选题模型必须已经在 MaiBot 正常配置。可以设为你现有的其他可用模型任务。

默认有 25 秒启动等待和 60 秒内部检查，首次检查成功后才产生数据；不要因为安装即刻无消息就认为失败。演练模式会真实访问资讯源并调用模型、**会消耗 API Token**，但不会唤醒发消息、不会写入 A_Memorix。为了省钱，模型评估同一目标默认至少间隔 120 分钟。

查看日志：

```bash
# 容器名不叫 maibot 时换成实际名称
docker logs --since 30m maibot 2>&1 | grep -i 'MaiLivingWorld'
```

期望看到：

```text
[MaiLivingWorld] 数据源 B站热门 获取 ... 条，新增 ...
[MaiLivingWorld] 探索结束：新增 ... 条
[MaiLivingWorld][DRY-RUN] 目标=qq:private:... 计划=explore 题目=...
```

如果没有 `[DRY-RUN]`，可能是免打扰、最近聊天活跃、模型不愿分享、无历史会话或尚未达到闲置时长。**出现资讯抓取成功只说明数据源可用，不代表已经成功唤醒 Planner。**

想加速一次实验，可暂时调整 `private_min_idle_minutes=1`、`check_interval_minutes=1`、`min_decision_gap_minutes=5`、免打扰开始与结束都设为 `00:00`，然后保持一段时间不发消息，观察日志；完成后请恢复默认值。

## 第二阶段：允许它主动来找你

确认日志中有 `[DRY-RUN]` 后，把 `dry_run` 改为 `false`，其他安全限制先保持默认：

| 控制 | 默认值 |
| --- | --- |
| 私聊无消息多久才考虑 | 90 分钟 |
| 群聊无消息多久才考虑 | 35 分钟 |
| 同目标两次实际唤醒最少间隔 | 6 小时 |
| 每目标每日最多实际唤醒请求 | 2 次 |
| 模型选题最短间隔 | 120 分钟 |
| 夜间免打扰 | 23:00—09:00（按配置的时区） |
| 群聊白名单 | 空，默认**不主动发群** |

插件通过 `ctx.maisaka.proactive.trigger()` 唤醒 MaiBot 原生 Planner；麦麦自己决定开口还是沉默。因此日志显示“主动唤醒请求已提交”**不等于** QQ 实际发送成功。

最近消息默认只查 24 小时；不足两条时插件会回查该会话数据库中更早的持久化消息（有条数上限），不会只因一天没聊天就放弃。宿主删掉全部历史、没有既存会话或没有合适的话题时，仍不会强行发消息。

主动发送回执现校验平台账号和发送者，但目前 Host Hook 没有完整的主动任务↔单条发送的关联 ID，并发情况下仍无法做到绝对精确。公开见闻会等到符合条件的发送成功回执后才提交 A_Memorix，Planner 选择 stop、发送失败或中断时不提前写入。

QQ 群聊需先在原 MaiBot / NapCat / 适配器里确保群消息可以正常收取；本插件不会替你加群或改变接收规则。

## A_Memorix 与长期记忆

- v0.5.0 只有显式启用 `memory.backend="webui_import"` 并提供 `MLW_WEBUI_TOKEN` 后，才通过官方 WebUI 导入接口写入 A_Memorix。
- 为防止使用虚构的聊天流 ID，WebUI 模式只导入公开素材到 **global 全局作用域**；不包含 QQ 私聊内容。
- 任务完成 `completed` 才标记成功；提交排队、失败或超时都不能冒充写入成功。
- 不保证自动在任意私聊中召回；是否检索取决于 A_Memorix 的召回设置。
- 未开启桥接或 WebUI 不可用时，跳过记忆而不影响主动聊天。
- 记录明确包含“只读取标题/简介，并未完整观看或核验”的来源限制。真正读字幕/评论要等下一阶段。

## 技术与稳定性

- 默认 SQLite WAL；可在 WebUI 切换为 MySQL，记录资讯、已尝试分享的对象、主动唤醒次数与写入状态。
- 重启后仍能挡住重复分享和当天超额唤醒；所有真正的唤醒请求都**先记账再提交**，失败时不自动重复刷屏。
- 仅对明确填入的 QQ 私聊用户和群号尝试唤醒，不会遍历并联系所有 QQ 好友。
- 信息源仅允许配置的 HTTPS 白名单主机、不跟随 HTTP 重定向、限制响应体积和请求超时；但这不等于对外部网络和内容安全的绝对保证。不要添加不可信 RSS 地址。
- 外部内容只作为素材交给 LLM，不应作为指令执行。模型仍可能误解内容，所以本插件不提供任何支付、社区发帖、账户更改等写操作。
- 插件代码只依赖 Python 标准库及 MaiBot SDK 已提供的配置模型，未另行安装浏览器或 MCP 服务。

## 常见问题

**ZIP 被 WebUI 拒绝？** 确认你选的是发行 ZIP、没有再二次打包；插件目录只有一层，顶层直接可看到 `MaiLivingWorld/_manifest.json` 与 `MaiLivingWorld/plugin.py`。同 ID 已存在时，WebUI 不允许重复安装。

**显示安装成功但没有运行？** 在插件管理开启它，确认 `general.enabled=true`；查看 Runner 日志。若出现 SDK 或宿主版本不兼容，不能仅靠手工放宽清单版本来解决，应检查实际接口。

**B站接口返回 -412/403/429？** 可能是 B 站风控。插件按只读模式跳过，不尝试绕过。可只用可访问的 RSS/Atom。

**小黑盒没数据？** RSSHub 公共实例和路由不保证一直可用，先查看日志；禁用该 URL 或换成可信、实际可访问的镜像。独立关闭它不会影响其他信息源。

**本来就没主动发图片？** 本插件不直接挑选表情包，只让 Planner 有机会使用你现有的文字选图 Tool；主动发图概率和表达仍由 MaiBot/其他插件决定。

**为什么安装了却不马上主动找我？** 默认演练 + 免打扰 + 90 分钟空闲 + 120 分钟决策间隔；并且 Planner 可以选择沉默。这是设计行为。

**会自动给自己充值吗？** 不会，也不索要支付凭证。

## 与其他社区项目的关系 / 致谢

本插件是**独立实现**，参考以下公开项目的设计思路，没有合并它们的全部功能，也没有直接复制其实现代码：

- hot-topics（资讯来源与上下文注入思路，MIT）：https://github.com/ZhaoCang-QWQ/hot-topics
- maibot-bilibili-mcp（外部回想及 A_Memorix 记忆能力思路，MIT）：https://github.com/umouyoumou-del/maibot-bilibili-mcp
- bilibili_autopilot（浏览任务独立于回复、去重和配额，MIT）：https://github.com/namchengwuqiu/bilibili_autopilot
- A_chatter（调度/唤醒方向，GPL-3.0-or-later）：https://github.com/A-Dawn/A_chatter
- LifeSimulationPlugin（内部状态方向，AGPL-3.0）：https://github.com/SkonJIYHD/LifeSimulationPlugin
- 官方 SDK 与插件安装规范：https://docs.mai-mai.org/manual/plugins/management

本项目代码本身采用 MIT；**不包含 GPL/AGPL 项目代码**。如未来实际引入第三方代码，必须审查并遵守对应许可证。

## 下一版规划

- 经用户明确授权后接入更深层 B 站只读内容（字幕/评论区），并保留可追溯来源，防止假装完整观看。
- 对外部发现构建持久“经历”对象（何时发现、看过哪些材料、当时自己的看法），而不只保存标题。
- 增加日程感与兴趣逐渐变化机制，但不得伪称现实生活里的身体经历；外部行动需用户单独授权。
- 进一步检查 MaiBot 1.3.x 实机 Runner 兼容性，增加真实网络源和 NapCat 端到端验收。

## 群聊关系归属修复 (v0.5.0)

群聊主人提示必须同时匹配入站消息的可信 QQ 发送者、session_id 和消息 ID。Replyer 的 reply_message_id 匹配该消息才会注入主人关系。Planner 缺少目标消息 ID，群聊不注入主人提示；目标 ID 缺失时宁可跳过。成功注入时会出现 `[MaiLivingWorld][OWNER]` 日志。

## v0.5.0 · A_Memorix WebUI 兼容导入桥（显式开启）

社区 maibot-bilibili-mcp 采用未注册的 memory.ingest_summary 能力；
Mai_Forever_memories 使用已迁移的旧 LPMM 内部接口，均不可直接照搬。
本版仅使用官方 WebUI 记忆文本导入任务 API，不直接访问 A_Memorix 内部库。

**默认关闭，需要明确开启。** WebUI Token 拥有管理权限；不要把 Token 写进仓库或插件 TOML。
在 MaiBot **所在容器**的环境变量设置 MLW_WEBUI_TOKEN（WebUI 正式登录 Token），
重建容器让插件 Runner 继承该变量。

```toml
[memory]
enabled = true
backend = "webui_import"
webui_port = 8001
import_max_wait_seconds = 45
chat_id = "living_world_explore" # 旧字段，本桥接不使用
```

仅连接本容器 127.0.0.1:8001；不支持指定远程 URL。WebUI 必须在同一网络命名空间内可达。
只导入公开 B 站/RSS 标题、简介和来源，采用 global 公共记忆作用域，绝不导入 QQ 私聊。
异步导入任务状态达到 completed 才标记 memory_saved=1；失败、超时或排队不算成功。
采用内容哈希去重；超时可到 A_Memorix「导入任务」页面查询。

## v0.5.0 · 内在反思与关系成长（首期）

- 新增独立 `[mind]` 配置（默认关闭），不影响现有主人识别、B站探索、主动聊天与 A_Memorix 桥接。
- 仅从已绑定主人 **QQ 私聊** 中进行周期复盘，使用现有 `brain.model_task`；至少 8 条消息且历史确有变化，每位主人默认 12 小时限频。
- 提取偏好、共同经历、明确边界、未完话题四种候选观察；严格 JSON、置信度过滤、敏感信息屏蔽；不从一句玩笑自动推断亲密关系升级。
- **不保存聊天原文**，只在插件独立 `mind.sqlite3` 保存有限结构化观察；不写入 A_Memorix global 公共资讯库，也不混进 B站资讯表。
- 只有主人私聊能读取个人观察用于回复风格微调；群聊、其他 QQ、公开消息不得读取。
- 只有显式且高置信的观察，或多次独立反思得到的重复观察，才会逐渐用于提示。不做每聊一次好感度+1。
- 提供仅内部可用的 `living_world_mind_status`（查看笔记）和 `living_world_mind_clear(confirm="CLEAR_OWNER_MIND")`（清空主人本地反思数据）。
- 注意这是“定期反思摘要 + 可修正的行为调整”，不是自我意识或持续运行的隐藏思维链。世界视觉浏览将留待后续。

启用方式（建议先备份原始 config.toml）：

```toml
[mind]
enabled = true
auto_reflect = true
interval_hours = 12
history_limit = 40
min_history_messages = 8
min_confidence = 0.8
note_limit = 20
```

启用后在插件日志搜索 `[MaiLivingWorld][MIND]`。如果配置已生效而长期没出现日志，请核对是否存在主人既有私聊会话、至少 8 条有效私聊消息以及反思模型任务是否可用。该模块不会替代现有 A_Memorix 检索。


## v0.6.0 · 有来源的主人私聊反思、冲突审核、版本回滚

- 反思仅使用 MaiBot Host 提供了真实 `session_id`、`message_id`、`message_info.user_info.user_id` 的结构化 QQ 私聊消息；不获取群消息或陌生人发言。没有足够可信消息时保守跳过。
- 每条候选观察都要引用当前确实存在的 `evidence_ids`；SQLite 中只存结构化观察、证据哈希和索引，不存完整聊天正文。
- 相同私聊消息在多次反思中重复出现也只计算一次证据；模型自称的置信度不是事实保证。
- 模型提出的旧记忆矛盾先进入待审核队列，旧笔记暂停注入。只有显式审核才决定否定旧观察或恢复旧观察。
- 写入、审核及清空之前生成结构化版本快照，支持回滚。
- 升级到 0.5.1 之后，旧版没有消息来源关联的 v0.5.0 笔记保留为 `legacy`，不再自动注入；不删旧数据。
- 新增 `mode`，默认 `observe`（观察，不改变聊天内容）。人工检查候选、冲突后可以手动切换到 `active`（仅对绑定主人的私聊注入有来源的有效笔记）。群聊始终不注入私聊笔记。

```toml
[mind]
enabled = true
auto_reflect = true
mode = "observe"  # 推荐先观察；确认后才改为 active
interval_hours = 12
history_limit = 40
min_history_messages = 8
min_confidence = 0.8
note_limit = 20
```

内部管理 API（`public=False`，不要暴露给普通 QQ 用户；含隐私笔记的状态不应截图公开）：

- `living_world_mind_status()`：查询观察、待审核项目、快照。
- `living_world_mind_review(review_id, decision, confirm="CONFIRM_MIND_REVIEW")`：`approve`=确认旧笔记已过时，`reject`=否定冲突并恢复旧笔记。
- `living_world_mind_restore(snapshot_id, confirm="CONFIRM_MIND_RESTORE")`：还原结构化笔记，不改动 A_Memorix。
- `living_world_mind_clear(confirm="CLEAR_OWNER_MIND")`：清空当前绑定主人的笔记，操作前保留恢复快照。

**边界**：模型不能被证明百分之百理解真实意图；其提供的证据关联仍可能错误，所以请先检查观察模式结果，确认后再应用。长期运行建议备份 `mind.sqlite3`。

## v0.6.0 · 主动聊天 Planner 回执和沉默退避

针对插件主动唤醒后 Planner 连续 `stop`，没有发 QQ 消息却用完每日额度的问题，
新增独立的 `proactive_feedback.sqlite3`（与 `mind.sqlite3` 和外部资讯库隔离）。
通过官方 `maisaka.planner.after_response` 观察模型选择 `reply/stop`，
通过 `send_service.after_send` 观察 Host 的真实发送回执；不控制 Planner 决策。

**注意**：发送回执确认该会话确有出站发送成功。如果其他插件在同一会话、同一时间窗口发送消息，仅凭现有 Hook 仍有归因歧义。收到新的用户消息会中止任务归因。

- `audience.max_daily_per_target`：每天最多确认发送的主动轮次；Planner `stop` 不占这项额度。
- `audience.max_daily_attempts_per_target`：每天最多主动唤醒尝试次数，包含 stop，防止刷模型 Token。
- `audience.min_gap_hours`：两次确认发送的最小间隔小时数。
- `audience.min_attempt_gap_minutes`：两次主动尝试之间最少间隔分钟数。
- `audience.stop_backoff_base_minutes`：首次 stop 的退避，后续连续 stop 翻倍。
- `audience.stop_backoff_max_minutes`：退避上限。

```toml
[audience]
min_gap_hours = 6.0
max_daily_per_target = 2
min_attempt_gap_minutes = 30
max_daily_attempts_per_target = 8
stop_backoff_base_minutes = 45
stop_backoff_max_minutes = 240
```

**迁移说明**：旧版 `attempts` 表继续累计当天唤醒尝试，升级后不会把旧尝试补记成确认发送。升级当天配额统计可能有断层。无需迁移旧 SQLite/MySQL 资讯库，反馈账本不保存私聊文本。

日志搜索 `[MaiLivingWorld][PROACTIVE]`：可见 `Planner选择stop`、`Planner选择reply`、`send_confirmed`、`send_failed` 和 `跳过 ... reason=...`。内部 `living_world_status` API 的 `proactive_feedback` 提供每个目标的今日结果计数及退避信息。

## v0.6.0 · 有限自主认知（公开资讯试验版）

这一版在 v0.5.2 的主人识别、私聊证据反思、A_Memorix 和主动聊天基础上，
**独立增加** `[cognition]`（默认关闭）。不动已有数据表；公开认知存储于
`cognition.sqlite3`，与私人 `mind.sqlite3`、公开资讯库分离。

工作机制：仅分析保存的 B 站/RSS 标题、简介和来源 → 提出最多 2 个待验证问题
→ 标注观察与未知 → 等待新的公开来源 → 更新问题状态与证据引用 → 人工审核完成/废弃。
每个来源有唯一 key，已经分析过的来源不会重复当新证据。模型输出只作为候选，严格校验
来源 key、问题 ID 和允许的下一步动作。

### 安全边界

- `enabled=false` + `auto_think=false` 是升级默认值；原功能不变。
- 需要 `general.enabled=true` 与 `explorer.enabled=true`，且已经采集到新公开资讯才会调用模型。
- `mode=observe` 只写公共观察笔记，不影响 QQ 聊天或主人私人反思。
- `mode=active` **仅让公开关注的主题成为外部资讯选题的弱偏好**；不能自动发QQ、执行工具、搜索网页、修改人格、访问私人聊天或授权消息。
- `next_action` 只能是 `wait_for_update`（等新资料）、`compare_public_sources`（比较来源）、`ask_when_relevant`（合适时可提出问题的计划），但 **只存计划，不执行**。
- 标题和简介不等于已核实内容；即使更新了多条来源，也不能让模型自行把问题标记为完成。
- 每日尝试次数与最小间隔由 SQLite 跨重启保留，失败也计入调用预算，避免反复烧 Token。
- 原有 `[mind]` 私聊反思仍建议保持 `mode="observe"`，两个模块相互独立。

### 开启方式

```toml
[cognition]
enabled = true
auto_think = true
mode = "observe"
interval_hours = 6
max_daily_cycles = 2
max_public_candidates = 6
max_open_questions = 8
max_new_per_cycle = 2
```

日志搜索 `[MaiLivingWorld][COGNITION]`，正常时格式：

```text
[MaiLivingWorld][COGNITION] 处理公开素材=6 新增关注=2 更新关注=0 mode=observe
```

仅供插件内部/管理员调用的 API：

- `living_world_cognition_status`：只读查看问题及近期预算。
- `living_world_cognition_evidence(goal_id)`：查看公开标题、原始链接和问题更新事件。
- `living_world_cognition_review(goal_id, decision, confirm="REVIEW_COGNITION")`：
  `decision` 只接受 `resolved / discarded / reopen`，**只改变问题状态**，不执行计划。

本阶段还没有可视化问题管理页面，不支持自动网页浏览和真正的后台行动。
从观察中提出一个问题不等于产生意识，也不等于已经证实信息。

