"""Time helpers. Stored times are integer epoch seconds; business days use a fixed offset.

China Standard Time has no DST, so a fixed UTC+8 offset is exact and avoids needing
tz database files on Windows.
"""
import math
from datetime import date, datetime, time, timedelta, timezone


def business_tz(offset_hours=8):
    return timezone(timedelta(hours=offset_hours))


def to_ts(value):
    """Epoch seconds from an aware datetime, ISO text with offset, or a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            return None
        return int(value.timestamp())
    return None


def to_dt(ts, tz):
    return datetime.fromtimestamp(ts, tz)


def iso(ts, tz):
    return None if ts is None else to_dt(ts, tz).isoformat(timespec='seconds')


def business_date(ts, tz):
    return to_dt(ts, tz).date().isoformat()


def day_start(business_day, tz):
    return int(datetime.combine(date.fromisoformat(business_day), time(0), tz).timestamp())


def shift_day(business_day, days):
    return (date.fromisoformat(business_day) + timedelta(days=days)).isoformat()


def weekday(business_day):
    return date.fromisoformat(business_day).weekday()


def hhmm(ts, tz):
    return to_dt(ts, tz).strftime('%H:%M')


def parse_hhmm(text):
    """Seconds since midnight for 'HH:MM'; '24:00' is the end of the day."""
    hours, minutes = (int(part) for part in text.split(':'))
    if not (0 <= minutes < 60 and (0 <= hours < 24 or (hours == 24 and minutes == 0))):
        raise ValueError('invalid time of day: ' + text)
    return hours * 3600 + minutes * 60
