"""Excel-compatible date serial numbers, including the 1900 leap-year bug.

Serial 1 is 1 Jan 1900. Excel wrongly treats 1900 as a leap year, so serial 60 is the
nonexistent 29 Feb 1900 and every date from 1 Mar 1900 is one higher than the real
day count. The fractional part of a serial is the time of day.
"""

from __future__ import annotations

import math
import re
from datetime import date

MAX_SERIAL = 2_958_465  # 31 Dec 9999
FAKE_LEAP_SERIAL = 60  # the nonexistent 29 Feb 1900

_EPOCH = date(1899, 12, 31).toordinal()  # real day count offset for serials before 1 Mar 1900
_SECONDS_PER_DAY = 86_400

_DMY = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_YMD = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def make_serial(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> float:
    """Return the serial number for a date and optional time. Raises ValueError if invalid."""
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError("Invalid time of day")
    if (year, month, day) == (1900, 2, 29):
        days = FAKE_LEAP_SERIAL
    else:
        days = date(year, month, day).toordinal() - _EPOCH  # ValueError if not a real date
        if days < 1:
            raise ValueError("Dates before 1 Jan 1900 are not supported")
        if days >= FAKE_LEAP_SERIAL:
            days += 1
    return days + (hour * 3600 + minute * 60 + second) / _SECONDS_PER_DAY


def split_serial(serial: float) -> tuple[int, int, int, int, int, int]:
    """Return (year, month, day, hour, minute, second), rounded to the nearest second."""
    if not math.isfinite(serial) or not 1 <= serial < MAX_SERIAL + 1:
        raise ValueError(f"Serial out of range: {serial}")
    days = math.floor(serial)
    seconds = round((serial - days) * _SECONDS_PER_DAY)
    if seconds == _SECONDS_PER_DAY:
        days, seconds = days + 1, 0
    if days > MAX_SERIAL:
        raise ValueError(f"Serial out of range: {serial}")
    if days == FAKE_LEAP_SERIAL:
        y, m, d = 1900, 2, 29
    else:
        real = days if days < FAKE_LEAP_SERIAL else days - 1
        dt = date.fromordinal(real + _EPOCH)
        y, m, d = dt.year, dt.month, dt.day
    return y, m, d, seconds // 3600, seconds % 3600 // 60, seconds % 60


def parse_date_text(text: str) -> tuple[float, str] | None:
    """Recognise ``D/M/YYYY`` and ``YYYY-M-D`` text. Returns (serial, format) or None."""
    text = text.strip()
    if m := _DMY.fullmatch(text):
        d, mo, y = (int(g) for g in m.groups())
        fmt = "DD/MM/YYYY"
    elif m := _YMD.fullmatch(text):
        y, mo, d = (int(g) for g in m.groups())
        fmt = "YYYY-MM-DD"
    else:
        return None
    try:
        return make_serial(y, mo, d), fmt
    except ValueError:
        return None
