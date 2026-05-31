"""Skill execution telemetry — lightweight usage statistics and observability.

Records are appended as JSON Lines to {work_path}/skill_data/.telemetry.jsonl
so they can be tail -f'd or queried without locking the whole file.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SkillExecutionRecord:
    """A single skill execution event."""

    skill_name: str
    timestamp: float  # time.time()
    success: bool
    duration_ms: float
    error: str = ""
    caller_user_id: str = ""
    params: dict[str, Any] | None = None
    result_summary: str = ""


class SkillTelemetry:
    """Append-only JSONL telemetry store for skill executions."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, record: SkillExecutionRecord) -> None:
        """Append a record atomically."""
        try:
            line = json.dumps(asdict(record), ensure_ascii=False, default=str) + "\n"
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            # Telemetry is best-effort; never crash the skill because of it
            pass

    def query(
        self,
        *,
        skill_name: str | None = None,
        success: bool | None = None,
        since: float = 0,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SkillExecutionRecord], int]:
        """Read records with optional filtering and pagination.

        JSONL 文件按追加顺序存储（最旧在前），本方法从末尾分页返回最新记录。
        ``offset=0`` 表示最新一页，``offset=limit`` 表示上一页，以此类推。

        Returns:
            (当前页记录列表, 符合条件的总记录数)
        """
        results: list[SkillExecutionRecord] = []
        if not self._path.exists():
            return results, 0
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if skill_name is not None and data.get("skill_name") != skill_name:
                        continue
                    if success is not None and data.get("success") != success:
                        continue
                    if data.get("timestamp", 0) < since:
                        continue
                    results.append(
                        SkillExecutionRecord(
                            skill_name=data.get("skill_name", ""),
                            timestamp=data.get("timestamp", 0.0),
                            success=data.get("success", False),
                            duration_ms=data.get("duration_ms", 0.0),
                            error=data.get("error", ""),
                            caller_user_id=data.get("caller_user_id", ""),
                            params=data.get("params"),
                            result_summary=data.get("result_summary", ""),
                        )
                    )
        except OSError:
            pass

        total = len(results)
        if offset >= total:
            return [], total
        # 从末尾分页：offset=0 → 最新一页，offset=limit → 上一页
        end = total - offset
        start = max(0, end - limit)
        return results[start:end], total

    def summary(self, since: float = 0) -> dict[str, Any]:
        """Return aggregate statistics per skill."""
        stats: dict[str, dict[str, Any]] = {}
        records, _ = self.query(since=since, limit=10_000, offset=0)
        for rec in records:
            s = stats.setdefault(
                rec.skill_name,
                {"calls": 0, "successes": 0, "failures": 0, "total_ms": 0.0, "errors": []},
            )
            s["calls"] += 1
            if rec.success:
                s["successes"] += 1
            else:
                s["failures"] += 1
                if rec.error and len(s["errors"]) < 5:
                    s["errors"].append(rec.error)
            s["total_ms"] += rec.duration_ms
        for s in stats.values():
            if s["calls"]:
                s["avg_ms"] = round(s["total_ms"] / s["calls"], 2)
        return stats
