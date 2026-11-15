"""唯一的 cron 匹配器：五字段表达式的校验、匹配与「下一次是什么时候」。

bash 的 crontab 和插件的定时任务共用这一份规则，一律按本地（中国）时间解读，
与宿主机时区无关：

- 字段：分 时 日 月 周；支持 ``*``、``a-b``、``a,b``、``*/n``、``a-b/n`` 和英文月份/星期缩写；
- 星期字段 0 和 7 都表示周日；
- 日和周都受限时按标准 cron 的「或」语义：任一匹配即可（``*/2`` 也算受限）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = [
    "CRON_FIELD_COUNT",
    "CRON_TIMEZONE",
    "CronParseError",
    "cron_matches",
    "next_fire",
    "validate_expression",
]

CRON_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
CRON_FIELD_COUNT = 5
_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_WEEKDAY_NAMES = {
    name: index for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}
_FIELD_NAMES: dict[int, dict[str, int]] = {3: _MONTH_NAMES, 4: _WEEKDAY_NAMES}
#: 找下一次触发时最多往后看几天；「2 月 31 日」这种永远不会到的表达式到此为止。
_SEARCH_DAYS = 366 * 5


class CronParseError(ValueError):
    """表达式不合法或不受支持。"""


class _Schedule:
    """解析好的五个字段；日 / 周是否「受限」决定两者是与还是或。"""

    __slots__ = ("minutes", "hours", "days", "months", "weekdays", "day_wild", "weekday_wild")

    def __init__(self, expression: str) -> None:
        parts = str(expression or "").split()
        if len(parts) != CRON_FIELD_COUNT:
            raise CronParseError("目前只支持五字段 cron 表达式：分 时 日 月 周")
        fields = [
            _parse_field(part, low, high, _FIELD_NAMES.get(index))
            for index, (part, (low, high)) in enumerate(zip(parts, _LIMITS))
        ]
        self.minutes, self.hours, self.days, self.months = fields[:4]
        self.weekdays = {0 if value == 7 else value for value in fields[4]}
        # 标准规则看字段是不是字面上的 '*'；'*/2' 这样的步长仍算受限。
        self.day_wild = parts[2] == "*"
        self.weekday_wild = parts[4] == "*"

    def matches_day(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        day_ok = moment.day in self.days
        weekday_ok = (moment.weekday() + 1) % 7 in self.weekdays
        if self.day_wild and self.weekday_wild:
            return True
        if self.day_wild:
            return weekday_ok
        if self.weekday_wild:
            return day_ok
        return day_ok or weekday_ok

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minutes and moment.hour in self.hours and self.matches_day(moment)
        )


def validate_expression(expression: str) -> None:
    """不合法时抛 :class:`CronParseError`。"""
    _Schedule(expression)


def cron_matches(expression: str, now: datetime | None = None) -> bool:
    """``now`` 所在的这一分钟是否命中表达式；表达式不合法视为不命中。"""
    moment = (now or datetime.now(CRON_TIMEZONE)).astimezone(CRON_TIMEZONE)
    try:
        return _Schedule(expression).matches(moment)
    except CronParseError:
        return False


def next_fire(expression: str, after: datetime) -> datetime | None:
    """``after`` 所在分钟之后、第一个命中表达式的整分钟（本地时区）；永远不会到返回 None。"""
    schedule = _Schedule(expression)
    start = after.astimezone(CRON_TIMEZONE).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.replace(hour=0, minute=0)
    hours = sorted(schedule.hours)
    minutes = sorted(schedule.minutes)
    for _ in range(_SEARCH_DAYS):
        if schedule.matches_day(day):
            for hour in hours:
                for minute in minutes:
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    return None


def _parse_field(
    field: str,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None = None,
) -> set[int]:
    result: set[int] = set()
    for raw_part in field.upper().split(","):
        part = raw_part.strip()
        if not part:
            raise CronParseError(f"cron 字段无效: {field}")
        base, separator, raw_step = part.partition("/")
        if separator:
            if base != "*" and "-" not in base:
                raise CronParseError(f"cron 步长必须作用于范围或 *: {field}")
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise CronParseError(f"cron 步长无效: {field}") from exc
            if step <= 0:
                raise CronParseError(f"cron 步长必须大于 0: {field}")
        else:
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _field_value(raw_start, names)
            end = _field_value(raw_end, names)
        else:
            start = _field_value(base, names)
            end = maximum if separator else start

        if start < minimum or end > maximum or start > end:
            raise CronParseError(f"cron 字段超出范围: {field}")
        result.update(range(start, end + 1, step))
    return result


def _field_value(value: str, names: dict[str, int] | None) -> int:
    if names and value in names:
        return names[value]
    try:
        return int(value)
    except ValueError as exc:
        raise CronParseError(f"cron 字段值无效: {value}") from exc
