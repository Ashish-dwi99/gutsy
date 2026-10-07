"""Schedule rules: when a scheduled task runs next.

A rule is plain JSON so the store, the MCP tool, and the daemon share one
shape:

  {"kind": "once", "at": <epoch seconds>}
  {"kind": "interval", "every_minutes": 90}
  {"kind": "daily", "time": "08:30", "weekdays": [0, 2, 4], "timezone": "Asia/Kolkata"}

`weekdays` uses Monday=0 and is empty for every day.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_INTERVAL_MINUTES = 5


class RuleError(ValueError):
    pass


def build_rule(
    *,
    kind: str,
    timezone: str,
    at: str | None = None,
    every_minutes: int | None = None,
    time: str | None = None,
    weekdays: list[int] | None = None,
) -> dict[str, Any]:
    """Validate a tool's schedule arguments into a rule."""
    zone = _zone(timezone)
    if kind == "once":
        if not at:
            raise RuleError("a once schedule needs `at`, a local date and time like 2026-10-08T09:00")
        moment = datetime.fromisoformat(at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone)
        return {"kind": "once", "at": moment.timestamp()}
    if kind == "interval":
        if every_minutes is None or every_minutes < MIN_INTERVAL_MINUTES:
            raise RuleError(f"an interval schedule needs every_minutes of at least {MIN_INTERVAL_MINUTES}")
        return {"kind": "interval", "every_minutes": int(every_minutes)}
    if kind == "daily":
        _clock(time)
        days = sorted(set(weekdays or []))
        if any(day not in range(7) for day in days):
            raise RuleError("weekdays are 0 (Monday) to 6 (Sunday)")
        return {"kind": "daily", "time": time, "weekdays": days, "timezone": timezone}
    raise RuleError("kind must be once, interval, or daily")


def next_run(rule: dict[str, Any], after: float) -> float | None:
    """The first run strictly after `after`, or None when the rule is spent."""
    kind = rule["kind"]
    if kind == "once":
        return rule["at"] if rule["at"] > after else None
    if kind == "interval":
        return after + rule["every_minutes"] * 60
    if kind == "daily":
        zone = _zone(rule["timezone"])
        hour, minute = _clock(rule["time"])
        days = set(rule["weekdays"])
        start = datetime.fromtimestamp(after, zone)
        for offset in range(8):
            day = (start + timedelta(days=offset)).date()
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if candidate.timestamp() > after and (not days or candidate.weekday() in days):
                return candidate.timestamp()
        raise RuleError("daily rule has no reachable day")
    raise RuleError(f"unknown rule kind {kind!r}")


def first_run(rule: dict[str, Any], now: float) -> float:
    if rule["kind"] == "once":
        if rule["at"] <= now:
            raise RuleError("that time has already passed")
        return rule["at"]
    upcoming = next_run(rule, now)
    assert upcoming is not None
    return upcoming


def describe(rule: dict[str, Any]) -> str:
    kind = rule["kind"]
    if kind == "once":
        return "once at " + datetime.fromtimestamp(rule["at"]).astimezone().isoformat(timespec="minutes")
    if kind == "interval":
        return f"every {rule['every_minutes']} minutes"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = ",".join(names[day] for day in rule["weekdays"]) or "every day"
    return f"{days} at {rule['time']} ({rule['timezone']})"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuleError(f"unknown timezone {name!r}") from exc


def _clock(value: str | None) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in str(value).split(":"))
    except ValueError as exc:
        raise RuleError("time must be HH:MM in 24-hour form") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise RuleError("time must be HH:MM in 24-hour form")
    return hour, minute
