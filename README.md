# MaiLivingWorld · 麦麦的小世界 v0.29.0

> MaiBot WebUI 可上传 ZIP 安装。新闻、百科和可选的 Mastodon 账号工具随插件运行。CAP 和 Pixiv 通过各自的 Docker 网关连接。首次安装时，外部工具和主动联系均需手动开启。


## 第一次使用

在 MaiBot WebUI 打开 **插件设置 → MaiLivingWorld → 快速开始**，选 **日常陪伴**，填你自己的 **QQ 号**，然后保存。首次安装用默认的 SQLite 即可。你可以私聊发 `/相处 记住 我们约好周末讨论这本小说`，下次聊到这本书时看看她是否接得上。

| 使用方式 | 适合什么时候选 |
|---|---|
| **日常陪伴** | 先从私聊开始。她以伴侣风格回应，可以记住你明确留下的回忆、称呼和聊天偏好。 |
| **陪伴与成长** | 在日常陪伴之上，允许她在后台调用模型，从聊天记录整理事件、事实与近况，并在证据足够时逐渐形成兴趣与观点。这会额外消耗模型额度。 |
| **详细配置** | 已经调好自己的插件，或希望自己控制每一项。升级时默认使用这项。 |

快捷方案只在运行时组合相处和学习开关。其他标签仍显示手动保存的值，切回「详细配置」即可按这些值运行。主动发 QQ 消息、群名单、外部工具及数据库分别按你原有的配置运行。需要群聊也作为学习来源时，选「陪伴与成长」，到 **记忆与成长 → 聊天记忆与群名单** 填允许的群号。

## v0.29.0 · 查新闻、自己上网和参与讨论

**想让她回答一个问题**，在「快速开始」选「日常陪伴」或「陪伴与成长」，填主人 QQ 并保存。到 **阅读与工具 → 12 / MCP 工具与自主查阅** 打开 **启用 MCP 工具** 和 **查新闻和百科**，保存。在主人私聊问“最近有什么太空新闻？挑一篇读给我听，带上出处和日期。”她可以查看联合国中文新闻、IT之家近期条目，打开报道，也可以用中英文维基百科查背景。新闻工具会说明实际取得的是原文节选还是订阅摘要。这两项可直接在 MaiBot 容器运行，无需申请账号。

**想让她自己逛**，把同一页的 **自主上网** 设为 **只逛逛**。她会定时按兴趣选题、决定要不要点开某条，也可以什么都不选。想让她从更广的网页中搜索并打开结果，可以为 MaiBot 配置可选的 Brave Search API 密钥。

**想让她公开回复**，为她注册独立的 Mastodon 账号，将实例和令牌放进 MaiBot 容器。选 **参与讨论**，到 **运行与存储 → 01 / 基础与安全** 关闭 **安全演练** 并保存。首次安装时同页的 **主动 QQ 联系** 默认关闭，只想让她在外站活动时保持关闭即可。她会先读公开原帖，觉得有话可说时才用自己的账号回复。主人也可在 QQ 私聊发 `公开回复 帖子编号 正文` 指定一条原帖和准确措辞。每个账号每天最多尝试两条，对同一原帖最多一次。需要处理账号或权限时，她会在主人私聊里提起，处理后可回 `外站问题已处理` 关闭待办。账号和容器配置见[部署步骤](deploy/README.md)，可照着[验收示例](deploy/EVALUATION.md)分别试读和回复。

## v0.28.0 · 聊聊刚发的图和视频

在 MaiBot 的模型配置中给 `vlm` 任务选一个支持图片输入的模型。完成「快速开始」后，打开 **阅读与工具 → 连续阅读与学习 → 看聊天里的图片和视频**，保存。现在可以在主人私聊发一张照片，问她“你最先注意到什么？”。要让她也接群里的图和链接，到 **记忆与成长 → 聊天记忆与群名单** 填允许的群号。

她会根据实际收到的图片、视频抽样帧或 B 站官方分镜接话。分享多分 P 视频时会选链接指定的那一 P，能取到公开字幕时还可结合字幕节选。只有标题和简介时，就只按链接资料介绍。画面抽样和字幕节选各有范围，涉及声音、对白细节或结局时需要另外提供依据。

同一个聊天里只有一张近期图片时，“刚才那张图”可以接上。接连分享几张时，可以用 QQ 的“回复”引用想聊的那张。指代含糊时，她会先问你指哪张。观察摘要也进入统一记忆，按当前听众决定能表达的内容。「陪伴与成长」可把有依据的观察作为后续学习材料。此开关默认关闭，启用后宿主会把选取的画面交给所配置的视觉模型，并使用模型额度。[使用说明](deploy/COMPANION.md) · [连续对话验收](deploy/EVALUATION.md)

## v0.26.0 · 记得自己跟你提过什么

主人问起群里聊过的摄影展时，她可以接着用自己学到的资料。第一次跟主人私聊这件事，会给对方足够的背景；只有核实她确实私聊发过同一版内容，回复模型才得到“我向你提过”的依据。消息里谈到谁、谁当时在群里，都不能当作谁说过或谁读过的证据。

统一心智使用 `active` 模式时，开启 `reference.enabled` 即可记录提及。最近30天最多保留32份来源版本的指纹，换窗口和重启后仍可承接。主人私聊发送 `/话题 清除` 可以清掉这份记录。见[伴侣使用说明](deploy/COMPANION.md)。

## v0.25.0 · 把已有能力协调成持续的相处

新增可选的回复协调：依据当前已核验的对话目的，选择这一轮的表达重点。倾听时暂缓无关建议和任务回告，明确求建议时提供相应帮助，询问进度时依据实际记录回应；原身份、相处边界、事实修正、共读保护和必要澄清继续保留。这个选择不修改记忆或任务状态，不额外调用模型。

新增有事件对象的短期感受：成长的原提案及独立复核可记录对具体互动、观察或已读材料的有限反应，同一事情可以同时欣赏和失望。保留原始依据，按时间衰减，相关话题才进入原统一表达检查；不会把主人自己的心情、沉默或缺席变成她的负面情绪，也不把感受当外部事实或新的学习证据。

建议和自身表达修复可以将超出活跃窗口的旧项归档，保留版本、来源和遗忘指纹，并按相关词取回或分页检索。启用后64条限制作用于活跃窗口，忘记后的指纹不占活跃名额；其它记忆账本仍遵守各自边界，归档不会抹去正文或改变表达权限。

四个新开关 `attention.reply_coordination`、`growth.emotional_episodes`、`advice.archive_enabled`、`repair.archive_enabled` 均默认关闭。30节197字段、95文件发布包，沿用二十一SQLite库/82张MySQL表，无新增DDL；v25备份清单兼容v14–v24。启用及真实Docker/QQ验收见 [长期伴侣说明](deploy/COMPANION.md)、[连续对话验收](deploy/EVALUATION.md) 和 [备份说明](deploy/BACKUP.md)。

## v0.24.0 · 对自己说过的话负责

新增可选的表达修复：主人明确指出她哪一句说错、应该怎样更正时，可以将这次纠正保存下来，今后相关私聊中提醒她注意。依据必须来自实际成功发给主人的纯文本，以及主人本轮的逐字纠正；经过两阶段复核才入账，含糊的“你错了”不会被补成具体事实。修复保留主人报告的性质，不进入知识根或性格成长，也不自动改写其它记忆。

