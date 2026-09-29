"""Collection slots: every 5 minutes 08:00-24:00, every 30 minutes 00:00-08:00 by default."""
import bisect

from . import timeutil


class Schedule:
    def __init__(self, windows, tz):
        self.windows, self.tz = windows, tz
        self._offsets = [t for start, end, step in windows for t in range(start, end, step)]

    def slots(self, business_day):
        base = timeutil.day_start(business_day, self.tz)
        return [base + offset for offset in self._offsets]

    def slot_at(self, ts):
        """Start of the slot containing ts."""
        day = timeutil.business_date(ts, self.tz)
        base = timeutil.day_start(day, self.tz)
        return base + self._offsets[bisect.bisect_right(self._offsets, ts - base) - 1]

    def next_slot(self, ts):
        day = timeutil.business_date(ts, self.tz)
        base = timeutil.day_start(day, self.tz)
        index = bisect.bisect_right(self._offsets, ts - base)
        if index < len(self._offsets):
            return base + self._offsets[index]
        return timeutil.day_start(timeutil.shift_day(day, 1), self.tz)

    def interval_at(self, ts):
        offset = ts - timeutil.day_start(timeutil.business_date(ts, self.tz), self.tz)
        for start, end, step in self.windows:
            if start <= offset < end:
                return step
        raise ValueError('time outside schedule')

    def expected_slots(self, business_day, until_ts=None):
        return [s for s in self.slots(business_day) if until_ts is None or s <= until_ts]
