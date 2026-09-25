"""Facility-local time. Everything in the DB is naive local time as 'YYYY-MM-DD HH:MM'.

`now()` is the single seam tests use to move time.
"""
from datetime import datetime, timedelta, timezone

from . import config

_TZ = timezone(timedelta(minutes=config.TZ_OFFSET_MIN))
_override = None

FMT = "%Y-%m-%d %H:%M"


def now():
    if _override is not None:
        return _override
    return datetime.now(_TZ).replace(tzinfo=None, second=0, microsecond=0)


def set_now(dt):
    """Test hook: freeze the clock (None to release)."""
    global _override
    _override = dt


def fmt(dt):
    return dt.strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT)


def today():
    return now().date()
