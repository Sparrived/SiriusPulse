# 模型提供者系统（Provider System）

> **LLM 调用层** — 统一接口、自动路由、健康检查。

## 一句话定位

Provider 系统负责**把引擎的生成请求翻译成对具体 LLM 服务的 HTTP 调用**，并支持多 provider 共存时的自动路由。

## 核心抽象

```python
# 异步接口（推荐，全链路 httpx）
class LLMProvider(Protocol):
    def generate(self, request: GenerationRequest) -> str: ...

class AsyncLLMProvider(Protocol):
    async def generate_async(self, request: GenerationRequest) -> str: ...
```

> **v1.1 变更**：`OpenAICompatibleProvider.generate()` 已改为异步 httpx 实现，移除了原来的 `urllib.request` 同步阻塞调用。`MockProvider` 和 `AutoRoutingProvider` 同步适配异步接口。

所有 provider 都接收同一个 `GenerationRequest`：

```python
@dataclass
class GenerationRequest:
    model: str           # 模型名称，如 "gpt-4o"
    system_prompt: str   # 系统提示词
    messages: list       # 对话历史
    temperature: float   # 0~2
    max_tokens: int      # 最大输出长度
    timeout_seconds: int # 超时时间
    purpose: str         # 调用目的（emotion_analyze / intent_analyze / response_generate...）
```

## 支持的 Provider 平台

| 平台 | 类名 | 说明 |
|------|------|------|
| **OpenAI-Compatible** | `OpenAICompatibleProvider` | 通用 OpenAI 格式，兼容大多数国产平台和自托管服务 |
| **DeepSeek** | `DeepSeekProvider` | 深度求索官方 API |
| **阿里云百炼** | `AliyunBailianProvider` | 阿里云大模型平台 |
| **智谱 AI** | `BigModelProvider` | GLM 系列模型 |
| **SiliconFlow** | `SiliconFlowProvider` | 硅基流动 |
| **火山方舟** | `VolcengineArkProvider` | 字节跳动火山引擎 |
| **Mock** | `MockProvider` | 测试用，确定性返回预设内容 |

所有具体 provider 都继承自 `OpenAICompatibleProvider` 或实现 `LLMProvider` 协议。

## 自动路由（AutoRoutingProvider）

当配置了多个 provider（比如 DeepSeek + 阿里云），`AutoRoutingProvider` 会根据 `request.model` 自动选择用哪个 provider：

```python
# provider_keys.json 示例
{
    "deepseek": {
        "provider_type": "deepseek",
        "api_key": "sk-...",
        "models": ["deepseek-chat", "deepseek-reasoner"]
    },
    "aliyun": {
        "provider_type": "aliyun-bailian",
        "api_key": "sk-...",
        "models": ["qwen-max", "qwen-plus"]
    }
}
```

路由规则：
1. 遍历所有启用的 provider
2. 优先匹配：`models` 列表包含 `request.model`
3. 次优匹配：`healthcheck_model` 精确等于 `request.model`
4. 无匹配：报错，提示用户把模型加到对应 provider 的 `models` 列表

**为什么每次请求都重新创建 provider 实例**：保持状态最小化，避免连接池复杂性。单次实例创建的开销在 HTTP 调用面前可忽略。

## 模型路由（ModelRouter）

`AutoRoutingProvider` 解决"**用哪个 provider**"，`ModelRouter` 解决"**用哪个模型**".

引擎内部按任务类型选择模型：

| 任务 | 默认模型 | temperature | fallback |
|------|---------|-------------|----------|
| 情感分析 | gpt-4o-mini | 0.3 | deepseek-chat |
| 意图分析 | gpt-4o-mini | 0.3 | deepseek-chat |
| 记忆提取 | gpt-4o-mini | 0.3 | deepseek-chat |
| 回复生成 | gpt-4o | 0.7 | deepseek-reasoner |
| 主动发言 | gpt-4o | 0.8 | deepseek-chat |
| 共情生成 | gpt-4o | 0.6 | deepseek-chat |

**动态调整**：
- `urgency > 80`：升级更强模型，降低 temperature（更严谨）
- `urgency > 95`：用最强模型，temperature 压到 0.1
- `heat_level = overheated`：max_tokens 减半（群里刷屏时短回复）
- `user_style = concise`：max_tokens 封顶 80

## 本地图片自动转 base64

当消息包含本地图片路径时，`prepare_openai_compatible_messages()` 会自动读取文件并转为 base64 Data URL：

```python
# 输入
{"role": "user", "content": [{"type": "image", "url": "C:\\screenshot.png"}]}

# 输出（自动转换）
{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KG..."}}]}
```

这意味着任何 OpenAI-Compatible 的 provider（包括自托管）都能直接消费本地图片，不需要额外的文件上传 API。

## Thinking 模型默认关闭

部分平台（DeepSeek、智谱）支持 reasoning/thinking 模式。框架默认禁用：

```python
# DeepSeek
{"thinking": {"type": "disabled"}}

# 阿里云 / SiliconFlow
{"enable_thinking": False}
```

避免 AI 输出中间推理过程，保持回复干净。

## Provider 注册流程

```python
from sirius_chat.providers import register_provider_with_validation, probe_provider_availability

# 1. 注册
register_provider_with_validation(
    registry=runtime._provider_manager,
    name="my_deepseek",
    provider_type="deepseek",
    api_key="sk-...",
    models=["deepseek-chat", "deepseek-reasoner"],
)

# 2. 健康检查
available = probe_provider_availability(provider_config)
```

注册时会：
1. 规范化 provider_type（"openai" → "openai-compatible"）
2. 校验平台是否受支持
3. 发送 ping 请求验证连通性
4. 原子写入 `provider_keys.json`

## MockProvider（测试专用）

`MockProvider` 用于 600+ 单元测试：
- 维护一个响应队列，按 FIFO 出队
- 记录每次调用的 `GenerationRequest`（供断言）
- 特殊处理事件验证请求（返回结构化 JSON stub）
- 队列为空时返回 `"[mock] no configured response"`

所有测试不依赖网络，<15 秒跑完全套。
