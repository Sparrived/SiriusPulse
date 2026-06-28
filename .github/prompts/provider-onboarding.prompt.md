---
agent: ask
description: "为 Sirius Pulse 生成并接入新的 LLM provider 实现，同时完成测试与文档同步。"
---

# Provider 接入

为 Sirius Pulse 创建一个新的 provider 集成。

## 输入参数

- Provider 名称：${input:providerName:ExampleProvider}
- API 风格：${input:apiStyle:OpenAI-compatible 或 custom}
- Endpoint 路径：${input:endpointPath:/v1/chat/completions}
- 鉴权 Header 格式：${input:authHeader:Authorization: Bearer <token>}

## 必须产出

1. 在 `sirius_pulse/providers/` 下创建 provider 模块。
2. 确保其实现 `sirius_pulse/providers/base.py` 中的 `LLMProvider`。
3. 在 `sirius_pulse/providers/__init__.py` 中导出，并按需在 `sirius_pulse/__init__.py` 中导出。
4. 按需新增/扩展测试，优先使用 `MockProvider` 模式。
5. 在同一任务中同步更新文档与技能：
   - `docs/architecture.md`
   - `.github/skills/framework-quickstart/SKILL.md`
   - `README.md`（当用户可见用法变化时）

## 验收清单

- Provider 能正确解析成功响应。
- Provider 对 HTTP/网络失败路径给出清晰错误。
- `sirius_pulse/async_engine.py` 中不出现 provider 特定逻辑泄漏。
- `pytest -q` 通过。
