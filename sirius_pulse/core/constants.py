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
DEFAULT_BASIC_MEMORY_HARD_LIMIT = 30
DEFAULT_BASIC_MEMORY_CONTEXT_WINDOW = 5
DEFAULT_DIARY_TOP_K = 5
DEFAULT_DIARY_TOKEN_BUDGET = 800
DEFAULT_DIARY_VOLUME_THRESHOLD = 8

# ── 传记相关 ──────────────────────────────────────────────
BIOGRAPHY_TOKEN_BUDGET = 500
BIOGRAPHY_MAX_MESSAGE_CHARS = 2000

# ── 反馈相关 ──────────────────────────────────────────────
FEEDBACK_TIMEOUT_SECONDS = 120
FEEDBACK_DIRECTED_THRESHOLD = 0.3

# ── 主动行为相关 ──────────────────────────────────────────
DEFAULT_PROACTIVE_CHECK_INTERVAL_SECONDS = 60
DEFAULT_MEMORY_PROMOTE_INTERVAL_SECONDS = 180
DEFAULT_CONSOLIDATION_INTERVAL_SECONDS = 600
