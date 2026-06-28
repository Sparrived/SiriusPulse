---
applyTo: "tests/**/*.py,sirius_pulse/**/*.py"
description: "当新增或修改 Python 后端代码时使用，用于强制执行 pytest、回归检查与测试质量门禁。"
---

# 后端测试指令

## 默认测试流程

1. 以可编辑模式安装并包含测试依赖：
   - `python -m pip install -e .[test]`
2. 运行单元测试：
   - `pytest -q`
3. 定向调试时：
   - `pytest tests/test_engine.py -q`

## 质量门禁

- 测试必须从业务侧出发，依照用户实际使用路径编写，优先覆盖公开入口到可观察结果的业务闭环。
- `sirius_pulse/` 下新增代码路径必须至少有一个直接测试。
- provider 行为变更必须覆盖成功与失败路径断言。
- CLI/配置解析变更必须包含配置加载或执行相关测试。
- 禁止只为了覆盖率测试私有方法、内部字段或临时实现细节；除非该内部行为本身已成为业务契约。
- **测试抽象化**：同类功能使用参数化测试（`@pytest.mark.parametrize`）替代重复文件；新增 provider 只需扩展 `test_providers.py` 中的 `_PROVIDER_SPECS` 注册表。
- **测试整合性**：单功能测试数量 < 5 时应合并到同领域文件，禁止出现仅含 1-2 个测试的孤立文件。
- **基准化**：每类组件需有稳定的基准测试集（endpoint 正确性、内容解析、错误处理）。

## 回归防护

- 单元测试禁止真实网络调用，统一使用 `MockProvider`。
- 测试保持确定性（固定输出，无随机时间依赖）。
- 优先使用小型 fixture，并显式断言用户可观察的业务结果，例如响应内容、错误提示、持久化结果或预期 transcript。

## 测试编写参考

编写新测试时，使用 `write-tests` SKILL（`.github/skills/write-tests/SKILL.md`）获取完整规范：
- 标准 `OrchestrationPolicy` 配置（禁止开启积压静默批处理 / 后台任务）
- 速度红线：单测 < 1 秒，套件 < 30 秒
- `_run_live_turns` 辅助模式
- 常见陷阱速查表

## 若测试无法执行

- 说明具体阻塞原因。
- 提供用户可本地执行的准确命令。
- 标注新增但未执行的测试文件。
