"""Read-only search over long-term memory units and archived chat messages.

The regular prompt path only injects a small, ranked slice of memory.  This
module lets the persona actively look up older or retired units and exact lines
from the current chat's archive when the user refers to an old agreement,
identity, or relationship.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.memory.units.store import MemoryUnitFileStore

logger = logging.getLogger(__name__)

_SAFE_GROUP = re.compile(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_MESSAGE_CHARS = 800
_MAX_SCAN_LINES = 400
_MAX_SOURCE_IDS = 8


def _safe_group_id(group_id: str) -> str:
    base = _SAFE_GROUP.sub("_", str(group_id or "").strip())
    base = re.sub(r"_+", "_", base).strip("_")
    return base or "default"


def _parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if _DATE_ONLY.fullmatch(text):
            parsed = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _bounds(since: str, until: str) -> tuple[datetime | None, datetime | None]:
    start = _parse_time(since) if str(since or "").strip() else None
    end = _parse_time(until) if str(until or "").strip() else None
    if str(until or "").strip() and end is not None and _DATE_ONLY.fullmatch(str(until).strip()):
        end += timedelta(days=1)
    if str(since or "").strip() and start is None:
        raise ValueError("since 不是有效的日期或时间")
    if str(until or "").strip() and end is None:
        raise ValueError("until 不是有效的日期或时间")
    return start, end


def _within(value: str, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    parsed = _parse_time(value)
    if parsed is None:
        return False
    return (start is None or parsed >= start) and (end is None or parsed < end)


def _bounded_text(value: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _unit_hit(unit: MemoryUnit, score: float = 0.0) -> dict[str, Any]:
    return {
        "unit_id": unit.unit_id,
        "group_id": unit.group_id,
        "scope": unit.scope,
        "summary": _bounded_text(unit.summary, 500),
        "event_time": unit.event_time,
        "created_at": unit.created_at,
        "topics": list(unit.topics),
        "keywords": list(unit.keywords),
        "participants": list(unit.participants)[:12],
        "source_ids": list(unit.source_ids)[:_MAX_SOURCE_IDS],
        "salience": unit.salience,
        "confidence": unit.confidence,
        "active": bool(unit.should_prompt),
        "retired_reason": str(unit.metadata.get("retired") or ""),
        "score": round(float(score), 4),
    }


def _entry_hit(entry: BasicMemoryEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "timestamp": entry.timestamp,
        "user_id": entry.user_id,
        "speaker_name": entry.speaker_name,
        "role": entry.role,
        "sender_type": entry.sender_type,
        "platform_message_id": entry.platform_message_id,
        "content": _bounded_text(entry.content),
    }


class MemorySearchService:
    """Search persisted units and the current chat's append-only archive."""

    def __init__(self, work_path: str | Path) -> None:
        self.work_path = Path(work_path)

    def search(
        self,
        *,
        group_id: str,
        query: str = "",
        source: str = "both",
        limit: int = 6,
        include_global: bool = False,
        since: str = "",
        until: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        if not str(group_id or "").strip():
            return {"success": False, "error": "缺少当前会话上下文"}
        if source not in {"both", "units", "messages"}:
            return {"success": False, "error": "source 必须是 both、units 或 messages"}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            return {"success": False, "error": "limit 必须是 1 到 20 的整数"}
        text = str(query or "").strip()
        if len(text) > 200:
            return {"success": False, "error": "query 太长，请缩短到 200 字以内"}
        if "\n" in text or "\r" in text:
            return {"success": False, "error": "query 不能包含换行"}
        start, end = _bounds(since, until)
        result: dict[str, Any] = {
            "success": True,
            "group_id": group_id,
            "query": text,
            "source": source,
            "units": [],
            "messages": [],
            "counts": {"units": 0, "messages": 0},
        }
        if source in {"both", "units"}:
            result["units"] = self._search_units(
                group_id=group_id,
                query=text,
                limit=limit,
                include_global=include_global,
                start=start,
                end=end,
                user_id=user_id,
            )
            result["counts"]["units"] = len(result["units"])
        if source in {"both", "messages"}:
            result["messages"] = self._search_messages(
                group_id=group_id,
                query=text,
                limit=limit,
                start=start,
                end=end,
                user_id=user_id,
            )
            result["counts"]["messages"] = len(result["messages"])
        return result

    def _search_units(
        self,
        *,
        group_id: str,
        query: str,
        limit: int,
        include_global: bool,
        start: datetime | None,
        end: datetime | None,
        user_id: str,
    ) -> list[dict[str, Any]]:
        store = MemoryUnitFileStore(self.work_path)
        candidates: list[MemoryUnit] = list(
            store.load(group_id, with_embeddings=False)
        )
        if include_global:
            for other_group in store.list_group_ids():
                if other_group == group_id:
                    continue
                for unit in store.load(other_group, with_embeddings=False):
                    if unit.scope in {"persona", "global"}:
                        candidates.append(unit)
        seen: set[str] = set()
        normalized_query = unicodedata.normalize("NFKC", query).casefold().strip()
        terms = [term for term in re.split(r"\s+", normalized_query) if term]
        wanted_user = str(user_id or "").strip().casefold()
        ranked: list[tuple[float, MemoryUnit]] = []
        for unit in candidates:
            if unit.unit_id in seen:
                continue
            seen.add(unit.unit_id)
            stamp = unit.event_time or unit.created_at
            if not _within(stamp, start, end):
                continue
            if wanted_user and not self._unit_matches_user(unit, wanted_user):
                continue
            if normalized_query:
                primary = unicodedata.normalize(
                    "NFKC",
                    " ".join(
                        [
                            unit.summary,
                            *unit.topics,
                            *unit.keywords,
                            unit.status,
                            unit.event_time,
                        ]
                    ),
                ).casefold()
                support = unicodedata.normalize(
                    "NFKC",
                    " ".join([*unit.participants, *unit.identity_aliases, *unit.retrieval_terms]),
                ).casefold()
                phrase = normalized_query in support
                matched = sum(
                    1 for term in terms if term in primary or term in support
                )
                if not phrase and matched == 0:
                    continue
                score = 0.0
                if normalized_query in primary:
                    score += 100.0
                elif phrase:
                    score += 30.0
                score += sum(10.0 for term in terms if term in primary)
                score += sum(2.0 for term in terms if term in support)
                if terms and matched == len(terms):
                    score += 5.0
            else:
                score = 0.0
            score += max(0.0, min(1.0, unit.salience * unit.confidence)) * 5.0
            ranked.append((score, unit))
        ranked.sort(
            key=lambda item: (
                item[0],
                _parse_time(item[1].event_time or item[1].created_at)
                or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        return [_unit_hit(unit, score) for score, unit in ranked[:limit]]

    @staticmethod
    def _unit_matches_user(unit: MemoryUnit, wanted: str) -> bool:
        values = [unit.scope_id, *unit.participants]
        return any(
            unicodedata.normalize("NFKC", str(value or "")).casefold() == wanted
            for value in values
        )

    def _search_messages(
        self,
        *,
        group_id: str,
        query: str,
        limit: int,
        start: datetime | None,
        end: datetime | None,
        user_id: str,
    ) -> list[dict[str, Any]]:
        path = self.work_path / "archive" / f"{_safe_group_id(group_id)}.jsonl"
        if not path.is_file():
            return []
        scan_limit = min(_MAX_SCAN_LINES, max(limit * 8, 40))
        try:
            lines = self._matching_lines(path, query, scan_limit)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("读取聊天存档失败: %s", exc)
            return []
        wanted_user = str(user_id or "").strip()
        normalized_query = unicodedata.normalize("NFKC", query).casefold().strip()
        terms = [term for term in re.split(r"\s+", normalized_query) if term]
        entries: list[BasicMemoryEntry] = []
        for raw in lines:
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            try:
                entry = BasicMemoryEntry.from_dict(payload)
            except TypeError:
                continue
            if entry.group_id and entry.group_id != group_id:
                continue
            if wanted_user and entry.user_id != wanted_user:
                continue
            if normalized_query:
                haystack = unicodedata.normalize(
                    "NFKC", f"{entry.content} {entry.speaker_name}"
                ).casefold()
                if normalized_query not in haystack and not any(
                    term in haystack for term in terms
                ):
                    # grep only sees the raw JSON; hidden prompt/conversation
                    # fields must not make an unrelated message look relevant.
                    continue
            if not _within(entry.timestamp, start, end):
                continue
            entries.append(entry)
        entries.sort(key=lambda entry: _parse_time(entry.timestamp) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return [_entry_hit(entry) for entry in entries[:limit]]

    @staticmethod
    def _matching_lines(path: Path, query: str, scan_limit: int) -> list[str]:
        if not query:
            return MemorySearchService._tail_lines(path, scan_limit)
        if "\n" in query or "\r" in query:
            return []
        tac: subprocess.Popen[bytes] | None = None
        try:
            tac = subprocess.Popen(
                ["tac", "--", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            proc = subprocess.run(
                ["grep", "-F", "-i", "-m", str(scan_limit), "--", query],
                stdin=tac.stdout,
                capture_output=True,
                timeout=15,
            )
            if proc.returncode not in (0, 1):
                raise subprocess.SubprocessError(proc.stderr.decode("utf-8", "replace"))
            return proc.stdout.decode("utf-8", "replace").splitlines()
        except (FileNotFoundError, subprocess.SubprocessError):
            return MemorySearchService._scan_archive(path, query, scan_limit)
        finally:
            if tac is not None:
                if tac.stdout is not None:
                    tac.stdout.close()
                try:
                    tac.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tac.kill()

    @staticmethod
    def _tail_lines(path: Path, count: int) -> list[str]:
        try:
            proc = subprocess.run(
                ["tail", "-n", str(count), "--", str(path)],
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0:
                return proc.stdout.decode("utf-8", "replace").splitlines()
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        lines: deque[str] = deque(maxlen=count)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines.extend(handle)
        return list(lines)

    @staticmethod
    def _scan_archive(path: Path, query: str, scan_limit: int) -> list[str]:
        needle = query.casefold()
        matches: deque[str] = deque(maxlen=scan_limit)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if needle in line.casefold():
                    matches.append(line)
        return list(matches)


__all__ = ["MemorySearchService"]