出站全文的有效核对窗口最多24小时、最近12条、单条1200字；超长消息直接跳过，过期正文在后续运行维护时清理。长期修复最多64条，可撤回或忘记，遗忘会清除相应正文并保留防重放记录。她可以在相关话题中改变表达，但记录存在不等于已经道歉、得到原谅，也不能保证任意语义下永不再犯。

`[repair]` 默认关闭，需已绑定唯一主人并启用统一心智active。配置30节193字段、92文件发布包、二十一SQLite账本/82张MySQL表；新增 `repair.sqlite3` 四表。保留原持久卷，升级和实际验收见 [长期伴侣说明](deploy/COMPANION.md)、[连续对话验收](deploy/EVALUATION.md) 和 [备份说明](deploy/BACKUP.md)。

## v0.23.0 · 真实活动有连续性，性格同时带来长处与局限

新增可选的活动感知：只有实际完成并获接受的后台阶段进展才增加有限注意力负荷，随时间恢复；负荷较高时暂缓新的后台探索，主人当前消息仍照常回应。活动状态复用原注意力账本，换会话和重启不会凭空重置。主人询问她在忙什么时，只能根据近期、仍有效的实际活动记录说明阅读、查阅或项目进展，不能把一个阶段说成整件事已完成。

新增可选的性格取舍表达：沿用已有缓慢成长的好奇、社交、耐心和谨慎倾向，允许它们同时表现出长处与局限，例如愿意探索但容易展开旁枝、认真斟酌但需要时间收束。当前具体请求和相处边界优先；不会为模拟缺点而故意答错、随机发脾气或编造经历。

`attention.activity_awareness` 与 `growth.personality_tensions` 均默认 `false`，不增加内部模型调用。配置29节189字段、89文件发布包，沿用二十SQLite账本/78张MySQL表，无新增DDL；实际状态仍须保留原持久卷。启用、范围与真实宿主验收见 [长期伴侣说明](deploy/COMPANION.md) 和 [连续对话验收](deploy/EVALUATION.md)。

## v0.22.0 · 有依据地改变看法，也接住眼前的好消息

新增可选的观点复核：把相关旧观点的完整原始依据与新证据一起交给成长提案和独立复核。遇到一条反例先保留分歧；一致的修订候选需至少两条去重后的独立新来源，并在后续批次继续获准，才替换原看法。信心只作有限调整，不代表事实准确率。表达时区分暂时看法、尚有分歧和已经记录的前后变化，不编造改变原因。

当前主人明确说“我考试通过了”“我拿到offer了”时，可当轮接住好消息，覆盖此前的倾听/安慰语气；下一条普通消息恢复原偏好。识别仅覆盖有限中文完整表达，明确求建议、纠正、倾诉与正负混合内容优先，不把引述、假设或第三人的消息当本人喜讯。此回应无需额外模型调用，也不保存长期情绪。

`growth.review_beliefs` 默认 `false`；启用后复用成长每批最多两次模型调用和原共享预算。喜讯回应沿用已启用的 `attention.dialogue_enabled`，没有独立新开关。当前配置29节187字段、88文件发布包；仍为二十SQLite账本/78张MySQL表，无新增库表，保留v14–v21备份清单。启用与真实宿主验收见 [长期伴侣说明](deploy/COMPANION.md) 和 [连续对话验收](deploy/EVALUATION.md)。

## v0.21.0 · 记得建议有没有帮上忙，接住刚才的话题

新增建议效果跟进：实际发出的建议才建立跟进，主人明确报告后分别记录未尝试、尝试过、有帮助或没帮助。自然反馈须能唯一定位建议，并经原话与独立复核；沉默、未来打算或一句“听起来不错”不算成功。相关结果可以影响之后的建议，但仍是本人的一次反馈，不是普遍结论，也不自动改变性格。

新增有依据的话题承接：成功发出的内容若与允许表达的原始记忆存在明确逐字锚点，就保存有限时效的话题引用。换会话或重启后，“那件事”“后来呢”“继续说”等短句可以续接；来源失效、话题不唯一或尚未确认谈过时先澄清。新话题和清除使旧焦点失效，不凭模糊话宣布事情完成。

`[advice]` 与 `[reference]` 默认关闭，需已绑定唯一主人并启用统一心智active；不会自动联网或主动发消息。配置29节186字段，二十SQLite账本/78张MySQL表，v14–v20备份清单兼容。开启和实际验收见 [长期伴侣说明](deploy/COMPANION.md)。

## v0.20.0 · 有自己在琢磨的事，也记得一起读到哪里

新增她自己的有限项目：由已有自身兴趣和相关问题选题，利用已授权的本地 Pixiv 库查阅或继续已有小说，分阶段取证、整理和独立复核。结果可以是暂时结论，也可以是资料不足；不会把元信息当正文、把自己的总结重新当证据。当前主人私聊优先，进度、预算和暂停/停止状态跨重启保留。

新增共同阅读：分别保存她取回的内容、你亲自确认的阅读位置、实际成功发出的讨论位置。你可回退、停止或清除进度；默认不从插件工具或已关联的记忆中透露后文。防剧透覆盖本插件可识别的来源与出口，不保证宿主已有聊天或模型本身的知识不会透露情节。

`[projects]`、`[shared_reading]` 默认关闭。项目需要统一心智和成长 active 以及相应查阅权限；共读位置只能由可信主人私聊指令确认。配置、命令与验收见 [长期伴侣说明](deploy/COMPANION.md)。本版27节180字段、十八SQLite账本/72张MySQL表；原有Docker MCP服务继续复用，旧备份清单保持兼容。

## v0.19.0 · 相处有习惯，答应的任务有下文

新增长期沟通习惯：区分默认、工作、技术、倾诉和兴趣情境，保存篇幅、倾听/建议、追问频率三个维度。主人明确长期要求经原话与独立复核后生效；普通反馈须三条不同消息跨至少一天，不能因一次不满或沉默改变习惯。当前请求、必要解释和已确认相处边界优先，主人可用完整指令设置或清除。

新增已有MCP任务跟进：先核验本插件实际发起的任务与服务来源，成功送达承诺后才查询状态；结果成功送达才算已回告。仅支持cap下载/传输和Pixiv归档/动图预览/同步任务，不把自由聊天里的未来打算当作已执行的任务。不会自动创建或重试写入；主动回告沿用白名单、安静时间、联系配额与暂停设置。

`[habits]`、`[followthrough]` 默认关闭，各自独立启用；习惯需要统一心智active，任务跟进另需已启用的MCP和相应操作权限。新增两个只读查询/跟进工具，详情及实际验收见 [伴侣使用说明](deploy/COMPANION.md)。25节175字段，十六个SQLite账本/66张MySQL表；v14–v18固定备份清单继续兼容。

## v0.18.0 · 长期更正、回忆和少重复追问

主人明确纠正旧说法后，经逐字证据核对与独立复核保存更正；以后回忆和成长使用有效版本，保留原记录与审计。更正只替换命中的唯一片段，不吞掉其它独立事实；旧来源变化则暂停采用，更正可由主人本人撤回。新增相关词检索和明确事件时间排序，不替含糊的话猜测事件。

