"""Time helpers shared by the bot, the database layer and the API.

The bot stores UTC timestamps and also reports the local trading day. The local
day matters because the circuit breaker resets at local midnight. `zoneinfo`
handles this when IANA data is available (Linux, macOS, Windows with `tzdata`);
otherwise a fixed CET/CEST approximation is used.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

BOT_TIMEZONE_NAME = os.environ.get("BOT_TIMEZONE", "Europe/Zagreb")
try:
    from zoneinfo import ZoneInfo

    _BOT_TZ: Optional[ZoneInfo] = ZoneInfo(BOT_TIMEZONE_NAME)
except Exception:  # pragma: no cover - depends on the host's tz database
    _BOT_TZ = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _last_sunday_utc(year: int, month: int) -> datetime:
    d = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d.replace(hour=1, minute=0, second=0, microsecond=0)


def _eu_cet_offset_hours(dt_utc: datetime) -> int:
    start = _last_sunday_utc(dt_utc.year, 3)
    end = _last_sunday_utc(dt_utc.year, 10)
    return 2 if start <= dt_utc < end else 1


def to_local(dt_utc: datetime) -> datetime:
    aware = dt_utc.replace(tzinfo=timezone.utc)
    if _BOT_TZ is not None:
        return aware.astimezone(_BOT_TZ)
    off = _eu_cet_offset_hours(dt_utc)
    return aware.astimezone(timezone(timedelta(hours=off), "CEST" if off == 2 else "CET"))


def _local_midnight_to_utc(local_date) -> datetime:
    naive_midnight = datetime(local_date.year, local_date.month, local_date.day)
    if _BOT_TZ is not None:
        return naive_midnight.replace(tzinfo=_BOT_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    approx_utc = naive_midnight - timedelta(hours=1)
    return naive_midnight - timedelta(hours=_eu_cet_offset_hours(approx_utc))


def local_day_bounds(now_utc: datetime) -> Tuple[str, datetime, datetime]:
    """Return (local ISO day, UTC start, UTC end) for the local trading day."""
    local_now = to_local(now_utc)
    day = local_now.date()
    start_utc = _local_midnight_to_utc(day)
    next_utc = _local_midnight_to_utc(day + timedelta(days=1))
    return day.isoformat(), start_utc, next_utc


def fmt_local(dt_utc: Optional[datetime]) -> Optional[str]:
    if not dt_utc:
        return None
    loc = to_local(dt_utc)
    return loc.strftime("%Y-%m-%d %H:%M:%S ") + (loc.tzname() or "")
