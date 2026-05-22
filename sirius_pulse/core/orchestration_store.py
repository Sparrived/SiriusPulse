"""Orchestration store: persists model/task configuration for engine.

Path: {work_path}/engine_state/orchestration.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class OrchestrationStore:
    """Persists and loads orchestration config to/from disk."""

    @staticmethod
    def _path(work_path: Any) -> Path:
        p = Path(work_path)
        return p / "engine_state" / "orchestration.json"

    @classmethod
    def load(cls, work_path: Any) -> dict[str, Any]:
        """Load orchestration config from disk. Returns empty dict if not found."""
        path = cls._path(work_path)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            logger.warning("Invalid orchestration.json format at %s", path)
            return {}
        except (OSError, json.JSONDecodeError) as exc:
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
        tmp.replace(path)
        logger.debug("Orchestration saved to %s", path)