成功发送给主人、且能核验目标与话题的问题才记录为“问过”，默认24小时提示减少同一版本的重复追问。共享模型预算默认预留12次给前台内部理解与表达判断，后台不能消耗这部分额度。新增多轮合成情境和可接真实部署回复链的验收脚本，实际模型效果需要人工评估。

新增 `[recollection]` 默认关闭，统一心智active后可显式启用；`living_world_recollection` 仅可信主人私聊只读检查。配置23节168字段，十四库58张MySQL表，旧备份清单兼容。见 [伴侣使用与验收](deploy/COMPANION.md)、[连续对话验收](deploy/EVALUATION.md) 和 [状态备份](deploy/BACKUP.md)。

## v0.17.0 · 统一关注、对话节奏与误会修复

新增统一注意力控制：已启用的持续阅读与问题查阅每轮只选择一项，关注与进度跨重启保留；收到可信主人私聊时先让位，稍后可以接续。选择参考当前有效兴趣与好奇倾向，同一任务连续推进两次后轮换，失败暂让其它任务先行。已有的工具权限、来源检查和各项预算继续生效。

短时对话判断把自然长句分为倾听、建议、纠正理解或轻松交流；普通寒暄与无关知识问句不额外分类，明确短句零模型，其余符合线索的消息最多一次共享预算调用。只输出固定风格提示，不把模型给出的解释写成长期记忆，也不更改主人身份。最新明确要求优先于历史倾听/建议设置。

面板新增「此刻的关注」，主人私聊可用只读工具 `living_world_attention` 查询状态。新增三表 `attention.sqlite3`，总计十三库/54张MySQL表。`[attention] enabled=true` 且统一心智active才启用；各阅读、MCP和成长开关需独立打开。未自动修改实际配置。具体边界与多轮验收见 [长期伴侣使用说明](deploy/COMPANION.md)。

## v0.16.0 · 自然近况、接话和给对方空间

自然私聊里的“明天要考试，有点担心”现在可成为待续话题：后台保留逐字证据、独立复核，之后聊到相关事情或说“我回来了”时可自然续接。事情结束或不想再提时，按新原话核对状态；不会把聊天近况变成代主人接受的计划或提醒。

- **互动方式**：“先听我说就好”“我有点难过”切到短时倾听；“帮我想想办法”切到建议，随后明确求助仍以本轮问题为准。状态到期恢复，不记录诊断。
- **联系节奏**：“先去忙”“晚安”暂停主动打扰，普通回复照常。回来可解除忙碌/休息暂停，不能撤销明确的暂不联系；恢复主动联系需明确表达，仍受白名单、静默时段和配额限制。
- **连续近况**：最多合并四条可信主人消息，每批最多两次模型调用，默认每日六批；使用共享模型预算，失败和重启不重置。仅核验通过的原话进入统一记忆，不把模型摘要当成原始证据。
- **可查看、可遗忘**：只读 `living_world_continuity` 查询状态与话题；主人可发送 `/近况 忘记 <ID> <版本>` 清理该近况正文并使相关引用失效。面板增加“最近放在心上的事”。

新增 `continuity.sqlite3` 四表，总计十二库/51张MySQL表，旧SQLite备份保持明确清单兼容。`[continuity].enabled` 默认关闭；自动整理另需统一心智active，关闭 `auto_topics` 可停止提取并保留现有近况的读取。完整设置与例句见 [长期伴侣使用说明](deploy/COMPANION.md)。

## v0.15.0 · 长期伴侣、相处约定和回忆

后续功能围绕长期 AI 伴侣与更自然的相处展开：同一个持续的角色、唯一主人、自己的爱好和看法、可续接的共同经历。新增 `owner.relationship_mode="partner"`，私聊自然亲昵，群聊保持分寸；主人明确要求停止亲昵时立即生效。聊天不靠固定撒娇台词、关系积分或因沉默降级维持。

- **持久相处约定**：可信主人私聊说“以后叫我小明吧”“别叫我主人”“不要撒娇”，或发送 `/相处` 完整指令，可跨会话/重启保存称呼、亲昵偏好和边界。模型不能代主人更改。
- **回忆与纪念日**：主人主动记录自己的回忆、明确完整日期；统一联想仍保留来源与听众限制，不虚构共同线下生活。停用回忆会让引用和在途旧结果失效。
- **自然表达**：成长出的兴趣和观点在表达许可后作为自身倾向与看法呈现，不把内部权重说成台词；实际时区只提供语气节奏，不推断生活安排。
- **相处面板与备份**：管理页展示约定、回忆与纪念日。新增 `bond.sqlite3` 三表，总计十一库/47张MySQL表；SQLite旧十库备份可恢复并报告新库缺省，MySQL跨结构恢复需先在匹配旧结构还原再升级。

新增功能默认关闭。配置、指令、边界与实际QQ验收步骤见 [长期伴侣说明](deploy/COMPANION.md)。使用既有成长/阅读能力仍需相应开关与来源数据；新增关系账本不自动启用主动联系或MCP。

## v0.14.0 · 连续阅读、共同计划和成长面板

- **连续阅读**：`living_world_reading` 支持 Pixiv 缓存小说、插画和 cap 网关已有 PDF。`open` 建立固定版本的阅读，`next` 读取下一段/页，`list` 查看进度，`notes` 查看核验笔记，`review` 重试待核验文字笔记，`resume` 重新检查后恢复暂停任务。小说按 Unicode 字符、PDF/插画按页推进。来源撤回或版本改变使记录暂停；同版本重新获取而来源指纹变化时建立新阅读，保留旧记录。
- **阅读学习**：文字笔记需逐字引用原文并独立复核，失败或预算不足保留 pending。真实原文进入统一工具记忆，后续成长仍受证据、变化上限与复核约束，不把小说当现实事实。图片通过 Host `content_items` 交给当前模型，后台文字笔记不冒充视觉理解。`auto_continue` 只继续已开始的小说，不自行下载；后台次数及模型/MCP预算跨重启保留。
- **共同计划**：`living_world_shared_plans` 只能起草和列出计划。主人私聊发送 `/共同计划 接受 <计划ID> <当前版本>` 后生效；`暂停`、`继续`、`完成`、`取消` 使用同样格式。模型不能代替主人提交状态变更。`due_at=0` 不提醒；开启跟进后，到期计划进入已有主动聊天流程，仍受白名单、静默、免打扰、拒绝联系与配额限制。
- **成长面板**：`webui.json` + `webui/index.html` 使用 Host 自定义页面协议 v2，经登录管理端查询 `living_world_dashboard`。展示身份连续性、性格/情绪、带来源的成长结论、阅读/计划进度和预算；第三方私人内容只统计、不输出正文。需要支持 `custom_pages` 的 Host；旧 Host 可调用同名管理 API，不能仅凭 SDK 版本断言页面可用。
- **备份恢复**：`scripts/backup_state.py` 提供 SQLite/MySQL 备份、校验与空目标恢复演练。恢复默认只检查，执行另需 `--apply --confirm RESTORE_EMPTY_TARGET`；不覆盖运行数据、不打包配置密钥。数据库备份含私人记忆，须妥善保存。具体参数见 `python scripts/backup_state.py --help`。

新增 `companion.sqlite3` 或五张 MySQL `companion_` 表，共十套账本、44 张 MySQL 表；旧备份没有新库时可省略迁移。`[reading]` 和 `[shared_plans]` 的所有功能开关默认关闭。cap PDF 还需网关环境 `MLW_CAP_PDF_READ_ENABLED=true` 和插件 `reading.cap_pdf_enabled=true`，无需给 MaiBot 挂载 cap 文件卷。PyMuPDF 仅安装在 cap 网关环境。

