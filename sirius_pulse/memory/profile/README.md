# 人物画像系统

Sirius Chat 的人物画像由主聊天模型按需维护，不再由后台总结器或三元组演化链自动抽取。

## 设计原则

- 主模型只在信息长期稳定、明确、未来有用时调用画像工具。
- 临时任务、玩笑、角色扮演、一次性情绪和模型猜测不进入长期画像。
- 画像写入采用增量 patch，系统负责合并、持久化、审计和 prompt 渲染。
- 别称属于人物画像的 `aliases` section，不再由独立 alias/evolution 系统维护。
- `UserSemanticProfile` 只保留互动统计职责，例如熟悉度和回应亲和，不存人物事实。

## 数据结构

核心模块位于 `sirius_pulse/memory/profile/`：

- `UserPersonaProfile`：人物画像卡，按用户和群组隔离。
- `ProfileSection`：画像分区，例如 `aliases`、`identity`、`preferences`、`boundaries`。
- `ProfileItem`：单条画像项，包含 value、confidence、evidence、来源消息和状态。
- `UserPersonaProfileStore`：SQLite 持久化，写入 `user_persona_profiles` 和 `user_profile_events`。
- `UserPersonaProfileManager`：画像读写、别称解析、事件查询和 prompt card 渲染。

## 主模型工具

内置技能 `user_profile` 暴露给主模型：

- `get`：读取画像。
- `update`：写入稳定画像信息。
- `mark`：将画像项标记为 `rejected` 或 `stale`。
- `list_events`：查看画像变更事件。

别称也通过 `user_profile` 写入 `aliases` section，不再暴露独立别名工具。

## Prompt 注入

`PromptFactory` 将相关用户画像渲染到 `<biography>` 动态段中，供主模型理解当前发言者和被提及者。画像只作为相处参考；当前对话和用户纠正优先级更高。

## 已移除旧功能

旧的 `memory/biography` 和 `memory/evolution` 模块、WebUI biography/evolution API 以及 `UnifiedUserManager` 中的 alias 管理职责已移除。身份解析、技能别称管理和认知分析统一读取 `profile_manager`。
