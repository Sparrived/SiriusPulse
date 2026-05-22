from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from sirius_pulse.config import TokenUsageRecord
from sirius_pulse.models import Transcript


class TokenUsageBucketDict(TypedDict):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class TokenUsageBaselineDict(TypedDict):
    total_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    avg_tokens_per_call: float
    avg_prompt_tokens_per_call: float
    avg_completion_tokens_per_call: float
    completion_to_prompt_ratio: float
    retry_rate: float


class TokenUsageSummary(TypedDict):
    baseline: TokenUsageBaselineDict
    by_actor: dict[str, TokenUsageBucketDict]
    by_task: dict[str, TokenUsageBucketDict]
    by_model: dict[str, TokenUsageBucketDict]


@dataclass(slots=True)
class TokenUsageBucket:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, record: TokenUsageRecord) -> None:
        self.calls += 1
        self.prompt_tokens += record.prompt_tokens
        self.completion_tokens += record.completion_tokens
        self.total_tokens += record.total_tokens

    def to_dict(self) -> TokenUsageBucketDict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(slots=True)
class TokenUsageBaseline:
    total_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    avg_tokens_per_call: float
    avg_prompt_tokens_per_call: float
    avg_completion_tokens_per_call: float
    completion_to_prompt_ratio: float
    retry_rate: float

    def to_dict(self) -> TokenUsageBaselineDict:
        return {
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "avg_tokens_per_call": self.avg_tokens_per_call,
            "avg_prompt_tokens_per_call": self.avg_prompt_tokens_per_call,
            "avg_completion_tokens_per_call": self.avg_completion_tokens_per_call,
            "completion_to_prompt_ratio": self.completion_to_prompt_ratio,
            "retry_rate": self.retry_rate,
        }


def _empty_baseline() -> TokenUsageBaseline:
    return TokenUsageBaseline(
        total_calls=0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        total_tokens=0,
        avg_tokens_per_call=0.0,
        avg_prompt_tokens_per_call=0.0,
        avg_completion_tokens_per_call=0.0,
        completion_to_prompt_ratio=0.0,
        retry_rate=0.0,
    )


def build_token_usage_baseline(records: list[TokenUsageRecord]) -> TokenUsageBaseline:
    if not records:
        return _empty_baseline()

    total_calls = len(records)
    total_prompt_tokens = sum(item.prompt_tokens for item in records)
    total_completion_tokens = sum(item.completion_tokens for item in records)
    total_tokens = sum(item.total_tokens for item in records)
    retried_calls = sum(1 for item in records if item.retries_used > 0)

    return TokenUsageBaseline(
        total_calls=total_calls,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        total_tokens=total_tokens,
        avg_tokens_per_call=total_tokens / total_calls,
        avg_prompt_tokens_per_call=total_prompt_tokens / total_calls,
        avg_completion_tokens_per_call=total_completion_tokens / total_calls,
        completion_to_prompt_ratio=(total_completion_tokens / total_prompt_tokens) if total_prompt_tokens else 0.0,
        retry_rate=retried_calls / total_calls,
    )


def summarize_token_usage(
    transcript: Transcript,
) -> TokenUsageSummary:
    by_actor: dict[str, TokenUsageBucket] = {}
    by_task: dict[str, TokenUsageBucket] = {}
    by_model: dict[str, TokenUsageBucket] = {}

    for record in transcript.token_usage_records:
        by_actor.setdefault(record.actor_id, TokenUsageBucket()).add(record)
        by_task.setdefault(record.task_name, TokenUsageBucket()).add(record)
        by_model.setdefault(record.model, TokenUsageBucket()).add(record)

    baseline = build_token_usage_baseline(transcript.token_usage_records)

    return {
        "baseline": baseline.to_dict(),
        "by_actor": {key: value.to_dict() for key, value in by_actor.items()},
        "by_task": {key: value.to_dict() for key, value in by_task.items()},
        "by_model": {key: value.to_dict() for key, value in by_model.items()},
    }