主动聊天的 `general.dry_run` 不替代 MCP、阅读或计划的独立开关。显式启用阅读/计划后会保存进度与确认记录；它们不会直接发送 QQ。备份和恢复具体步骤见 [备份说明](deploy/BACKUP.md)，实际备份前须停止所有写入者以保证跨库一致性。

保存的是可检查的状态、证据和短篇结论，不保存模型隐藏思维链，也不代表模型具有人类意识。

## v0.13.0 · 工具、带来源的学习与 Docker 接入

本版把 cap 和 Pixiv 缓存后端作为同一智能体的工具入口，新增工具观察账本、未解问题查阅和主动联系的统一表达判断。它们与 QQ 私聊、群聊和已有资讯共用内部记忆；来源场景决定表达限制，不把不同窗口拆成不同人格。

- **固定工具目录**：模型先发现工具，再按参数 schema 调用。工具只来自管理员配置的 cap/Pixiv 服务与本插件允许目录的交集，远端说明或聊天文字不能授予新权限。
- **逐项开启**：MCP、两个服务、群聊读取、写入、账户操作、文件传输、图片读取和后台研究都默认关闭；`owner_only=true`。`remember_results=true` 仅在 MCP 已启用且调用成功时生效。账户操作和文件传输还需同时打开写入总开关。
- **工具结果成为观察**：可保存作品元数据与已读取的小说片段，附稳定来源、版本、时间和原场景。任务路径、凭据、签名链接和模型转述不入观察库；小说内容明确标为虚构文本。重复查阅不会伪装成新的独立经历，来源变化或撤回使旧派生结论暂停使用。
- **有限自主查阅**：有效的未解问题可以轮流查询 Pixiv 的已有本地资料库；每轮最多一次检索和一次详情读取，不自动联网补库、归档或执行写工具。问题游标、间隔、日配额和失败退避持久化。新观察交给后续成长循环处理，不保证这一次就解决问题。
- **主动联系统一表达**：统一心智 active 时，主动联系也经内部联想和表达审核，替代旧 Brain/Mind 直接注入。群聊主动联系没有当前发言者，不把某个群成员当成主人；原主动联系名单、免打扰、配额及主人拒绝状态继续生效。

### Docker 部署与配置

主插件可用 WebUI ZIP 安装。要使用 CAP 或 Pixiv，另部署对应的 `cap-mcp`、`pixiv-mcp` 网关。Pixiv 原 HTTP 后端和 worker 继续沿用现有部署。本轮对接源码为 cap `feat/jm-mcp` 的 `58a555e`、pixiv-cache-backend `codex/mcp-server` 的 `55397d2`。构建前请在两个目录核对这些包含 MCP 的分支。

完整步骤见 [Docker MCP 网关部署](deploy/README.md)，包括同一 Docker 网络、源码构建上下文、独立 cap 持久卷和 Pixiv 后端密钥。网关默认不发布宿主端口、不挂 Docker socket；容器内的 `127.0.0.1` 不代表其他服务。

MaiBot 1.3.10 的插件沿用宿主自带的 MCP 1.x（1.28.1 起）。两个 Docker 网关在自己的镜像内使用 MCP 2.3，通过 Streamable HTTP 与插件通信。已部署在兼容 MCP 2.3 环境中的客户端也可以继续使用。插件的 JSON 校验、MySQL 和媒体依赖随安装清单声明。

先绑定可信主人、启用统一心智和需要的输入名单，再逐项合并；不要覆盖现有 QQ 身份、数据库或其他配置：

```toml
[mcp]
enabled = true
owner_only = true
allow_group_reads = false
allow_writes = false
allow_account_actions = false
allow_file_transfer = false
media_enabled = false
research_enabled = false

[mcp_cap]
enabled = true
transport = "http"
url = "http://cap-mcp:8000/mcp"
token_env = "MLW_CAP_MCP_TOKEN"

[mcp_pixiv]
enabled = true
transport = "http"
url = "http://pixiv-mcp:8000/mcp"
token_env = "MLW_PIXIV_MCP_TOKEN"
```

`token_env` 填的是 **MaiBot 容器内环境变量名**，不是密钥正文。Compose 示例的 `MLW_CAP_GATEWAY_TOKEN` / `MLW_PIXIV_GATEWAY_TOKEN` 是各网关的令牌来源；在 MaiBot 容器中将同样的值分别提供为 `MLW_CAP_MCP_TOKEN` / `MLW_PIXIV_MCP_TOKEN`，或明确把 `token_env` 改成实际变量名。它们与 Pixiv 后端 API Key 是不同凭据，不写进聊天、URL、仓库或 TOML。插件不自动读取其他项目的 `.env`。

若开启图片读取，MaiBot 容器还需单独提供 `PIXIV_MCP_BACKEND_URL` 和 `PIXIV_MCP_API_KEY`；后者仅通过 `X-API-Key` 发给固定后端，不能拿网关 Bearer 令牌代替。默认最多读取一张 8 MiB 的当前版本缓存图，仅支持 JPEG/PNG/WebP/GIF，核对索引、页码、MIME 和文件签名，拒绝跳转与任意路径。图片交给模型观察，**不自动向 QQ 发送**，也不把图片或模型描述自动变成已证实记忆。

HTTP 是 Docker 默认选项；同一环境可改用 `transport="stdio"`，由管理员填写独立解释器绝对路径、参数数组、工作目录和 `env_names`。cap 使用根目录 `mcp_server.py`，Pixiv 使用 `-m app.mcp`；只透传明确列出的现有环境变量，不从聊天提供 shell 命令。容器模式通常无需配置这些 stdio 字段。

### AI 工具与管理 API

| 入口 | 用途 |
| --- | --- |
| `living_world_mcp_tools(provider)` | 按当前身份列出已启用的 CAP、Pixiv、新闻、百科或 Mastodon 读取工具及参数 |
| `living_world_mcp_call(provider, tool, arguments)` | 按已发现 schema 查询、创建任务或检查任务状态 |
| `living_world_pixiv_image(pixiv_id, page_index=0)` | 主人私聊读取插画类作品的一张当前缓存图供模型观察，需额外媒体开关 |
| `living_world_mcp_status()` | 管理端查看连接、调用和研究计数，不返回查询正文或密钥 |

例如先发现 `pixiv` 目录，再调用 `get_library_work`，参数形如 `{"pixiv_id":12345678,"kind":"illust"}`（编号仅作示意，替换为明确目标）；小说正文需另调用 `read_novel_excerpt` 并使用偏移分页。查到标题不代表读过正文，读过一段不代表读完整本。cap 的查询从 `jm_get_album` 开始，下载后查看本插件创建的任务状态；`queued`/`running` 不代表完成。

QQ 互动工具使用宿主绑定的真实消息和发言人身份。群聊需要准确的回复目标和允许的群号。开启群读后，可读固定作品资料和已启用的公开新闻、百科与网页来源。CAP/Pixiv 的写入、账号与文件操作只接受获准的主人私聊调用，任务详情限该主人通过插件发起的任务。Pixiv 网关默认 `PIXIV_MCP_READ_ONLY=true`，使用写入时需要在服务端和插件端分别启用权限。Mastodon 公开回复使用「参与讨论」的专用流程。

