"""Rule-based autonomy policy.

This is the deliberate twin of :mod:`sirius_pulse.core.participation`: a pure
rule, explainable, non-LLM gate.  ``ParticipationPolicy`` answers "should I react
to someone else's message"; ``AutonomyPolicy`` answers "should I start doing
something of my own accord, and what".

Design invariant: the default outcome is *do nothing*.  A tick that cannot
answer "do nothing" is a cron job, not motivation.  Only when this policy
returns ``should_act`` is an LLM turn worth paying for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Restlessness reaches its maximum after this much idle time since the last
# self-initiated episode.  Longer waits do not score higher.
_RESTLESSNESS_FULL_SECONDS = 6 * 60 * 60

# Size of the recent-kind memory used for the novelty term.
_RECENT_KIND_WINDOW = 3

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_IMAGE_RE = re.compile(r"\[图片|\[动画表情|表情包|图里|截图|图片描述")
_CODE_RE = re.compile(r"(代码|项目|重构|函数|脚本|报错|异常|接口|部署|bug|error|traceback|refactor)", re.IGNORECASE)
_QUESTION_RE = re.compile(r"[?？]|(怎么|为什么|如何|哪个|有没有)")

# Seed weights: an unfinished episode is the strongest reason to continue,
# a link or a technical thread is next, a picture or an open question is weaker.
_WEIGHT_ACTIVE_EPISODE = 1.0
_WEIGHT_LINK_OR_CODE = 0.7
_WEIGHT_IMAGE_OR_QUESTION = 0.5
_WEIGHT_PLAIN = 0.3


@dataclass(slots=True)
class AutonomyDecision:
    """A non-LLM decision about whether to start an episode, and which one."""

    should_act: bool
    reason: str
    score: float
    threshold: float
    kind: str = ""
    seed: str = ""
    restlessness_score: float = 0.0
    curiosity_score: float = 0.0
    novelty_score: float = 0.0
    budget_pressure: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_act": self.should_act,
            "reason": self.reason,
            "score": round(self.score, 4),
            "threshold": round(self.threshold, 4),
            "kind": self.kind,
            "seed": self.seed,
            "restlessness_score": round(self.restlessness_score, 4),
            "curiosity_score": round(self.curiosity_score, 4),
            "novelty_score": round(self.novelty_score, 4),
            "budget_pressure": round(self.budget_pressure, 4),
            "context": dict(self.context),
        }


@dataclass(slots=True)
class Episode:
    """One thing the persona did on her own initiative.

    ``kind`` is a free-form label (reading / note / draft / image / musing ...),
    not an enum: autonomy is not restricted to producing "works".
    """

    episode_id: str
    started_at: str
    kind: str = "musing"
    seed: str = ""
    outcome: str = ""
    ended_at: str = ""
    refs: list[str] = field(default_factory=list)
    intensity: float = 0.5
    status: str = "done"

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "kind": self.kind,
            "seed": self.seed,
            "outcome": self.outcome,
            "refs": list(self.refs),
            "intensity": round(self.intensity, 4),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Episode":
        return cls(
            episode_id=str(data.get("episode_id", "")),
            started_at=str(data.get("started_at", "")),
            kind=str(data.get("kind", "musing")),
            seed=str(data.get("seed", "")),
            outcome=str(data.get("outcome", "")),
            ended_at=str(data.get("ended_at", "")),
            refs=[str(item) for item in data.get("refs", []) or []],
            intensity=_clamp(data.get("intensity", 0.5)),
            status=str(data.get("status", "done")),
        )


def infer_kind(text: str) -> str:
    """Map a piece of encountered material to a free-form episode label."""
    sample = str(text or "")
    if _URL_RE.search(sample):
        return "reading"
    if _CODE_RE.search(sample):
        return "building"
    if _IMAGE_RE.search(sample):
        return "image"
    if _QUESTION_RE.search(sample):
        return "note"
    return "musing"


def build_seed(text: str, *, kind: str = "", weight: float = 0.0, source: str = "chat") -> dict:
    """Build one autonomy seed from material the persona already encountered."""
    sample = " ".join(str(text or "").split())[:120]
    resolved_kind = kind or infer_kind(sample)
    if weight <= 0:
        if resolved_kind == "reading" or resolved_kind == "building":
            weight = _WEIGHT_LINK_OR_CODE
        elif resolved_kind in {"image", "note"}:
            weight = _WEIGHT_IMAGE_OR_QUESTION
        else:
            weight = _WEIGHT_PLAIN
    return {
        "kind": resolved_kind,
        "seed": sample,
        "weight": _clamp(weight),
        "source": source,
    }


class AutonomyPolicy:
    """Pure-rule autonomy policy for a persona acting on her own."""

    def evaluate(
        self,
        *,
        seconds_since_episode: float,
        seeds: list[dict[str, Any]],
        recent_kinds: list[str] | None = None,
        episodes_today: int = 0,
        daily_episode_budget: int = 3,
        expressiveness: float = 0.5,
    ) -> AutonomyDecision:
        """Decide whether to start an episode, without calling an LLM.

        Args:
            seconds_since_episode: Idle time since the last self-initiated episode.
            seeds: Candidate material, each ``{kind, seed, weight, source}``.
            recent_kinds: Kinds of the most recent episodes, newest first.
            episodes_today: Episodes already started today.
            daily_episode_budget: Hard daily cap; reaching it suppresses autonomy.
            expressiveness: Persona trait (0-1) shifting the threshold.
        """
        valid_seeds = [seed for seed in seeds if str(seed.get("seed", "")).strip()]
        if not valid_seeds:
            return self._decision(
                False,
                "no_seed",
                0.0,
                self._threshold(expressiveness),
                restlessness=self._restlessness_score(seconds_since_episode),
            )

        best = max(valid_seeds, key=lambda item: float(item.get("weight", 0.0)))
        kind = str(best.get("kind", "musing"))
        restlessness = self._restlessness_score(seconds_since_episode)
        curiosity = self._curiosity_score(valid_seeds)
        novelty = self._novelty_score(kind, recent_kinds or [])
        budget_pressure = self._budget_pressure(episodes_today, daily_episode_budget)
        threshold = self._threshold(expressiveness)

        score = 0.50 * restlessness + 0.35 * curiosity + 0.15 * novelty - 0.45 * budget_pressure
        score = _clamp(score)
        should_act = score >= threshold
        reason = (
            "self_initiated"
            if should_act
            else self._suppression_reason(
                restlessness, curiosity, novelty, budget_pressure, score, threshold
            )
        )
        return self._decision(
            should_act,
            reason,
            score,
            threshold,
            kind=kind if should_act else "",
            seed=str(best.get("seed", "")) if should_act else "",
            restlessness=restlessness,
            curiosity=curiosity,
            novelty=novelty,
            budget_pressure=budget_pressure,
            context={"seed_count": len(valid_seeds), "episodes_today": episodes_today},
        )

    def _decision(
        self,
        should_act: bool,
        reason: str,
        score: float,
        threshold: float,
        *,
        kind: str = "",
        seed: str = "",
        restlessness: float = 0.0,
        curiosity: float = 0.0,
        novelty: float = 0.0,
        budget_pressure: float = 0.0,
        context: dict[str, Any] | None = None,
    ) -> AutonomyDecision:
        return AutonomyDecision(
            should_act=should_act,
            reason=reason,
            score=score,
            threshold=threshold,
            kind=kind,
            seed=seed,
            restlessness_score=restlessness,
            curiosity_score=curiosity,
            novelty_score=novelty,
            budget_pressure=budget_pressure,
            context=dict(context or {}),
        )

    @staticmethod
    def _restlessness_score(seconds_since_episode: float) -> float:
        try:
            idle = max(0.0, float(seconds_since_episode))
        except (TypeError, ValueError):
            idle = 0.0
        return _clamp(idle / _RESTLESSNESS_FULL_SECONDS)

    @staticmethod
    def _curiosity_score(seeds: list[dict[str, Any]]) -> float:
        weights = sorted(
            (_clamp(seed.get("weight", 0.0)) for seed in seeds),
            reverse=True,
        )[:_RECENT_KIND_WINDOW]
        if not weights:
            return 0.0
        return _clamp(sum(weights) / len(weights))

    @staticmethod
    def _novelty_score(kind: str, recent_kinds: list[str]) -> float:
        window = [str(item) for item in recent_kinds[:_RECENT_KIND_WINDOW]]
        return 0.35 if kind in window else 1.0

    @staticmethod
    def _budget_pressure(episodes_today: int, daily_episode_budget: int) -> float:
        try:
            budget = int(daily_episode_budget)
            used = int(episodes_today)
        except (TypeError, ValueError):
            return 0.0
        if budget <= 0:
            return 1.0
        return _clamp(used / budget)

    @staticmethod
    def _threshold(expressiveness: float) -> float:
        value = _clamp(expressiveness)
        if value >= 0.65:
            return 0.47
        if value <= 0.35:
            return 0.63
        return 0.55

    @staticmethod
    def _suppression_reason(
        restlessness: float,
        curiosity: float,
        novelty: float,
        budget_pressure: float,
        score: float,
        threshold: float,
    ) -> str:
        if budget_pressure >= 1.0:
            return "daily_budget_exhausted"
        if restlessness < 0.15:
            return "not_restless_yet"
        if curiosity < 0.2:
            return "nothing_worth_pursuing"
        if score < threshold:
            return "below_autonomy_threshold"
        return "suppressed"


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


__all__ = [
    "AutonomyDecision",
    "AutonomyPolicy",
    "Episode",
    "build_seed",
    "infer_kind",
]
