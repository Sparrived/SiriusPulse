"""Checkpoint memory unit generation."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.units.models import MemoryUnit, MemoryUnitGenerationResult

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a memory checkpoint worker.

Extract compact, third-person memory units from the provided chat records.
Do not write diary prose. Do not use first-person narration. Do not mention
reading history, checking logs, remembering, or looking back. Keep only facts
that may affect future replies: events, preferences, relationships, boundaries,
group norms, terminology, unresolved plans, and durable context.

Return strict JSON:
{
  "units": [
    {
      "type": "event|preference|relationship|boundary|group_norm|term|profile_candidate|note",
      "scope": "group|user|persona|global",
      "scope_id": "stable id when scope is user/group, otherwise empty",
      "summary": "one factual sentence, max 80 Chinese chars or 120 English chars",
      "participants": ["stable user ids or display names"],
      "topics": ["short topic"],
      "keywords": ["short keyword"],
      "salience": 0.0,
      "confidence": 0.0,
      "lifespan": "short|medium|long",
      "should_prompt": true,
      "source_indices": [1, 2]
    }
  ]
}
"""


def _build_user_prompt(
    persona_name: str,
    persona_description: str,
    candidates: list[BasicMemoryEntry],
) -> str:
    lines: list[str] = []
    for index, entry in enumerate(candidates, 1):
        name = entry.speaker_name or entry.user_id or "unknown"
        role = entry.role or "human"
        content = (entry.content or "").replace("\n", " ").strip()
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(
            f"{index}. source_id={entry.entry_id} user_id={entry.user_id} "
            f"speaker={name} role={role}: {content}"
        )

    persona_line = f"Persona: {persona_name}. {persona_description}".strip()
    return (
        f"{persona_line}\n\n"
        "Chat records:\n"
        + "\n".join(lines)
        + "\n\nExtract memory units. Prefer fewer high-signal units over many vague units."
    )


class MemoryUnitGenerator:
    """Generates structured memory units from basic memory candidates."""

    async def generate(
        self,
        *,
        group_id: str,
        candidates: list[BasicMemoryEntry],
        persona_name: str,
        persona_description: str,
        brain: Any,
        model_name: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        max_retries: int = 1,
    ) -> MemoryUnitGenerationResult | None:
        if not candidates:
            return None

        from sirius_pulse.core.brain import RawRequest

        user_prompt = _build_user_prompt(persona_name, persona_description, candidates)
        index_to_source = {idx: entry.entry_id for idx, entry in enumerate(candidates, 1)}
        all_source_ids = {entry.entry_id for entry in candidates}

        parsed: dict[str, Any] | None = None
        system_prompt = _SYSTEM_PROMPT
        for attempt in range(max_retries + 1):
            raw_request = RawRequest(
                model=model_name,
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                purpose="memory_unit_extract",
                response_format={"type": "json_object"},
            )
            try:
                raw = await brain.raw_call(raw_request)
            except Exception as exc:
                logger.warning("Memory unit generation failed for group %s: %s", group_id, exc)
                return None

            parsed = self._parse_response(raw)
            if parsed is not None:
                break
            if attempt < max_retries:
                system_prompt = _SYSTEM_PROMPT + "\nReturn only valid JSON, no markdown."

        if parsed is None:
            return None

        now_iso = datetime.now(timezone.utc).isoformat()
        units: list[MemoryUnit] = []
        for item in parsed.get("units", []):
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            if not summary:
                continue

            source_ids = self._resolve_source_ids(item, index_to_source, all_source_ids)
            if not source_ids:
                continue

            unit = MemoryUnit(
                unit_id=f"mem_{uuid.uuid4().hex[:12]}",
                group_id=group_id,
                created_at=now_iso,
                unit_type=self._clean_choice(item.get("type"), "event"),
                scope=self._clean_choice(item.get("scope"), "group"),
                scope_id=str(item.get("scope_id") or "").strip()[:80],
                summary=summary[:180],
                participants=self._clean_list(item.get("participants"), limit=8),
                topics=self._clean_list(item.get("topics"), limit=8),
                keywords=self._clean_list(item.get("keywords"), limit=12),
                salience=self._clean_float(item.get("salience"), default=0.5),
                confidence=self._clean_float(item.get("confidence"), default=0.7),
                lifespan=self._clean_choice(item.get("lifespan"), "medium"),
                should_prompt=bool(item.get("should_prompt", True)),
                source_ids=source_ids,
            )
            units.append(unit)

        return MemoryUnitGenerationResult(units=units)

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any] | None:
        text = raw.strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Memory unit response is not valid JSON")
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _resolve_source_ids(
        item: dict[str, Any],
        index_to_source: dict[int, str],
        all_source_ids: set[str],
    ) -> list[str]:
        resolved: list[str] = []
        for raw_index in item.get("source_indices") or []:
            try:
                source_id = index_to_source.get(int(raw_index))
            except (TypeError, ValueError):
                source_id = None
            if source_id and source_id not in resolved:
                resolved.append(source_id)
        for raw_id in item.get("source_ids") or []:
            source_id = str(raw_id).strip()
            if source_id in all_source_ids and source_id not in resolved:
                resolved.append(source_id)
        return resolved

    @staticmethod
    def _clean_list(value: Any, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text[:80])
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _clean_float(value: Any, *, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0.0, min(1.0, parsed))

    @staticmethod
    def _clean_choice(value: Any, default: str) -> str:
        text = str(value or "").strip().lower()
        return text[:40] if text else default