`mcp.max_daily_calls` 持久限制工具与图片读取次数，失败也计数；`unified_mind.max_daily_model_calls` 限制内部模型与成长调用，两种预算分开。一次图片观察需详情读取与图片读取两个额度。相同消息的重复请求不会自动重试有副作用操作；超时或断连可能已经入队，先核实任务终态。

`general.dry_run` 控制主动 QQ 联系的演练，也阻止 Mastodon 实际公开回复。CAP/Pixiv 使用各自的写入授权，显式启用后可能创建真实后端任务。做只读验收时保持 `allow_writes=false`。

### 工具记忆、研究和保留边界

新增第九个 SQLite 库 `tool_memory.sqlite3`，MySQL 对应 `tool_observations`、`tool_calls`、`tool_tasks`、`tool_research`，统一 DDL 现为 **39 表**。工具观察进入统一检索，来源默认保留调用场景的个人属性；本地缓存不自动等于公开知识。后续成长仍须验证原始来源及其版本，模型自己的摘要不能成为自证来源。

后台查阅还需 `growth.enabled=true`、`growth.mode="active"`、统一心智、MCP、Pixiv 服务与 `remember_results` 均启用。它会把问题主题送到管理员配置的本地缓存服务检索，不调用公共搜索引擎，不自动看图或读取小说全文，也不扩展到无限规划。仅有问题但库中没有相关资料时，可以持续未解决。`research_daily_limit` 默认 6 轮，`research_interval_seconds` 默认一小时；`general.enabled` 控制主动聊天，不是 MCP 或学习总开关。

所有新能力默认关闭，未替换用户运行配置或真实账号凭据。验证使用临时数据库、离线模型/工具响应及模拟网络边界；真实容器、QQ、账号后端和模型效果仍需部署验收。持续状态与有限学习循环不等于人类意识、模型权重训练或隐藏思维链恢复。

以下版本节保留历史演进；当前安装、配置和数据库数量以本文开头 v0.24.0 及现行部署/迁移说明为准。

## v0.12.0 · 自身爱好、观点、性格和可恢复学习

在 v0.11.0 的唯一主人和统一记忆上，增加长期成长循环。QQ 私聊、群聊、已接入的公开资讯共同提供证据；不同窗口仍共用同一个自我。它保存结构化状态和待处理任务，不训练模型权重，也不保存模型隐藏思维链。

- **自身兴趣与观点**：兴趣权重可增减，观点遇到新证据可修订；与“用户喜欢什么”分开。知识始终标为待验证推断，问题可保持未解决并在后续学习时继续检索。
- **缓慢性格**：好奇、社交、耐心、谨慎四个维度，每次最多变化 0.01，需要至少三条内容不同的来源记录并通过独立模型复核；每天每维累计绝对变化不超过 0.03（按 UTC 日期）。重复或改写同一来源不会再次推动兴趣、性格或情绪。
- **短期情绪**：情绪和精力独立保存，以六小时半衰期逐渐恢复中性。长期倾向通过固定表达风格影响回复，不向外解释私人原因。
- **持久来源约束**：每条兴趣、观点、知识和问题保存证据 ID 与版本指纹。来源删除、过期、被撤回或改写后，旧推断暂停使用；私人来源不会因为形成摘要就自动变成公开材料。
- **两次校验**：模型先提出有限更新，再独立复核。学习和回复共用持久每日调用预算；学习不会更改主人、增加主动发信次数或执行任意工具。
- **恢复和回滚**：任务先入库，再领取有期限的处理租约。重启后重试未完成任务；失败退避，最多三次，预算不足不消耗重试次数。成长状态、来源和修订历史同事务提交；管理员可回滚最近一次成长。

### 配置

先按下面 v0.11.0 的说明绑定主人、开启统一心智和需要的记忆来源，再合并：

```toml
[growth]
enabled = true
mode = "observe"        # 只验证任务；改为 active 才持久更新成长状态
interval_seconds = 3600
```

要让成长影响对话，还需 `unified_mind.mode="active"`。只有来源全部公开且仍有效的自身兴趣，会影响已有资讯选题；私人兴趣可参与内部理解，并经当前场合的表达判断使用。`unified_mind.max_daily_model_calls` 同时覆盖联想、表达复核、成长提案和成长复核，通常一次学习需两次模型调用。旧 Brain/Mind/Cognition 各自的调用预算保持原配置。

状态 API 均为管理端内部接口：

| API | 用途 |
| --- | --- |
| `living_world_growth_status()` | 模式、修订号、队列计数、最近结果；无私人正文 |
| `living_world_growth_profile()` | 当前仍有有效来源的档案，可含敏感推断 |
| `living_world_growth_history(limit=20)` | 成长前后版本与来源审计，可含敏感内容 |
| `living_world_growth_rollback(expected_revision, confirm="ROLLBACK_GROWTH")` | 按当前版本回滚最近成长；不重置主人、证据去重和当日变化额度 |
| `living_world_growth_rebuild_item(key, expected_revision, confirm="REBUILD_GROWTH_ITEM")` | 显式废弃某条失效推断，保留历史，供后续按新证据重新形成 |

同一档案的来源依赖只合并、不自动丢弃。原来源改版或累计超过 24 条时，不会偷偷替换证据以恢复表达；管理员可先在历史中检查原因，再用重建接口废弃该条旧推断。重建不清空整个记忆，也不重新消费已经使用过的证据。

SQLite 备份新增 `growth.sqlite3`，连同原有七库保存；v0.12.0 的 MySQL DDL 共 35 张表。旧六库/七库备份可直接使用迁移脚本，新成长库缺失时留空，不凭空创建旧身份。数据目录或数据库不变时，切换聊天和程序重启都不会清空已提交的成长档案。单次模型请求若中断，会重新执行任务，不从模型内部某一步续算。

自主资讯探索使用 RSS 和 B 站热门标题、简介。v0.28 起，聊天中具体分享的 B 站视频另走媒体读取流程，可提供抽样画面和公开字幕节选。小黑盒资讯沿用 RSS 入口。真实模型的成长质量、长期效果和语义表达判断仍需部署验收。

## v0.11.0 · 唯一主人、持续自我和统一记忆

QQ 私聊、群聊与外部资讯属于同一个智能体的经历入口。新统一检索层跨来源联想，原始账本仍保留说话者、场景、时间和审核状态；不会迁移或清空旧记忆。

- 新增 `self_state.sqlite3`（MySQL 对应两张 `self_` 表）：保存稳定的 `agent_id`、唯一主人绑定、管理配置变更历史和模型调用预算。重启不换身份，也不重置当日预算。
- 主人仍通过 `owner.qq` 的可信平台身份识别。不同窗口里是同一个主人；昵称、自称和模型提案不能更换主人。显式修改管理配置会记录绑定变更。
- 内部联想可同时读取启用的 Brain、Mind、Cognition 和公开资讯账本。只把事实的有效版本作为事实，保留事件和公开观察的不确定性。
- 表达环节决定直接使用、提炼通用知识、只影响交流方式或不采用。跨来源的通用知识候选须经第二次模型复核；未公开的私人原文不会直接放入其他会话的回复提示。
- `active` 下统一表达接替原 Brain/Mind 的直接记忆注入，避免从旧路径绕过表达判断。主人称呼和专属关系风格继续生效。
- v0.11.0 建立统一心智基础，v0.12.0 补充长期兴趣、观点和人格成长。后台资讯功能沿用公开标题和简介。

