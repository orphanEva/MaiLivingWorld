# Docker 中的 MCP 与账号配置

## 在 MaiBot 容器查新闻和百科

上传 v0.29 或以上的插件 ZIP，完成「快速开始」并绑定主人 QQ。在 **阅读与工具 → MCP 工具与自主查阅** 打开 **启用 MCP 工具** 和 **查新闻和百科**。使用详细配置的实例还需启用主人和 `active` 统一心智。主人私聊可先试“查一下维基百科上的月球轨道，给我词条出处。”内置 MCP 服务用 MaiBot 自己的 Python 启动，新闻和百科都无需账号，也无需增加 Compose 服务或开放端口。

使用 v0.30 时，可把 **自主上网** 设为 **只逛逛** 并启用工具观察记忆。她实际选读过内容后，主人私聊问“你最近逛到了什么？”，会按有效来源回顾最近七天内最多两条自主阅读。浏览记录随原插件数据卷保存，升级和重启时保留该数据卷。此功能使用同一套配置和工具额度。

需要她联网查特定主题时，直接发“查一下月球轨道”或“搜索韦布望远镜的新发现”这样的纯文本。联网检索会核对本轮明确提出的公开主题；返回需要公开搜索词的提示时，可以重新单独说清要查的主题。自主浏览则从已有兴趣对应的公开话题中选词。

## v0.31 继续查问题与聊阅读感受

在「快速开始」选择「陪伴与成长」，并打开前述 **启用 MCP 工具**、**查新闻和百科**、工具观察记忆。需要她继续研究成长档案里的问题时，在同一 MCP 配置节开启 **继续查阅未解问题**（`research_enabled`）。详细配置实例还需启用 `growth.enabled` 并设置 `growth.mode = "active"`。后台可根据仍有效的公开来源与话题词查询新闻和百科，Brave Search 密钥按下节选配。此流程使用原有查阅间隔、每日轮数和模型预算，每轮最多两次检索并读取一个结果。

阅读感受追问复用 `reference.enabled`。开启后，等她在主人私聊实际说过一篇材料，再直接问“你觉得它哪里有意思？”。追问使用当前有效的原资料和已有相关成长记录，来源不清时先确认。启用 `growth.emotional_episodes` 的实例还能带入与该材料相关、仍有效的事件感受。

升级到 v0.31 时保留原插件数据卷。自主浏览的正文已读标记会保留24小时，依据成功回执与当前候选版本减少重复选读。具体操作和验收见[伴侣说明](COMPANION.md)与[连续对话验收](EVALUATION.md)。

## 可选的网页搜索和 Mastodon 账号

