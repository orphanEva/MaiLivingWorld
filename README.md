# MaiLivingWorld · 麦麦的小世界 v0.2.0（SQLite / MySQL 双后端）

> 一份 ZIP，MaiBot WebUI 可直接上传安装。独立插件，既不改 MaiBot 本体，也不需要新的 Docker 容器。**初装关闭，打开后仍默认为演练模式。**

## 功能与边界

| 能力 | v0.2.0 状态 |
| --- | --- |
| 统一主动聊天调度 | ✅ QQ 私聊/群聊白名单，既存会话，静默期、免打扰、冷却、日配额 |
| B 站自主发现素材 | ✅ 尝试读取 B 站公开热门 API 的**标题、简介、链接** |
| 小黑盒或其他信息源 | ✅ RSS / Atom，默认示例 RSSHub 小黑盒新闻；必须能在你的 NAS 网络访问 |
| 筛选有趣素材 | ✅ 调用 MaiBot `utils` 任务选择一个话题，也允许拒绝 |
| 记住外部探索 | ✅ 可选写入 A_Memorix `memory.ingest_summary`（单独 chat_id） |
| SQLite / MySQL 去重存储 | ✅ WebUI 配置切换；MySQL 需可达的服务与专用数据库 |
| 现有 QQ 表情包 | ✅ 不覆盖、不接管。Planner 可继续调用你已安装的表情选择插件 |
| 聊天时查看已发现资讯 | ✅ `get_living_discoveries` Tool，仅返回公开素材 |
| 登录 B 站、刷推荐算法 | ❌ 未实现 |
| 真正读取 B 站字幕、音轨、视频画面或评论 | ❌ 未实现，下一阶段扩展；**不能声称“看完视频”** |
| 小黑盒登录、回帖、B 站评论/点赞/投币/发布 | ❌ 不支持，第一版只读 |
| 自动充值 Token / 访问支付 | ❌ 不支持 |
| 人格长期成长、日程/睡眠模拟 | ❌ 尚未实现，暂不复制其他项目的模拟成长逻辑 |

## 安装：WebUI 一键导入 ZIP

1. 确保 MaiBot `>=1.3.10`，插件 SDK `>=2.12.0`。
2. 打开 MaiBot WebUI：**插件扩展 → 更多操作 → 从 ZIP 安装插件**。
3. 选择 `MaiLivingWorld-v0.2.0-WebUI.zip`（不需要手工解压）。
4. 安装完成后到插件管理打开 **MaiLivingWorld · 麦麦的小世界**，在 WebUI 插件设置里调整配置。
5. **如果之前安装了 `MaiProactive` 或其他会主动唤醒的插件，先禁用它们的主动发送功能。** 否则不同插件的频率限制互不知晓，可能重复打扰。
6. ZIP 安装不会覆盖同 ID 已安装插件。升级本插件前先在 WebUI 备份配置和数据并按官方方式卸载或发布新版；勿直接覆盖数据库记录。

ZIP 内部是单一 `MaiLivingWorld/` 文件夹，直接包含 `_manifest.json` 和 `plugin.py`，符合官方 ZIP 结构要求。


## 什么是“去重”？为何增加 MySQL？

不是去重 QQ 消息，而是保存：

- `stories`：公开资讯发现历史（来源、标题、URL、描述、首次发现时间、记忆写入状态），按 `key` 避免重复采集。
- `story_attempts`：同一资讯对同一个私聊/群的**分享尝试**；避免反复推送相同资讯。
- `attempts`：每个聊天流的**主动唤醒尝试**，用于冷却和每日上限。**记录唤醒尝试不等于 QQ 消息发送成功**。

SQLite 和 MySQL 实现相同接口；SQLite 零配置单机更简单，MySQL 方便与已有数据库集中维护，但**不提升语言模型的理解能力**。

### 如何切换到 MySQL

1. 确认 MaiBot 所在 Docker 容器能访问 MySQL。两个容器同一个自定义 Docker 网络时，`mysql_host` 可填写数据库服务名；不同机器请填**私有网络地址**，不要将 MySQL 3306 暴露公网。
2. 用 `sql/mysql.sql` 的建库语句先创建 `mai_living_world` 数据库，给插件一个只允许访问该库的独立账号。插件启动时会自动创建表；首次运行账号需要 `CREATE` 权限。
3. 在 WebUI 插件设置中找到 **「06 / 数据持久化」**，填写以下参数：

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
- **03 / 外部世界**：`enabled = true`，`bilibili_popular_enabled = true`。
- `rss_feed_urls = ["https://rsshub.app/xiaoheihe/news"]` 是示例；小黑盒源失效或 RSSHub 拒绝访问可改为 `[]`，不影响 B 站源。
- **04 / 自主决策**：`model_task = "utils"`，选题模型必须已经在 MaiBot 正常配置。可以设为你现有的其他可用模型任务。

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

QQ 群聊需先在原 MaiBot / NapCat / 适配器里确保群消息可以正常收取；本插件不会替你加群或改变接收规则。

## A_Memorix 与长期记忆

- 正式模式里，如果模型选中一条真实素材，插件尝试通过 `memory.ingest_summary` 写入 A_Memorix，并在 当前配置的存储后端中标识成功。
- 记忆使用 `chat_id = "living_world_explore"`，避免把外部新闻直接混成 QQ 私聊里的用户事实。
- **不保证** 该记忆会自动从任意私聊中被召回；跨作用域检索仍受你现有 A_Memorix 权限、召回和共享策略限制。
- 读取不到 A_Memorix 时跳过记忆，不影响 QQ 主动唤醒。
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