### 开启与观察

在现有配置中合并以下字段，保留已配置的主人、数据库与名单，不要用示例覆盖私人配置。先确保 `[owner] enabled=true` 且 `qq` 已绑定；输入窗口沿用 `[brain_memory] include_owner/private_users/group_ids`，这些名单控制采集和交谈入口，**不是内部记忆的检索分区**。

```toml
[unified_mind]
enabled = true
mode = "observe"            # 确认运行后改为 active，让筛选后的记忆影响回复
recall_limit = 12
candidate_limit = 200
min_interval_seconds = 30
max_daily_model_calls = 60   # 联想和通用知识复核分别计一次；失败也计数
model_timeout_seconds = 8
```

为了产生新的聊天记忆，还需 `[brain_memory] enabled=true`、`auto_reflect=true`，按需要打开 `enable_events/enable_facts/enable_goals` 并设置群/用户名单。可以保留 `brain_memory.mode="observe"`；统一回复由 `unified_mind.mode` 控制。Mind、Cognition 各自的 `enabled` 控制是否参与统一检索，它们原有的 `mode` 仍控制各自独立功能。仅开启统一心智不会自动联网、增加主动发送或启用原本关闭的资料源。

管理端内部 API `living_world_unified_status()` 可查看持续身份、主人是否已绑定、绑定版本、预算日期、已用调用次数和最近联想统计；不返回私人记忆正文或主人 QQ 号。`observe` 会调用模型但不新增回复提示；若已有旧模块处于 active，它们保持原行为。`active` 每次回复最多额外进行一次联想和一次通用知识复核，每次请求有超时。

### 保留与恢复

统一检索读取原账本，不把私聊内容复制成其他群的新事实。重启后，已有记忆、目标和自我身份继续存在；收到新的可信消息后建立当前听众对应关系，再使用这些记忆。临时表达缓存不会持久保存，未完成的模型请求重启后不续跑。

SQLite 备份需包含原数据文件及新增 `self_state.sqlite3`。v0.11.0 的 MySQL DDL 共 31 张表；迁移程序支持原先没有身份库的六库备份，并在身份库存在时原样迁移身份和预算。不同智能体使用不同数据库/表前缀或数据目录；同一智能体各窗口共用这些数据。

表达复核由程序规则和两次分开的模型请求共同完成，语义匿名化仍可能判断错误，不能声称完全杜绝泄露。当前验证使用离线模型响应和 SDK 模拟；真实 MaiBot/QQ 会话及模型效果需要部署后验收，真实 MySQL 集成测试另由 CI 的临时服务运行。

## v0.10.0 · 六套 SQLite/MySQL 同后端及完整迁移

- `[storage] backend="mysql"` 统一控制外部资讯、BrainBook、Mind、Cognition、ExperienceLoop、ProactiveFeedback 六套持久化，不再只切换资讯库。
- `sql/mysql.sql` 包含总计 29 张表，建议 MySQL 8.4，保留原始作用域、身份、主键、历史审核状态和证据表。
- `scripts/migrate_all_sqlite_to_mysql.py` 支持一次性只读预览；正式导入必须目标为空、数据库字段完全匹配、全部表校验通过且原子提交。确实没有启用某个模块时，正式迁移需显式加 `--allow-missing`。
- SQLite 仍为可选后端；MyBatis 风格的脱敏 SQL 日志共用 `storage.sql_debug`。**线上迁移前必须停止写入并完整备份，不会自动双写。**

## v0.9.1 · 事实与目标未入库的隐私安全诊断

针对 v0.9.0 实测中出现的「快速周期正常运行、但事实和目标数量为零」问题，增加独立 `[MaiLivingWorld][BRAIN-STAGES]` 日志，区分：

- `model`：模型 JSON 中四类提案的数量；
- `parsed`：通过证据 ID / 敏感字段格式验证的数量；
- `attempted`、`accepted`：送入数据库与成功处理的数量；
- `parse_rejected`、`store_rejected`：固定原因代码及计数，例如 `goals.invalid_action`、`facts.subject_evidence_mismatch`；
- JSON 无效时仅记录 `parse_error`，不回显原文。

不保存或打印聊天原文、事实值、模型输出、消息 ID、证据哈希；保持原有事实审核、目标完成规则和全部配置字段不变。

升级后用以下命令筛选诊断日志：

```bash
docker logs --since 20m maibot 2>&1 | grep -E 'BRAIN-STAGES|BRAIN-QUICK'
```

**注意**：这次是诊断补丁，不表示事实提取或目标完成条件问题已修复；先根据新增统计定位根因再处理。旧 `brain_book.sqlite3` 保留，不要清库。升级原有私人配置时，将 `[plugin].config_version` 更新为 `"0.9.1"`。

## v0.9.0 · 五分钟快速记忆、证据修正与目标进展

在原有分作用域 SQLite 账本上进行**非破坏性迁移**：保留 v0.8.0 数据，无需清空 `brain_book.sqlite3`。

- `brain_memory.quick_update = true` 开启额外的轻量周期，默认每五分钟只在当前作用域消息摘要变化后尝试一次；每分钟检查是否到期。**不会触发主动 QQ 发送**。
- `brain_memory.interval_hours` 继续控制完整反思（含元认知）；两套模型限频独立，快速整理不会抢占小时级周期。
- 明确事实的短中文字段（如「颜色＝蓝色」）、简短纠正消息现在能通过证据入口；已有事实仍须满足独立证据门槛。
- 旧事实被否决或取代后，必须有至少两条新的本人证据才能重新成为待审核/冲突候选，管理员仍负责矛盾裁决；`brain_fact_revisions` 记录每次状态变化。
- 对于内容相同、完成条件相同的一次性目标，防止仅仅改写标题造成重复条目；每年等周期目标独立维护。已存在的其他语义重复目标不会自动删除。
- 事件读取 Host 提供的可信时间戳（含毫秒），缺少原始时间时保存 `happened=0` 表示未知，而不是错误地使用反思时间。
- 管理端新增 `living_world_brain_quick_reflect(scope, confirm="RUN_BRAIN_QUICK")`，只允许已经授权的 QQ 会话、至少 60 秒冷却，不发送消息。
- 日志按 `[BRAIN]`（深度）与 `[BRAIN-QUICK]`（快速）区分，记录有效证据数、未知时间条数和写入情况；调试日志展示跳过原因。

**安全边界**：私人/群聊隔离、主人白名单、管理员复核、主动发送退避保持不变；这不是逐条消息立即推理，短期可能存在几分钟延迟。建议先在 `observe` 测试后切换 `active`。备份用户配置和独立 SQLite 数据再升级，尤其不要把真实 QQ 号提交至公共仓库。

示例（其他开关沿用你的配置）：

```toml
[brain_memory]
enabled = true
auto_reflect = true
quick_update = true
quick_interval_minutes = 5
interval_hours = 1
```

## v0.7.0 · 事件→行动→回执→明确评价→策略调整（最小闭环）

在既有主动聊天回执链上添加只记录元数据的 `experience_loop.sqlite3`：

