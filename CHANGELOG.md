# 更新记录

## v0.2.0 — 2026-10-28

- 新增 WebUI 可选 SQLite 和 MySQL 存储后端，默认仍为免配置 SQLite。
- MySQL 采用 PyMySQL、参数化查询、InnoDB 事务，自动创建带中文注释的三张数据表。
- 保持原有资讯去重、每目标分享记录、唤醒冷却与每日配额语义。
- MySQL 不可用时明确报错，不会静默切换存储。
- 提供 `sql/mysql.sql`、安全配置说明和 GitHub CI 基础测试。
- 尚未进行真实 MySQL 服务和 MaiBot 容器端到端测试。

## v0.1.0 — 2026-10-28

- 统一主动聊天调度、B站公开热门/RSS 探索、A_Memorix 记忆接口和 SQLite WAL 去重。
