---
name: release-management
description: "在为 Sirius Pulse 做版本发布时使用，涵盖发布前校验、PyPI自动化发布流程与Trusted Publishing配置。关键词：版本发布、PyPI发布、发布检查、自动化构建、Trusted Publishing。"
---

# 版本发布管理指南

## 目标

在完成整个功能的设计实现后，执行一致的发布前检查流程，然后触发自动化发布到 PyPI。

本 SKILL 合并了原 `release-checklist` 的发布前校验清单和原 `pypi-publishing` 的发布流程说明。

## 语言规范（强制）

- 本 SKILL 及所有后续新增/修改的 SKILL 必须使用中文。
- `description` 和正文必须为中文。
- 若任务中发现英文 SKILL 内容，需在同一任务中同步中文化。

## 关键原则

**发布时机**：只有在完成整个功能的设计实现后，确保所有功能模块已集成、测试已通过、文档已同步的情况下，才进行 release 触发。禁止在功能尚未完成设计的阶段创建 release。

---

## 第一部分：发布前检查清单

### 步骤 1：版本与元数据

- [ ] 校验 `pyproject.toml` 中版本号与描述是否正确。

### 步骤 2：安装并执行检查

```bash
python -m pip install -e .[test]
pytest -q
```

- [ ] 测试全部通过。

### 步骤 3：命令校验

- [ ] `python main.py --help` 正常输出。
- [ ] `python main.py run --help` 正常输出。
- [ ] `python main.py persona --help` 正常输出。
- [ ] 在独立工作目录运行 `python main.py run` 验证入口可用（含人格目录自动创建）。
- [ ] 首次仅进入并退出时，检查 `data/` 下的人格目录和日志是否成功写出。

### 步骤 4：文档与 AI 资产同步

- [ ] 确认 `README.md` 中命令/示例仍可使用。
- [ ] 确认 `docs/architecture.md` 与当前模块边界一致。
- [ ] 确认 `docs/full-architecture-flow.md` 与当前执行流一致。
- [ ] 确认 `.trae/skills/framework-quickstart/SKILL.md` 反映最新架构。
- [ ] 确认所有 SKILL 文件为中文。

### 步骤 5：变更摘要

- [ ] 总结关键变更与已知限制。

### 失败条件

- 测试执行失败。
- 命令示例过期。
- 架构或 SKILL 文档与代码不同步。

---

## 第二部分：PyPI 自动化发布流程

## 概述

采用 GitHub 官方推荐的 Trusted Publishing（OIDC）认证方式，无需存储 API token，更加安全可靠。

## 前置条件

### 一次性配置（仅需一次）

在首次发布前，需要在 PyPI 和 TestPyPI 上各配置一次 Trusted Publisher：

1. **PyPI 配置**
   - 访问：https://pypi.org/manage/account/publishing/
   - 点击 "Add a new pending publisher"
   - 填入以下信息：
     - PyPI Project Name: `sirius-pulse`
     - GitHub Owner: `Sparrived`
     - GitHub Repository: `SiriusChat`
     - Workflow Name: `publish.yml`
     - Environment Name: `pypi`

详细步骤见项目根目录的 `SETUP_TRUSTED_PUBLISHING.md`。

## 发布流程

### 步骤 1：更新版本号

```bash
# 编辑 pyproject.toml，修改 version 字段
nano pyproject.toml
# 例如：version = "1.1.0"
```

### 步骤 2：更新 CHANGELOG（可选）

```bash
# 编辑 CHANGELOG.md，在 [Unreleased] 下添加新版本条目
nano CHANGELOG.md

# 示例：
# ## [1.1.0] - 2026-04-06
# ### Added
# - 新功能 X
# ### Fixed
# - Bug Y
```

### 步骤 3：本地验证

```bash
# 安装依赖
python -m pip install -e .[test]

# 运行测试
pytest -q

# 构建包并验证元数据
python -m build
python -m twine check dist/*.whl dist/*.tar.gz
```

### 步骤 4：提交更改

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump version to 1.1.0"
git push origin master
```

### 步骤 5：推送版本 tag（触发发布）

```bash
# 创建并推送 tag（仅需这一步，workflow 自动处理）
git tag v1.1.0
git push origin v1.1.0

# GitHub Actions 自动触发：
# 1. 构建 wheel 和 sdist
# 2. 验证包元数据
# 3. 发布到 PyPI
```

### 步骤 6：验证发布

- 访问 GitHub Actions：https://github.com/Sparrived/SiriusChat/actions/workflows/publish.yml
- 等待 workflow 完成（约 1-2 分钟）
- 访问 PyPI：https://pypi.org/project/sirius-pulse/
- 确认新版本已出现

## 工作流说明

### 自动触发条件

| Job | 触发条件 | 目标 |
|-----|---------|------|
| `build` | 所有 tag push | 构建 wheel 和 sdist |
| `publish-to-pypi` | 推送 v* tag | 发布到官方 PyPI |

### Workflow 文件位置

- `.github/workflows/publish.yml`

### 使用的 GitHub Action

- `actions/checkout@v4`：检出代码
- `actions/setup-python@v4`：设置 Python 环境
- `actions/upload-artifact@v4`：上传构建产物
- `pypa/gh-action-pypi-publish@release/v1`：发布到 PyPI

## 故障排除

### 问题 1：Workflow 执行失败

**症状**：GitHub Actions 页面显示红色

**解决步骤**：
1. 点击失败的 job 查看日志
2. 查找 ERROR 或 FAILED 关键字
3. 常见原因：
   - 包元数据错误：检查 `pyproject.toml` 的 `name`, `version`, `description`
   - Trusted Publisher 未配置：确认已在 PyPI 注册
   - Git tag 格式错误：必须为 `v` 前缀 + 版本号（如 `v1.1.0`）

### 问题 2：400 Bad Request from PyPI

**可能原因**：
- 相同版本重复发布
- 项目在 PyPI 上尚未初始化

**解决**：
- 确保版本号唯一（不重复发布）