| 想让她做什么 | 在哪里配置 |
|---|---|
| 搜索更广的网页并打开搜索结果 | 在 [Brave Search 开发者后台](https://api-dashboard.search.brave.com/)申请 Web Search API 密钥，将其作为 `MLW_BRAVE_SEARCH_KEY` 提供给 MaiBot。查询额度和费用按自己的账号套餐计算。 |
| 自己阅读新闻和搜索结果 | 打开前述新闻和百科开关，把 **自主上网** 设为 **只逛逛**。 |
| 读 Mastodon 的公开提及与讨论 | 在接受机器人账号的实例为她注册独立账号。在该账号「首选项 → 开发 → 新建应用」生成用户访问令牌，配置下方的实例和令牌。账号读取、状态读取、搜索和通知分别需要 `read:accounts`、`read:statuses`、`read:search` 和 `read:notifications` 权限。 |
| 用该账号公开回复 | 给令牌增加 `write:statuses`，选择 **参与讨论**，在 **运行与存储 → 01 / 基础与安全** 关闭 **安全演练**（`dry_run`）并保存。若只想让她在 Mastodon 实发，让同页的 **主动 QQ 联系**（`enabled`）保持关闭。首次安装默认关闭主动 QQ 联系、开启演练。 |

在运行 **MaiBot** 的 Compose 服务下加入需要的变量，值存放在部署机的私有环境文件或密钥管理中。实例填写账号所在服务器的 HTTPS 根地址，例如 `https://social.example.org`。令牌留在部署环境中，协助她绑定账号时无需将凭据发到 QQ。

```yaml
environment:
  MLW_BRAVE_SEARCH_KEY: ${MLW_BRAVE_SEARCH_KEY:-}
  MLW_MASTODON_INSTANCE: ${MLW_MASTODON_INSTANCE:-}
  MLW_MASTODON_TOKEN: ${MLW_MASTODON_TOKEN:-}
```

更新环境后用原 Compose 配置重新创建 MaiBot 容器，保留已有数据卷。容器需要能解析并直连对应的公网 HTTPS 地址。遇到长期读不到网页，可在容器里检查 DNS 是否返回保留地址。不同 Mastodon 实例的搜索范围和公开内容各不相同。账号注册与权限定义见 [Mastodon 注册说明](https://docs.joinmastodon.org/user/signup/) 和 [OAuth 权限文档](https://docs.joinmastodon.org/api/oauth-scopes/)。

## CAP、Pixiv 和聊天媒体

**只想先和她聊起来**，在 MaiBot 的插件设置打开「快速开始」，选择「日常陪伴」，填写你自己的 QQ 号后保存。普通私聊可直接使用 MaiBot 容器原有的模型和插件数据卷。想让她自动整理聊天、形成兴趣时，改选「陪伴与成长」。需要阅读 CAP 或 Pixiv 资源时，再按下文部署对应网关。

**想让她看聊天图片和视频**，让插件运行环境安装发行清单中的 Pillow 与 imageio-ffmpeg，给 MaiBot 的 `vlm` 任务指定支持图片输入的模型。回到插件「阅读与工具 → 连续阅读与学习」，启用「看聊天里的图片和视频」。群聊还要在「记忆与成长 → 聊天记忆与群名单」填入群号。媒体处理直接由 MaiBot 的插件和适配器完成，CAP/Pixiv 网关用于各自的缓存作品。B 站分享需要容器能访问官方公开接口及相应媒体域名。测试方法见[连续对话验收](EVALUATION.md)。

如果私聊图片正常、B 站链接却一直读不到，可在 MaiBot 容器里运行 `getent ahostsv4 api.bilibili.com` 检查 DNS。使用代理的网络有时会返回 `198.18.x.x` 一类保留地址。媒体下载只连接经过校验的公网地址。这种部署需要让容器对 B 站及对应 CDN 域名使用可直达的公网解析，或让管理员调整代理分流。

v0.26 可随 WebUI ZIP 升级。保留现有插件数据卷以及 SQLite 或 MySQL 连接配置，在已绑定主人的环境开启统一心智，设为 `active` 模式，再开启 `reference.enabled`，即可跨 Docker 重启保留她亲自向主人提过的资料版本。此功能使用插件自己的话题账本。开启步骤与测试例子见[长期伴侣说明](COMPANION.md)和[连续对话验收](EVALUATION.md)。

v0.25 的回复协调、事件感受和建议/修复归档继续使用原插件及两个网关，无新增服务、端口或DDL。四个新开关默认关闭，配置30节197字段、95文件包，仍为二十一SQLite库/82张MySQL表；保留原持久卷即可升级。情绪与归档元数据随已有账本备份，恢复后继续核验时间、来源和遗忘边界。合并配置与真实宿主验收见 [长期伴侣说明](COMPANION.md)、[连续对话验收](EVALUATION.md) 和 [状态备份](BACKUP.md)。

v0.24 增加默认关闭的 `[repair]` 自身表达修复，继续使用原插件及两个网关，不增加服务或端口。需保留插件状态持久卷；SQLite新增 `repair.sqlite3`，MySQL新增 `repair_control/items/inputs/receipts` 四表，合计二十一库/82表。配置30节193字段、92文件发布包。核验实际发送和主人纠正后，经两阶段复核保存有限的表达修复，沿用共享模型预算；不用MCP查询代替主人证据。开启和宿主验收见 [长期伴侣说明](COMPANION.md)、[连续对话验收](EVALUATION.md)、[状态备份](BACKUP.md)。

v0.23 的活动感知与性格取舍继续使用原插件及两个网关，无新增服务、端口或数据库表。配置29节189字段、89文件发布包；`attention.activity_awareness` 与 `growth.personality_tensions` 默认关闭，两项本身零额外模型调用。沿用二十SQLite库/78张MySQL表及原持久卷即可保留注意力负荷、活动事件与成长性格；高负荷只暂缓新后台探索，主人当前消息仍照常处理。启用与真实宿主验收见 [长期伴侣说明](COMPANION.md)、[连续对话验收](EVALUATION.md) 和 [备份说明](BACKUP.md)。

v0.22 的观点复核与当前喜讯回应继续使用原插件和这两个网关，无新增服务、端口或数据库表。配置29节187字段、88文件发布包；`growth.review_beliefs` 默认关闭，启用后复用成长最多两次模型调用及共享预算。沿用二十SQLite库/78张MySQL表，保留原持久卷即可延续已提交观点与分歧。喜讯回应只影响当前主人私聊语气，不保存长期情绪。升级、启用与实际验收见 [长期伴侣说明](COMPANION.md)、[连续对话验收](EVALUATION.md) 和 [备份说明](BACKUP.md)。

v0.21 建议反馈与话题承接无需新增MCP服务或Docker端口。沿用原插件持久卷/数据库即可跨重启保存；新增账本与配置见 [备份说明](BACKUP.md) 和 [长期伴侣说明](COMPANION.md)。

v0.20 的自主项目与共同阅读继续使用相同网关，无新增端口。项目仅查已有 Pixiv 库或继续已有小说；共读适用于已有小说、插画、cap PDF 的实际阅读单元。新增开关和主人指令见 [长期伴侣说明](COMPANION.md)，两个新增账本见 [备份范围](BACKUP.md)。

v0.19 的任务回告继续使用这里的两个网关及既有MCP分支，无新增服务或Docker端口。新任务来源登记需先启用 `[followthrough]`；具体开关、独立整句送达确认和验收见 [长期伴侣说明](COMPANION.md)，新增两份状态账本纳入 [备份范围](BACKUP.md)。

这套配置给 Docker 中的 MaiBot 提供两个固定地址：

- `http://cap-mcp:8000/mcp`
- `http://pixiv-mcp:8000/mcp`

两个网关均通过带 Bearer 鉴权的 Streamable HTTP 提供 `/mcp`。MaiBot 1.3.10 容器继续使用其自带的 MCP 1.x（1.28.1 起）。各网关使用独立的 MCP 2.3 环境，分别维持一个 stdio 子进程。cap 的 MCP 1.30 与 Pixiv 适配器使用各自的虚拟环境。连接仅通过 Docker 内网。

## 准备已有服务与源码

需要 Docker Compose 2.17 或更新版本及 BuildKit 的 `additional_contexts` 支持。MaiBot、Pixiv HTTP 后端及其 worker 仍由原来的部署管理，本配置不会启动或重建这些服务。

默认目录关系是 `MaiLivingWorld`、`cap`、`pixiv-cache-backend` 位于同一个父目录。Pixiv 源码必须已经包含 `app/mcp`（当前对应 `codex/mcp-server` 分支），cap 必须包含 `mcp_server.py`、`requirements-mcp.txt` 与持久任务模块。构建不会替你切换源项目分支。目录不同可在私有环境文件中设置绝对 `CAP_SOURCE_DIR`、`PIXIV_SOURCE_DIR`。

Dockerfile 仅复制列出的运行源码和本目录无凭据的 cap 配置示例；不复制源项目 `.env`、`nacos_config.yaml`、用户 `data`、缓存或数据库。不要把它改成 `COPY .`。

## 网络与凭据

先查询正在运行的 MaiBot 容器实际连接的网络：

```sh
docker inspect --format '{{json .NetworkSettings.Networks}}' <实际的MaiBot容器名>
```

把确认的网络名填入 `MAIBOT_DOCKER_NETWORK`。这里没有默认网络名；不会创建一个与现有 MaiBot 无关的网络。网关和 MaiBot 必须在同一网络中才能通过上面的服务名访问。如果 MaiBot 没有接到这个网络，可在它自己的 Compose 文件中声明同一个 `external: true` 网络并重新部署，或明确执行 `docker network connect <实际网络名> <实际MaiBot容器名>`；后者应同步到其 Compose 配置以便重建后保留。

Pixiv 后端也必须能从该网络到达。可把其 API 服务接入同一外部网络，或为网关额外配置后端现有网络；`PIXIV_MCP_BACKEND_URL` 使用确认后的服务名或可达地址。容器里的 `127.0.0.1` 不是宿主机，也不是另一个容器。

复制 `mcp.example.env` 到自己管理的**未跟踪私有文件**，填写两个不同的随机网关令牌，每个至少32个ASCII字符。另建私有文件，只写现有 Pixiv 后端 API Key，用其绝对路径设置 `PIXIV_API_KEY_FILE`。Compose 只向 Pixiv 网关挂载该 secret；网关读取后只传入 Pixiv 子进程，cap 不获得该密钥。Linux 上文件需要对容器 UID 10001 可读，父目录和宿主权限应由管理员控制。不要执行会把真实凭据输出到终端或日志的配置转储。

`PIXIV_MCP_READ_ONLY=true` 默认隐藏 Pixiv 所有写工具。若明确需要归档、订阅等操作，改为 `false` 并重建该服务；MaiLivingWorld 自己的主人身份、工具分类和写入开关仍需分别通过。

## 构建、启动与 MaiBot 配置

从项目根目录运行，替换私有环境文件的实际路径：

```sh
docker compose --env-file /absolute/private/path/mcp.env -f deploy/compose.mcp.yml build
docker compose --env-file /absolute/private/path/mcp.env -f deploy/compose.mcp.yml up -d
```

在 MaiBot 容器中，将 `MLW_CAP_MCP_TOKEN` 设置为网关的 `MLW_CAP_GATEWAY_TOKEN` 值，将 `MLW_PIXIV_MCP_TOKEN` 设置为 `MLW_PIXIV_GATEWAY_TOKEN` 值。这是两个部署环境中的显式映射；插件默认 `token_env` 分别为 `MLW_CAP_MCP_TOKEN`、`MLW_PIXIV_MCP_TOKEN`。插件两个提供者使用 HTTP transport，各自 `url` 填上方完整地址。令牌值不能写在 URL、对话或工具参数中。网关不需要向模型暴露任何 shell 命令；子进程 argv 仅由管理员 Compose 配置提供。

默认禁止所有带 `Origin` 的请求，适合服务器间调用。确需已知浏览器客户端时，可由管理员给相应服务配置 `MLW_MCP_GATEWAY_ALLOWED_ORIGINS` 的 JSON 精确名单；不支持通配符。若修改服务网络别名或端口，相应更新 `MLW_MCP_GATEWAY_ALLOWED_HOSTS` 精确名单。

健康检查 `/health` 只报告网关进程状态，不验证 Pixiv 认证、远端网络或 worker 可用性。首次应由主人私聊执行一次只读查询验收。`queued` 只是受理，成功必须继续查看后端任务的终态。

## cap 持久化与文件

`mailivingworld_cap_mcp_data`（可用 `MLW_CAP_DATA_VOLUME` 修改）保存独立的 `/var/lib/cap/tasks/tasks.sqlite3`、图片、PDF和接收文件。重建容器不清空该卷。它不复用原 cap 实例的任务数据库；**同一卷只允许一个 cap 网关实例、一个 uvicorn worker，不要扩容副本**。源项目也会通过文件锁拒绝任务库的多写者。

cap 返回的本地 PDF 路径属于网关容器。MaiLivingWorld 会隐藏这些路径。v0.14 可同时启用网关环境 `MLW_CAP_PDF_READ_ENABLED=true` 与插件 `reading.enabled`、`reading.cap_pdf_enabled`，通过 `living_world_reading` 阅读已有 PDF 单页。解析仅访问 `/var/lib/cap/downloads`，拒绝越界路径和链接；默认最多64MiB/1500页/20秒，每页不超过2百万像素/JPEG96KiB。无需给 MaiBot 挂文件卷，不自动发送 PDF。若另有经管理员配置的文件处理插件需要访问文件，可在 MaiBot 自己的 Compose 中显式把同一卷只读挂载到同一 `/var/lib/cap` 路径，并按那个文件插件的要求限制可读目录；仅挂载不会自动增加 MaiLivingWorld 的文件能力：

```yaml
services:
  maibot:  # 替换成真实服务名，合并到它已有配置
    volumes:
      - cap_mcp_files:/var/lib/cap:ro
volumes:
  cap_mcp_files:
    external: true
    name: mailivingworld_cap_mcp_data  # 与 MLW_CAP_DATA_VOLUME 一致
```

本配置默认不改 MaiBot 的挂载。另一种方式是连接已经部署的 cap 文件服务：同时设置 `JM_FILE_SERVER_URL` 与 `JM_FILE_API_TOKEN`，按需设置 `JM_FILE_DOWNLOAD_BASE_URL`。签名下载地址属于受限结果，不能写入公开记忆。没有这些配置时，cap 不注册上传/接收工具。

当前 `living_world_pixiv_image` 仅将单张受控图片作为 `content_items` 交给模型观察，不自动发送到 QQ。该功能还要求 MaiBot 容器配置独立可达的 `PIXIV_MCP_BACKEND_URL` 和 `PIXIV_MCP_API_KEY`；这是图片获取端需要的凭据，不会从网关的 secret 挂载自动复制。MCP 网关本身只转发结构化工具结果，不承载匿名图片代理。

## 边界与排错

网关主要暴露本仓库 `mcp_policy.CATALOG` 与实际上游目录的交集，另可显式启用固定 `living_read_pdf_page`，输入 schema 保持原样，工具描述使用本仓库固定说明，不转发上游 instructions。错误仅返回固定状态，已知凭据和原始异常正文被屏蔽。每个调用不会自动重试；超时后先确认任务状态。连接退役后，后续独立请求会先有界关闭旧子进程，再创建一个新实例并重新发现工具；并发请求不会产生多个 cap 写者，也不会重放此前结果不确定的调用。旧进程不能及时退出时，此次恢复失败，需要检查并重启对应服务。

401检查两端Bearer令牌；421检查精确Host名单；403检查Origin；缺少写工具检查Pixiv只读模式；工具错误检查后端/worker状态。网关日志不记录工具参数、返回正文、访问头或密钥。

此交付仅提供源码、配置和离线测试；没有启动实际容器、访问真实账号或替换用户凭据。