1. 主动联系绑定主人 QQ 私聊时保存 event_id、会话、话题哈希和类型（公开资讯/历史话题）。
2. 同步 Planner stop/reply、Host 真实发送结果；**只有 send_confirmed 才会等待反馈**。
3. 只读取同会话、结构化 QQ ID 与主人一致且带真实 message_id 的新私聊纯文本，不保存聊天原文。
4. 普通回复只表示 `reply_only`，并不代表满意；没有回复也不代表不满。只接受有限的明确评价。
5. `mode="active"` 下，明确不喜欢某类话题才会暂缓该类型的主动联系，且绝不突破原先的白名单、静默期和配额。
6. 主人明确要求“别再主动联系我”后暂停所有主动联系；只有管理员显式解除才恢复。

不会保存聊天原文、昵称、媒体；不会自动改写 A_Memorix、核心人格和主人可信长期观察；群聊和其他 QQ 成员暂不接入。

```toml
[owner]
enabled = true
qq = "绑定主人的QQ号"

[experience]
enabled = true
mode = "observe"  # 先观察，确认后再主动调整
feedback_hours = 24
negative_pause_hours = 12
```

新增仅管理员可调用的内部 API：`living_world_experience_status()` 查看状态，`living_world_experience_resume(confirm="RESUME_OWNER_PROACTIVE")` 解除主人拒绝主动联系。

**边界**：这是可运行的主动联系行为反馈**小闭环**，不是完整自主认知。长期目标规划、跨人物记忆、开放式反馈语义判断及事实观点修正尚待后续。MaiBot 现有 Hook 没有端到端 task_id/message_id 强关联，复杂并发下仍不能保证百分百归因。该数据库固定为本地 SQLite，切换资讯存储为 MySQL 时仍需备份。

## v0.8.0 · 情景记忆、可信事实、目标、进展与元认知（按模块逐项测试）

新增独立 `brain_book.sqlite3`，**不覆盖**既有 `experience_loop.sqlite3`、私人 `mind.sqlite3` 或 MaiBot `A_Memorix`。参考 Hindsight/A-MEM 的概念，完全独立实现（不复制 AGPL 源码）。默认全部关闭。

- **模块一：情景记忆**：当前 QQ 会话结构化消息，生成带人物、事件时间、摘要和证据哈希的记忆，不保存完整原文。
- **模块二：事实、关系与巩固**：作用域内的主体-属性-值；两个独立证据且置信度达到阈值才激活，矛盾先冻结并供管理员审核，保留被取代的旧事实。
- **模块三：目标与计划**：用户明确表达的未完目标、完成条件与 `next_action`；不执行外部工具。
- **模块四：进展闭环**：必须有目标发起者的新证据才可标记完成/放弃；管理员可显式复核。
- **模块五：元认知复盘**：记录有边界的反思摘要与每轮更新计数，不持久化隐藏思维链。
- **模块六：可用记忆与反馈**：`brain_memory.mode="active"` 才向身份/会话核验通过的提示词注入当前作用域的有效事实、待办；主动行为的真实发送评价仍由 v0.7 的经验模块处理，不会增加主动次数。

**隔离**：只允许显式配置的 QQ 私聊与群聊；`private:QQ号`、`group:群号` 独立。某群的记忆不进入任何人私聊，也不跨群共享。同一用户在群中和私聊的内容不会无审核合并。

### 按顺序开启测试

保留其他配置，只修改以下配置字段：

```toml
[brain_memory]
enabled = true
mode = "observe"
auto_reflect = true
include_owner = true
private_users = []
group_ids = []
enable_events = true          # ① 情景事件
enable_facts = false          # ② 事实巩固
enable_goals = false          # ③ 未完目标
enable_progress = false       # ④ 完成与修正
enable_metacognition = false  # ⑤ 元认知摘要
interval_hours = 12
min_messages = 4
history_limit = 40
max_scopes_per_check = 2
event_limit = 200
```

先观察事件，再逐一开启 `enable_facts`、`enable_goals`、`enable_progress`、`enable_metacognition`。需开启 `owner.enabled` 并绑定 `owner.qq` 才会自动包含主人私聊。其他用户必须加入 `private_users`；群聊必须在 `group_ids`。**不必打开 `general.enabled` 来运行后台记忆反思**。同时启用旧 `mind.auto_reflect` 会增加模型开销，初期建议只测一个。

仅管理端内部 API（`public=False`，仍需宿主访问控制）：
- `living_world_brain_status(scope="private:QQ号")`
- `living_world_brain_recall(scope="group:群号")`
- `living_world_brain_review_fact(scope,fact_id,decision="approve|reject",confirm="REVIEW_BRAIN_FACT")`
- `living_world_brain_decide_goal(scope,goal_id,decision="done|abandoned|reopen",confirm="REVIEW_BRAIN_GOAL")`
- `living_world_brain_clear(scope,confirm="CLEAR_BRAIN_SCOPE")`

**行为闭环联动**：若同时打开 `experience.enabled=true`，新账本按真实主动反馈事件 ID 关联行动、Planner/Host 结果与主人经验证的明确评价。后续反思能参考当前私聊范围的这些行动结果。未经确认发送的尝试不能拥有用户反馈；普通回复不代表好评。

**局限**：LLM 给出证据 ID 只能证明对应消息存在，不能证明总结语义完全准确，先用 `observe` 人工检查；没做完整的跨群授权分享、自动网页验证、无限规划或底模微调。它在系统层面提供有限的记忆/认知循环，而不是人类意识。SQLite 文件需独立备份；若启用 MySQL 资讯后端仍不会转移此账本。

## 功能与边界

| 能力 | 当前状态 |
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
| QQ 中分享的图片、短视频、B 站链接 | 主人私聊和允许的群可读取图片、视频抽样帧或 B 站官方分镜，B 站公开字幕可节选。音轨和评论暂无读取入口。 |
| 小黑盒登录、回帖、B 站评论/点赞/投币/发布 | ❌ 不支持，第一版只读 |
| 自动充值 Token / 访问支付 | ❌ 不支持 |
| 自身兴趣、观点与渐进性格 | ✅ v0.12.0 起持久保存并限幅更新；不含日程/睡眠模拟 |
| cap / Pixiv 工具与观察记忆 | ✅ v0.13.0 可选接入，按身份、工具类别与预算限制 |

## 标准 [plugin] 配置节（v0.5.0 修复）

SDK Runner 校验插件配置时必须存在 `[plugin]`，其中要有 `config_version`。
本版通过 `config_model` 的标准 `PluginSection` 自动生成：

```toml
[plugin]
enabled = true
config_version = "0.28.0"

[general]
enabled = false
dry_run = true
```

- `plugin.enabled` 是插件总开关；`general.enabled` 只控制主动聊天，默认关闭。外部探索由 `explorer.enabled` 控制，公开认知还需 `cognition.enabled=true` 与 `auto_think=true`；主人身份识别由 `owner.enabled` 单独控制。
- Runner 会自动生成 `config.toml`；发行 ZIP 不包含本地配置，避免覆盖主人 QQ 或数据库密码。
- 已安装旧版并提示缺少 `[plugin]` 时，请先备份原始配置；可以在 WebUI 的原始 TOML 顶部补上上述 `[plugin]` 节，再安装修复版。不要删除插件数据目录中的 SQLite 数据库。

## 中文注释版配置模板

