"""核心引擎常量定义。

集中管理各模块共用的魔法数字，提高可读性和可维护性。
"""

from __future__ import annotations

# ── 时间相关 ──────────────────────────────────────────────
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

# 回复去重窗口（秒）
REPLY_DEDUP_WINDOW_SECONDS = 300

# 冷却检测：群组沉默阈值（秒）
SILENCE_THRESHOLD_SECONDS = 300

# 冷却检测：热度阈值
COLD_HEAT_THRESHOLD = 0.25

# 心跳超时（秒）
HEARTBEAT_TIMEOUT_SECONDS = 30

# Embedding 服务默认端口
EMBEDDING_DEFAULT_PORT = 18900

# ── Token 相关 ────────────────────────────────────────────
DEFAULT_MAX_TOKENS = 512
RESPONSE_MAX_TOKENS = 4096
DIARY_GENERATION_MAX_TOKENS = 2048
COGNITION_MAX_TOKENS = 1024

# ── 记忆相关 ──────────────────────────────────────────────
# 原始消息保留到被 checkpoint 记忆单元覆盖为止（见提交 0866054）。保留量由下面的
# token 触发阈值主导；DEFAULT_BASIC_MEMORY_HARD_LIMIT 只是高于该阈值的内存兜底上限，
# 正常运行时会先触发 token 压缩，不会先按条数丢消息。
DEFAULT_BASIC_MEMORY_HARD_LIMIT = 10_000
DEFAULT_BASIC_MEMORY_CONTEXT_WINDOW = 5

# 基础记忆压缩：原始窗口超过 TRIGGER 时开始归纳成记忆单元，归纳到 TARGET 为止；
# 被覆盖的原始条目随后从活跃窗口移除，改由记忆单元 RAG 提供摘要。
DEFAULT_BASIC_MEMORY_CHECKPOINT_TOKEN_TRIGGER = 80_000
DEFAULT_BASIC_MEMORY_CHECKPOINT_TOKEN_TARGET = 20_000
# Keep extraction prompts bounded. Each candidate can contain up to 500 characters
# plus provenance fields, so 64 candidates can exhaust a provider's context before
# the JSON response is generated.
DEFAULT_BASIC_MEMORY_CHECKPOINT_BATCH_SIZE = 32

# 注入聊天提示词的对话历史 token 上限。默认与压缩触发阈值一致：正常运行时整个原始
# 窗口（约 2 万~8 万 token）都会进入提示词，与压缩设计保持一致；该预算只在窗口异常
# 膨胀（例如后台归纳被长时间拖住）时兜底，避免再次把提示词和宿主内存推向失控。
DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET = DEFAULT_BASIC_MEMORY_CHECKPOINT_TOKEN_TRIGGER
DEFAULT_DIARY_TOP_K = 5
DEFAULT_DIARY_TOKEN_BUDGET = 800
DEFAULT_DIARY_VOLUME_THRESHOLD = 8

# ── 传记相关 ──────────────────────────────────────────────
BIOGRAPHY_TOKEN_BUDGET = 500
BIOGRAPHY_MAX_MESSAGE_CHARS = 2000

# ── 反馈相关 ──────────────────────────────────────────────
FEEDBACK_TIMEOUT_SECONDS = 120
FEEDBACK_DIRECTED_THRESHOLD = 0.3

# ── 后台任务相关 ──────────────────────────────────────────
DEFAULT_MEMORY_PROMOTE_INTERVAL_SECONDS = 180
DEFAULT_CONSOLIDATION_INTERVAL_SECONDS = 600
