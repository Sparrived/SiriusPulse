# Sirius Pulse 文档导航

## 快速开始

| 文档 | 适合谁 | 内容 |
|------|--------|------|
| [README.md](../README.md) | 第一次使用 | 安装、基本用法、项目简介 |

## 核心系统详解

| 文档 | 一句话定位 |
|------|-----------|
| [情感化群聊引擎深度解析](engine-deep-dive.md) | v1.0 唯一引擎，从四层认知管线到微观情绪分析的完整实现 |
| [持久化系统](persistence-system.md) | 三层记忆底座（基础→日记→语义）+ 会话存储 + Token 统计 |
| [配置指南](configuration-guide.md) | 从全局配置到人格级配置，含模型编排与 JSONC 支持 |
| [SKILL 系统指南](skill-guide.md) | 插件机制：内置技能、编写规范、数据存储、依赖自动安装 |
| [模型提供者系统](provider-system.md) | LLM 调用层：7 个平台、统一接口、自动路由、健康检查 |
| [多人格生命周期](persona-lifecycle.md) | 主进程调度多个人格子进程：创建、启停、监控、数据隔离 |
| [平台适配层](platforms.md) | NapCat 多实例管理、OneBot v11 适配、QQ 桥接、首次配置向导 |
| [WebUI 管理面板](webui.md) | aiohttp REST API + 静态页面，统一管理多人格和 NapCat |
| [人格系统](persona-system.md) | 可配置、可持久化的角色人格，影响引擎的整个认知管线 |
| [核心数据模型参考](models-reference.md) | Message、Participant、PersonaProfile 等跨模块共享的数据结构 |

## 架构与流程

| 文档 | 内容 |
|------|------|
| [架构概览](architecture.md) | 整体模块关系、数据流、技术栈 |
| [完整架构流程](full-architecture-flow.md) | 从消息进入到回复生成的全链路详细流程（人类易读版） |

## 项目治理

| 文档 | 内容 |
|------|------|
| [项目问题跟踪](project-issues.md) | 已知问题、风险分析、改进建议与推进路线图 |
| [最佳实践](best-practices.md) | 生产环境部署、性能调优、常见问题 |

## 变更联动确认

| 文档 | 内容 |
|------|------|
| [变更联动确认指南](change-impact-guide.md) | 后端/配置/契约变更后，需同步检查的前端、API、文档位置速查表 |
