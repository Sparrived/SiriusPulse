---
name: pypi-publishing
description: "使用此技能在发布新版本到 PyPI 时执行完整的发布流程。涵盖版本更新、构建验证、tag 推送和 GitHub Actions 自动化。关键词：PyPI 发布、版本控制、自动化构建、Trusted Publishing。"
---

# PyPI 自动化发布流程

## 概述

该技能实现了 sirius-pulse 包到 PyPI 的完全自动化发布。采用 GitHub 官方推荐的 Trusted Publishing（OIDC）认证方式，无需存储 API token，更加安全可靠。

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
# 例如：version = "0.5.0"
```

### 步骤 2：更新 CHANGELOG（可选）

```bash
# 编辑 CHANGELOG.md，在 [Unreleased] 下添加新版本条目
nano CHANGELOG.md

# 示例：
# ## [0.5.0] - 2026-04-06
# ### Added
# - New feature X
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
git commit -m "chore: bump version to 0.5.0"
git push origin master
```

### 步骤 5：推送版本 tag（触发发布）

```bash
# 创建并推送 tag（仅需这一步，workflow 自动处理）
git tag v0.5.0
git push origin v0.5.0

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

**症状**：GitHub Actions 页面显示红色 ✗

**解决步骤**：
1. 点击失败的 job 查看日志
2. 查找 ERROR 或 FAILED 关键字
3. 常见原因：
   - 包元数据错误：检查 `pyproject.toml` 的 `name`, `version`, `description`
   - Trusted Publisher 未配置：确认已在 PyPI 注册
   - Git tag 格式错误：必须为 `v` 前缀 + 版本号（如 `v0.5.0`）

### 问题 2：400 Bad Request from PyPI

**可能原因**：
- 相同版本重复发布
- 项目在 PyPI 上尚未初始化

**解决**：
- 确保版本号唯一（不重复发布）
- 首次发布时，Trusted Publisher 会自动创建项目

### 问题 3：版本号冲突

**症状**：发布时提示版本已存在

**解决**：
- PyPI 不允许删除已发布版本
- 发布新版本号（如 0.5.1）
- 或在 PyPI 项目页面更新"Latest Release"标记

## 最佳实践

1. **使用 semantic versioning**
   - MAJOR.MINOR.PATCH（如 0.5.0）
   - 详见：https://semver.org/

2. **保持 CHANGELOG 更新**
   - 每个版本都应有对应 CHANGELOG 条目
   - 便于用户了解更新内容

3. **本地验证后再发布**
   - always run `pytest -q` before tagging
   - always run `twine check` before pushing tag

4. **一次发布一个版本**
   - 避免批量 tag 推送
   - 监控每个版本的发布状态

5. **保存发布日志**
   - GitHub Actions 日志可查询 30 天
   - 重要发布前截图保存

## 快速参考

```bash
# 完整发布流程（从版本更新到验证）
python -m pip install -e .[test]
nano pyproject.toml           # 更新版本号
pytest -q                     # 运行测试
python -m build               # 构建包
python -m twine check dist/*  # 验证元数据
git add pyproject.toml
git commit -m "chore: bump to vX.Y.Z"
git push origin master
git tag vX.Y.Z
git push origin vX.Y.Z        # ← 触发 PyPI 发布
```

## 相关资源

- 官方发布指南：https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/
- Trusted Publishing：https://docs.pypi.org/trusted-publishers/
- 项目配置指南：[SETUP_TRUSTED_PUBLISHING.md](../../SETUP_TRUSTED_PUBLISHING.md)
- 实现状态：[PYPI_PUBLISHING_STATUS.md](../../PYPI_PUBLISHING_STATUS.md)
