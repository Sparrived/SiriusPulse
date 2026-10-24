"""Orchestration store: persists model/task configuration for engine.

Path: {work_path}/engine_state/orchestration.json
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from sirius_pulse.utils.json_io import replace_with_retry

logger = logging.getLogger(__name__)


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite orchestration number: {value}")
    return parsed


class OrchestrationStore:
    """Persists and loads orchestration config to/from disk."""

    @staticmethod
    def _path(work_path: Any) -> Path:
        p = Path(work_path)
        return p / "engine_state" / "orchestration.json"

    @classmethod
    def load(cls, work_path: Any, *, strict: bool = False) -> dict[str, Any]:
        """Load config, retaining the legacy empty-dict fallback unless strict.

        Strict reads distinguish a saved empty config from a missing/unreadable
        or invalid file, so a failed hot reload cannot reset live settings.
        """
        path = cls._path(work_path)
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
                parse_float=_finite_json_float if strict else float,
                parse_constant=_finite_json_float if strict else float,
            )
            if not isinstance(data, dict):
                raise ValueError(f"Invalid orchestration.json format at {path}: expected object")
            return data
        except FileNotFoundError:
            if strict:
                raise
            return {}
        except (OSError, ValueError) as exc:
            if strict:
                raise
            logger.warning("Failed to load orchestration from %s: %s", path, exc)
            return {}

    @classmethod
    def save(cls, work_path: Any, config: dict[str, Any]) -> None:
        """Save orchestration config to disk (atomic write)."""
        path = cls._path(work_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        replace_with_retry(tmp, path)
        logger.debug("Orchestration saved to %s", path)
