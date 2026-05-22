"""Persona store: simple JSON persistence for PersonaProfile.

Path: {work_path}/engine_state/persona.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sirius_pulse.models.persona import PersonaProfile

logger = logging.getLogger(__name__)


class PersonaStore:
    """Persists and loads PersonaProfile to/from disk."""

    @staticmethod
    def _path(work_path: Any) -> Path:
        p = Path(work_path)
        return p / "persona.json"

    @classmethod
    def load(cls, work_path: Any) -> PersonaProfile | None:
        """Load persona from disk. Returns None if not found."""
        path = cls._path(work_path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PersonaProfile.from_dict(data)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load persona from %s: %s", path, exc)
            return None

    @classmethod
    def save(cls, work_path: Any, persona: PersonaProfile) -> None:
        """Save persona to disk (atomic write)."""
        path = cls._path(work_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(persona.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.debug("Persona saved to %s", path)