完整 `config.example.toml` 有 **30 个分组、199 个字段**，每个字段都附有中文说明。首次使用只需填写「快速开始」的两项。其余设置按「日常相处」「记忆与成长」「阅读与工具」「运行与存储」分类，按需展开。

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

v0.13.0 新增 MCP 三组共 31 个字段，默认不开启工具访问。新增字段时保留原有主人识别与主动聊天配置。

## 安装：WebUI 一键导入 ZIP

1. 确保 MaiBot `>=1.3.10`，插件 SDK `>=2.12.0`。
2. 打开 MaiBot WebUI：**插件扩展 → 更多操作 → 从 ZIP 安装插件**。
3. 选择 `MaiLivingWorld-v0.28.0-WebUI.zip`。
4. 在插件管理打开 **MaiLivingWorld · 麦麦的小世界**，到 **快速开始** 选择使用方式、填写主人 QQ 号并保存。
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

**九套账本统一后端：** `storage.backend="mysql"` 控制资讯、BrainBook、Mind、Cognition、ExperienceLoop、ProactiveFeedback、持续身份、成长与工具记忆；`sqlite` 使用各自文件。不会双写或自动搬迁旧数据，不同智能体使用不同数据库/表前缀或数据目录。

### MySQL 表结构与非破坏性升级

- 新库可执行 `sql/mysql.sql`，当前共 39 张表，含中文表/字段注释，建议 MySQL 8.4、InnoDB、utf8mb4。
- 已有数据库先备份并停止插件写入，再执行 DDL 创建缺少的身份、成长和工具表。不要删表重建；`CREATE TABLE IF NOT EXISTS` 不更新旧表注释。
- 需要补旧表注释时先核对实际列定义，再执行 `sql/mysql_comments_existing.sql`。该脚本不修改业务行，但 `ALTER TABLE` 可能持有元数据锁；自定义字段不能直接套用。

### 一次性迁移九个 SQLite 库

1. 停止所有插件写入，备份原配置和数据。WAL 模式使用 SQLite backup API 获取一致快照，不要只复制仍在写入的主文件。
2. 把存在的九库放入同一备份目录：`living_world.sqlite3`、`brain_book.sqlite3`、`mind.sqlite3`、`cognition.sqlite3`、`experience_loop.sqlite3`、`proactive_feedback.sqlite3`、`self_state.sqlite3`、`growth.sqlite3`、`tool_memory.sqlite3`。
3. 在带有迁移脚本和依赖的环境先只读预览；此步骤不连接 MySQL、不修改源文件：

```sh
python scripts/migrate_all_sqlite_to_mysql.py --directory /mnt/mlw-backup
```

4. 旧六库/七库/八库备份可以缺少后来新增的身份、成长和工具库；存在时必须完整迁移。原有六库缺失只有在确认模块从未启用时才显式加 `--allow-missing`。不要用此选项跳过未备份数据。
5. 把 `MLW_MYSQL_HOST`、`MLW_MYSQL_PORT`、`MLW_MYSQL_USER`、`MLW_MYSQL_PASSWORD`、`MLW_MYSQL_DATABASE`、`MLW_MYSQL_PREFIX` 安全提供给迁移进程；目标数据库/表前缀的 39 张表必须全部为空，再执行：

```sh
python scripts/migrate_all_sqlite_to_mysql.py --directory /mnt/mlw-backup --apply --confirm MIGRATE_ALL_EMPTY_TARGET
```

脚本统一事务写入并逐表核对记录数和字段值哈希，失败回滚，不删除旧 SQLite 或原配置；字段不匹配时拒绝静默遗漏。完成后才把插件 `storage.backend` 切换为 `mysql`，填写相同连接配置并重载，检查身份、成长版本、工具计数和原记忆。回退前先处理 MySQL 运行期间的新记录，避免丢失后续记忆。

仅迁移原资讯库的 `scripts/migrate_sqlite_to_mysql.py` 继续保留；完整切换使用上面的全量脚本，不要先后对同一目标混用两个导入脚本。

### MyBatis 风格 SQL 排查日志

设置 `[storage] sql_debug = true` 并重载插件，在 MaiBot 日志中可看到类似输出（为示意，敏感参数始终隐藏）：

```text
[MaiLivingWorld][SQL][mysql] ==> Preparing: SELECT ... WHERE target=%s
[MaiLivingWorld][SQL][mysql] ==> Parameters: [str(***)]
[MaiLivingWorld][SQL][mysql] <== Executed (0.41 ms)
[MaiLivingWorld][SQL][sqlite] ==> Preparing: UPDATE brain_goals SET status=? WHERE id=?
[MaiLivingWorld][SQL][sqlite] ==> Parameters: [str(***), str(***)]
[MaiLivingWorld][SQL][sqlite] <== Updates: 1 (0.22 ms)
```

SQLite 各账本及 MySQL 主库会使用同一开关，能显示 SQL 模板、参数**类型**、影响行数和耗时。**永不打印参数原值**，不含用户名密码、QQ 号、原始聊天或 Token。排障结束请设回 `sql_debug = false`，避免持续产生大量日志。

完整账本 MySQL 集成测试使用 GitHub Actions 启动临时 MySQL 8.4；上线前仍须使用你的真实备份做校验及回滚演练。如果你使用现有的 cap MySQL，请用单独的 `mai_living_world` 库，或至少不同表前缀，防止和 cap 的表混用。配置文件可能保存明文数据库密码，请限制文件权限并避免提交到仓库。

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
- 通过 A_Memorix 资讯桥导入的记录保留“仅标题和简介”的来源说明。聊天里读到的 B 站分镜、画面观察与公开字幕节选，归入插件自己的观察账本。

## 技术与稳定性

- 默认 SQLite WAL；可在 WebUI 切换为 MySQL，记录资讯、已尝试分享的对象、主动唤醒次数与写入状态。
- 重启后仍能挡住重复分享和当天超额唤醒；所有真正的唤醒请求都**先记账再提交**，失败时不自动重复刷屏。
- 仅对明确填入的 QQ 私聊用户和群号尝试唤醒，不会遍历并联系所有 QQ 好友。
- 信息源仅允许配置的 HTTPS 白名单主机、不跟随 HTTP 重定向、限制响应体积和请求超时；但这不等于对外部网络和内容安全的绝对保证。不要添加不可信 RSS 地址。
- 外部内容作为资料而非指令。默认只读；MCP 可在独立开关与可信主人私聊授权下提交有限下载、归档、账户或文件任务，不提供支付能力。
- 主插件依赖见 manifest；MCP 与 jsonschema 用于工具连接和参数校验。Docker 网关独立运行，不修改 MaiBot 本体，也不安装浏览器。

## 常见问题

**ZIP 被 WebUI 拒绝？** 确认你选的是发行 ZIP、没有再二次打包；插件目录只有一层，顶层直接可看到 `MaiLivingWorld/_manifest.json` 与 `MaiLivingWorld/plugin.py`。同 ID 已存在时，WebUI 不允许重复安装。

**显示安装成功但没有运行？** 在插件管理开启它并检查对应功能开关；只有主动聊天需要 `general.enabled=true`。查看 Runner 日志。若出现 SDK 或宿主版本不兼容，不能仅靠手工放宽清单版本来解决，应检查实际接口。

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
- 这个模块保存定期反思摘要和可修正的行为调整。聊天里取得的视觉观察可通过统一记忆参与后续理解。

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

