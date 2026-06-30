"""24-hour reply coefficient curve helpers."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any


def _clamp(value: float, minimum: float = 0.0, maximum: float = 2.0) -> float:
    return max(minimum, min(maximum, value))


def _parse_time_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return 1440
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def _format_time(minutes: int) -> str:
    if minutes == 1440:
        return "24:00"
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def normalize_reply_time_curve_points(points: Any) -> list[dict[str, float | str]]:
    if not isinstance(points, list):
        return []

    normalized: dict[int, float] = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        minutes = _parse_time_minutes(point.get("time"))
        if minutes is None:
            continue
        try:
            coefficient = float(point.get("coefficient", 1.0))
        except (TypeError, ValueError):
            coefficient = 1.0
        normalized[minutes] = round(_clamp(coefficient), 4)

    return [
        {"time": _format_time(minutes), "coefficient": coefficient}
        for minutes, coefficient in sorted(normalized.items())
    ]


def get_reply_time_coefficient(points: Any, now: time | None = None) -> float:
    normalized = normalize_reply_time_curve_points(points)
    if not normalized:
        return 1.0
    if len(normalized) == 1:
        return float(normalized[0]["coefficient"])

    current = now or datetime.now().time()
    current_minutes = current.hour * 60 + current.minute + current.second / 60
    parsed = [
        (_parse_time_minutes(point["time"]), float(point["coefficient"]))
        for point in normalized
    ]
    anchors = [(minutes, coefficient) for minutes, coefficient in parsed if minutes is not None]
    if not anchors:
        return 1.0

    for (left_minute, left_value), (right_minute, right_value) in zip(
        anchors, anchors[1:], strict=False
    ):
        if left_minute <= current_minutes <= right_minute:
            return _interpolate(left_minute, left_value, right_minute, right_value, current_minutes)

    left_minute, left_value = anchors[-1]
    right_minute, right_value = anchors[0]
    if current_minutes < right_minute:
        current_minutes += 1440
    return _interpolate(left_minute, left_value, right_minute + 1440, right_value, current_minutes)


def _interpolate(
    left_minute: float,
    left_value: float,
    right_minute: float,
    right_value: float,
    current_minute: float,
) -> float:
    if right_minute <= left_minute:
        return _clamp(left_value)
    ratio = (current_minute - left_minute) / (right_minute - left_minute)
    return round(_clamp(left_value + (right_value - left_value) * ratio), 4)


__all__ = ["get_reply_time_coefficient", "normalize_reply_time_curve_points"]
