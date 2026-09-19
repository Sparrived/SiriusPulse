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

from dataclasses import dataclass, field
from typing import Any

from sirius_pulse.core.intent import (
    RESOLUTION_TELL,
    Intention,
    IntentStore,
)

# Restlessness reaches its maximum after this much idle time since the last
# self-initiated episode.  It only ever *adds* to an intention's urgency.
_RESTLESSNESS_FULL_SECONDS = 6 * 60 * 60

# Size of the recent-kind memory used for the novelty term.
_RECENT_KIND_WINDOW = 3


@dataclass(slots=True)
class AutonomyDecision:
    """A non-LLM decision about whether to pursue an intention right now."""

    should_act: bool
    reason: str
    score: float
    threshold: float
    kind: str = ""
    seed: str = ""
    resolution: str = ""
    intention_id: str = ""
    audience: str = ""
    is_share: bool = False
    urgency: float = 0.0
    restlessness_score: float = 0.0
    novelty_score: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_act": self.should_act,
            "reason": self.reason,
            "score": round(self.score, 4),
            "threshold": round(self.threshold, 4),
            "kind": self.kind,
            "seed": self.seed,
            "resolution": self.resolution,
            "intention_id": self.intention_id,
            "audience": self.audience,
            "is_share": self.is_share,
            "urgency": round(self.urgency, 4),
            "restlessness_score": round(self.restlessness_score, 4),
            "novelty_score": round(self.novelty_score, 4),
            "context": dict(self.context),
        }


@dataclass(slots=True)
class Episode:
    """One thing the persona did on her own initiative.

    ``kind`` is a free-form label (reading / note / draft / share / musing ...),
    not an enum: autonomy is not restricted to producing "works".
    """

    episode_id: str
    started_at: str
    kind: str = "musing"
    seed: str = ""
    outcome: str = ""
    ended_at: str = ""
    intention_id: str = ""
    resolution: str = ""
    audience: str = ""
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
            "intention_id": self.intention_id,
            "resolution": self.resolution,
            "audience": self.audience,
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
            intention_id=str(data.get("intention_id", "")),
            resolution=str(data.get("resolution", "")),
            audience=str(data.get("audience", "")),
            refs=[str(item) for item in data.get("refs", []) or []],
            intensity=_clamp(data.get("intensity", 0.5)),
            status=str(data.get("status", "done")),
        )


class AutonomyPolicy:
    """Pure-rule autonomy policy for a persona acting on her own.

    The policy is an *intention gate*, not a motivation generator: it scores the
    intentions she already formed and picks at most one to pursue.  Nothing in
    here can invent a reason to act, so an empty or fully-faded intention set
    reliably yields "do nothing" no matter how long she has been idle.
    """

    def evaluate(
        self,
        *,
        seconds_since_episode: float,
        intentions: IntentStore | list[Intention] | None = None,
        recent_kinds: list[str] | None = None,
        expressiveness: float = 0.5,
        now: str = "",
    ) -> AutonomyDecision:
        """Decide whether to pursue one intention, without calling an LLM.

        There is no daily quota: she may act whenever she is actually carrying
        something.  What limits her is not a counter but the intentions
        themselves — an empty or faded set yields "do nothing" on its own.

        Args:
            seconds_since_episode: Idle time since the last self-initiated episode.
            intentions: The intentions she is carrying.
            recent_kinds: Kinds of the most recent episodes, newest first.
            expressiveness: Persona trait (0-1) shifting the threshold.
            now: Reference timestamp for urgency decay.
        """
        restlessness = self._restlessness_score(seconds_since_episode)
        threshold = self._threshold(expressiveness)
        open_items = self._open_intentions(intentions, now=now)

        if not open_items:
            # Nothing is being carried: she does not get to act out of boredom.
            # Intentions form in real turns (intend_pursue / intend_share), not here.
            return self._decision(False, "no_intention", 0.0, threshold, restlessness=restlessness)

        best = max(
            open_items,
            key=lambda item: self._intention_score(item, restlessness=restlessness, now=now),
        )
        urgency = best.effective_urgency(now=now)
        novelty = self._novelty_score(best.kind, recent_kinds or [])
        # Motivation comes from her own urgency; idleness only nudges it.  A
        # stale intention cannot be revived by waiting alone.
        score = _clamp(0.70 * urgency + 0.20 * restlessness + 0.10 * novelty)
        should_act = score >= threshold and not best.is_expired(now=now)
        reason = (
            "pursuing_intention"
            if should_act
            else self._suppression_reason(urgency, threshold, score)
        )
        return self._decision(
            should_act,
            reason,
            score,
            threshold,
            kind=best.kind,
            seed=best.what,
            resolution=best.resolution,
            intention_id=best.intention_id,
            audience=best.audience,
            is_share=best.resolution == RESOLUTION_TELL,
            urgency=urgency,
            restlessness=restlessness,
            novelty=novelty,
            context={"open_intentions": len(open_items)},
        )

    @staticmethod
    def _open_intentions(
        intentions: IntentStore | list[Intention] | None, *, now: str
    ) -> list[Intention]:
        if intentions is None:
            return []
        if isinstance(intentions, IntentStore):
            return intentions.open_items(now=now)
        return [item for item in intentions if item.is_open and not item.is_expired(now=now)]

    @classmethod
    def _intention_score(cls, intention: Intention, *, restlessness: float, now: str) -> float:
        return _clamp(0.70 * intention.effective_urgency(now=now) + 0.20 * restlessness)

    def _decision(
        self,
        should_act: bool,
        reason: str,
        score: float,
        threshold: float,
        *,
        kind: str = "",
        seed: str = "",
        resolution: str = "",
        intention_id: str = "",
        audience: str = "",
        is_share: bool = False,
        urgency: float = 0.0,
        restlessness: float = 0.0,
        novelty: float = 0.0,
        context: dict[str, Any] | None = None,
    ) -> AutonomyDecision:
        return AutonomyDecision(
            should_act=should_act,
            reason=reason,
            score=score,
            threshold=threshold,
            kind=kind,
            seed=seed,
            resolution=resolution,
            intention_id=intention_id,
            audience=audience,
            is_share=is_share,
            urgency=urgency,
            restlessness_score=restlessness,
            novelty_score=novelty,
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
    def _novelty_score(kind: str, recent_kinds: list[str]) -> float:
        window = [str(item) for item in recent_kinds[:_RECENT_KIND_WINDOW]]
        return 0.35 if kind in window else 1.0

    @staticmethod
    def _threshold(expressiveness: float) -> float:
        value = _clamp(expressiveness)
        if value >= 0.65:
            return 0.42
        if value <= 0.35:
            return 0.58
        return 0.50

    @staticmethod
    def _suppression_reason(urgency: float, threshold: float, score: float) -> str:
        if urgency < 0.15:
            return "intention_faded"
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
]
