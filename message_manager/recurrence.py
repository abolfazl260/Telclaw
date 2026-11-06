"""Deterministic wall-clock recurrence, with fast-forward over missed intervals."""
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from .validation import ValidationError

TEHRAN = ZoneInfo("Asia/Tehran")


def normalize(data):
    try:
        kind = str(data.get("recurrence", "once"))
        if kind not in {"once", "daily", "weekly", "days", "weeks"}:
            raise ValueError("Invalid recurrence")
        start = date.fromisoformat(str(data["date"])[:10])
        clock = time.fromisoformat(str(data["time"]))
        if clock.tzinfo or clock.second or clock.microsecond:
            raise ValueError("Use a local HH:MM time")
        interval = int(1 if data.get("interval") in (None, "") else data["interval"])
        if not 1 <= interval <= 365:
            raise ValueError("Interval must be 1–365")
        if kind in {"once", "daily", "weekly"}:
            interval = 1
        weekdays = data.get("weekdays") or str(start.weekday())
        if isinstance(weekdays, str):
            weekdays = weekdays.split(",")
        weekdays = sorted(set(int(d) for d in weekdays))
        if not weekdays or any(d < 0 or d > 6 for d in weekdays):
            raise ValueError("Weekdays must be 0 (Monday) through 6 (Sunday)")
        until = str(data.get("end_date") or "")
        if until and date.fromisoformat(until) < start:
            raise ValueError("End date precedes start date")
        count = int(data.get("end_count") or 0)
        if not 0 <= count <= 100000:
            raise ValueError("End count must be 1–100000 or blank")
        return dict(recurrence=kind, date=start.isoformat(), time=clock.strftime("%H:%M"),
                    interval=interval, weekdays=weekdays, end_date=until, end_count=count,
                    timezone="Asia/Tehran")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValidationError(str(exc)) from exc


def _at(spec, index):
    """Return zero-based occurrence as UTC, or None after count/end date."""
    if index < 0 or spec["end_count"] and index >= spec["end_count"]:
        return None
    start = date.fromisoformat(spec["date"])
    kind = spec["recurrence"]
    if kind == "once":
        if index:
            return None
        day = start
    elif kind in {"daily", "days"}:
        day = start + timedelta(days=index * spec["interval"])
    else:
        days = spec["weekdays"]
        first = [d for d in days if d >= start.weekday()]
        monday = start - timedelta(days=start.weekday())
        if index < len(first):
            day = monday + timedelta(days=first[index])
        else:
            cycle, pos = divmod(index - len(first), len(days))
            day = monday + timedelta(weeks=(cycle + 1) * spec["interval"], days=days[pos])
    if spec["end_date"] and day > date.fromisoformat(spec["end_date"]):
        return None
    return datetime.combine(day, time.fromisoformat(spec["time"]), TEHRAN).astimezone(timezone.utc)


def at(spec, index):
    try:
        return _at(spec, index)
    except OverflowError:
        return None


def next_after(spec, after):
    """Return (instant, ordinal) strictly after a UTC instant, without backlog loops."""
    start = date.fromisoformat(spec["date"])
    local = after.astimezone(TEHRAN)
    if spec["recurrence"] == "once":
        first = at(spec, 0)
        return (first, 0) if first and first > after else (None, 0)
    if spec["recurrence"] in {"daily", "days"}:
        estimate = max(0, (local.date() - start).days // spec["interval"])
    else:
        first = len([d for d in spec["weekdays"] if d >= start.weekday()])
        monday = start - timedelta(days=start.weekday())
        cycle = max(0, (local.date() - monday).days // (7 * spec["interval"]))
        estimate = 0 if cycle == 0 else first + (cycle - 1) * len(spec["weekdays"])
    for index in range(estimate, estimate + 16):
        instant = at(spec, index)
        if instant is None or instant > after:
            return instant, index
    raise ValidationError("Cannot compute next occurrence")
