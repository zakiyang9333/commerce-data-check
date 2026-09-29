"""REALTIME_SIGNAL_ENGINE_V1: deterministic rules over latest-good realtime facts.

Each rule compares today's value with the median of the same time of day over recent
days. A signal needs a minimum sample of history days, must hold for a minimum
duration before any state change, resolves at a less extreme threshold than it opens
at (hysteresis), cannot reopen during a cooldown and is deduplicated per
rule/entity/day. Phase 0 signals are SHADOW only: stored for review, never alerted.
"""
import json
import statistics
from decimal import Decimal

from . import contract, timeutil

ENGINE = 'REALTIME_SIGNAL_ENGINE_V1'
ACTIVE = ('OPEN', 'ESCALATED')


def _fresh_state(rule, entity, day):
    return {'rule': rule, 'entity': entity, 'business_date': day, 'state': 'IDLE', 'seq': 0, 'signal_id': None,
            'last_event_ts': None, 'open_since_ts': None, 'escalate_since_ts': None, 'clear_since_ts': None,
            'cooldown_until_ts': None}


class SignalEngine:
    def __init__(self, store, rules, tz, *, max_lag_minutes=20, min_completeness=0.5):
        self.store, self.rules, self.tz = store, rules, tz
        self.max_lag, self.min_completeness = max_lag_minutes * 60, min_completeness

    @classmethod
    def from_config(cls, cfg, store):
        conf = cfg['signals']
        return cls(store, cfg.rules, cfg.tz, max_lag_minutes=conf['max_lag_minutes'],
                   min_completeness=conf['min_completeness'])

    def evaluate(self, now_ts):
        """Evaluate every rule on new latest-good data; return OPEN/ESCALATE/RESOLVE events."""
        day = timeutil.business_date(now_ts, self.tz)
        events, outcomes = [], {}
        with self.store.write() as conn:
            conn.execute("UPDATE signal_state SET state = 'EXPIRED' WHERE business_date < ? AND state IN ('OPEN', 'ESCALATED')",
                         (day,))
            for rule in self.rules:
                for entity in self._entities(conn, rule, day):
                    outcome, event = self._evaluate(conn, rule, entity, day, now_ts)
                    if outcome != 'NO_NEW_DATA':
                        outcomes[(rule['rule'], outcome)] = outcomes.get((rule['rule'], outcome), 0) + 1
                    if event is not None:
                        events.append(event)
            for (rule, outcome), n in outcomes.items():
                conn.execute('INSERT INTO signal_eval_count VALUES (?, ?, ?, ?) ON CONFLICT (business_date, rule, outcome) '
                             'DO UPDATE SET n = n + excluded.n', (day, rule, outcome, n))
        return events

    # -- measurement ------------------------------------------------------

    def _entities(self, conn, rule, day):
        series = rule['series'] if rule['kind'] == 'value' else rule['numerator']
        if series['entity']:
            return [series['entity']]
        return self.store.entities(conn, series['source'], series['metric'], series['entity_type'], day)

    def _parts(self, rule, entity):
        if rule['kind'] == 'ratio':
            return [(rule['numerator'], entity if not rule['numerator']['entity'] else rule['numerator']['entity']),
                    (rule['denominator'], entity if not rule['denominator']['entity'] else rule['denominator']['entity'])]
        return [(rule['series'], entity)]

    def _today(self, conn, rule, entity, day):
        """Observed value, its event time and completeness from today's latest good data."""
        latest = []
        for series, ent in self._parts(rule, entity):
            row = self.store.latest_good(series['source'], series['metric'], ent, day, conn=conn)
            if row is None:
                return None
            latest.append(row)
        event_ts = min(r['event_ts'] for r in latest)
        completeness = min(r['completeness'] for r in latest)
        values = [Decimal(r['value']) for r in latest]
        if rule['basis'] == 'window':
            start_ts = event_ts - rule['window_minutes'] * 60
            if start_ts <= timeutil.day_start(day, self.tz):
                start = Decimal(0)
            else:
                series, ent = self._parts(rule, entity)[0]
                found = self.store.good_at_or_before(conn, series['source'], series['metric'], ent, day, start_ts,
                                                     start_ts - rule['tolerance_minutes'] * 60)
                if found is None:
                    return {'event_ts': event_ts, 'completeness': completeness, 'observed': None,
                            'reason': 'NO_WINDOW_START'}
                start = found[0]
            values = [values[0] - start]
        return {'event_ts': event_ts, 'completeness': completeness, 'observed': self._combine(rule, values)}

    def _combine(self, rule, values):
        """Rule value from its parts, or None when the volume is too low to judge."""
        if rule['kind'] == 'ratio':
            numerator, denominator = values
            if denominator < Decimal(str(rule['min_denominator'])):
                return None
            return float(numerator / denominator)
        return float(values[0])

    def _baseline(self, conn, rule, entity, day, tod):
        first, last = timeutil.shift_day(day, -rule['lookback_days']), timeutil.shift_day(day, -1)
        tolerance = rule['tolerance_minutes'] * 60
        columns = [self.store.good_by_time_of_day(conn, s['source'], s['metric'], ent, first, last, tod - tolerance, tod)
                   for s, ent in self._parts(rule, entity)]
        start_column = None
        if rule['basis'] == 'window':
            start_tod = tod - rule['window_minutes'] * 60
            if start_tod > 0:
                s, ent = self._parts(rule, entity)[0]
                start_column = self.store.good_by_time_of_day(conn, s['source'], s['metric'], ent, first, last,
                                                              start_tod - tolerance, start_tod)
        samples = []
        for offset in range(rule['lookback_days'], 0, -1):
            past = timeutil.shift_day(day, -offset)
            if rule.get('same_weekday') and timeutil.weekday(past) != timeutil.weekday(day):
                continue
            if any(past not in column for column in columns):
                continue
            values = [column[past][0] for column in columns]
            if start_column is not None:
                if past not in start_column:
                    continue
                values = [values[0] - start_column[past][0]]
            value = self._combine(rule, values)
            if value is not None:
                samples.append(value)
        return samples

    # -- evaluation -------------------------------------------------------

    def _evaluate(self, conn, rule, entity, day, now_ts):
        row = conn.execute('SELECT * FROM signal_state WHERE rule = ? AND entity = ? AND business_date = ?',
                           (rule['rule'], entity, day)).fetchone()
        state = dict(row) if row else _fresh_state(rule['rule'], entity, day)
        today = self._today(conn, rule, entity, day)
        if today is None:
            return 'NO_DATA', None
        event_ts = today['event_ts']
        if state['last_event_ts'] is not None and event_ts <= state['last_event_ts']:
            return 'NO_NEW_DATA', None
        state['last_event_ts'] = event_ts
        outcome, event = self._judge(conn, rule, entity, day, now_ts, today, state)
        self._save_state(conn, state)
        return outcome, event

    def _judge(self, conn, rule, entity, day, now_ts, today, state):
        event_ts = today['event_ts']
        if now_ts - event_ts > self.max_lag:
            return 'SUPPRESSED_STALE', None
        if today['completeness'] < self.min_completeness:
            return 'SUPPRESSED_INCOMPLETE', None
        if today['observed'] is None:
            return today.get('reason', 'LOW_VOLUME'), None
        samples = self._baseline(conn, rule, entity, day, event_ts - timeutil.day_start(day, self.tz))
        if len(samples) < rule['min_sample']:
            return 'INSUFFICIENT_HISTORY', None
        baseline = statistics.median(samples)
        if baseline < rule['min_baseline']:
            return 'LOW_BASELINE', None
        observed = today['observed']
        delta = observed / baseline - 1
        if rule['direction'] == 'drop':
            breach, severe, clear = delta <= rule['open_delta'], delta <= rule['escalate_delta'], delta >= rule['resolve_delta']
        else:
            breach, severe, clear = delta >= rule['open_delta'], delta >= rule['escalate_delta'], delta <= rule['resolve_delta']
        transition = self._step(rule, state, event_ts, breach, severe, clear)
        if transition is None:
            return 'EVALUATED', None
        event = {
            'engine': ENGINE, 'signal_id': state['signal_id'], 'rule': rule['rule'], 'entity': entity,
            'business_date': day, 'transition': transition, 'state': state['state'], 'mode': contract.SIGNAL_MODE,
            'production_alert': False, 'observed': observed, 'baseline': baseline, 'delta': round(delta, 4),
            'event_time': timeutil.iso(event_ts, self.tz), 'event_ts': event_ts, 'evidence_n': len(samples),
            'completeness': today['completeness'], 'freshness_minutes': round((now_ts - event_ts) / 60, 1),
            'provisional': True,
            'basis': rule['basis'], 'window_minutes': rule.get('window_minutes') if rule['basis'] == 'window' else None,
            'thresholds': {k: rule[k] for k in ('open_delta', 'escalate_delta', 'resolve_delta', 'min_sample',
                                                'min_duration_minutes', 'cooldown_minutes', 'min_baseline')},
        }
        detail = {k: event[k] for k in ('engine', 'state', 'event_time', 'basis', 'window_minutes', 'thresholds')}
        conn.execute('INSERT OR IGNORE INTO signal_event VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)', (
            '%s|%s' % (state['signal_id'], transition), state['signal_id'], rule['rule'], entity, day, transition,
            contract.SIGNAL_MODE, observed, baseline, delta, event_ts, len(samples), today['completeness'],
            event['freshness_minutes'], json.dumps(detail, sort_keys=True), now_ts))
        return 'EVALUATED', event

    @staticmethod
    def _step(rule, state, event_ts, breach, severe, clear):
        """Advance the per-day state machine by one new data point; return the transition or None."""
        duration, transition = rule['min_duration_minutes'] * 60, None
        state['escalate_since_ts'] = (state['escalate_since_ts'] or event_ts) if severe else None
        if state['state'] in ACTIVE:
            state['clear_since_ts'] = (state['clear_since_ts'] or event_ts) if clear else None
            if clear and event_ts - state['clear_since_ts'] >= duration:
                state.update(state='RESOLVED', clear_since_ts=None, open_since_ts=None,
                             cooldown_until_ts=event_ts + rule['cooldown_minutes'] * 60)
                return 'RESOLVE'
            if state['state'] == 'OPEN' and severe and event_ts - state['escalate_since_ts'] >= duration:
                state['state'] = 'ESCALATED'
                return 'ESCALATE'
            return None
        cooling = state['cooldown_until_ts'] is not None and event_ts < state['cooldown_until_ts']
        state['open_since_ts'] = (state['open_since_ts'] or event_ts) if breach and not cooling else None
        if state['open_since_ts'] is not None and event_ts - state['open_since_ts'] >= duration:
            state['seq'] += 1
            state.update(state='OPEN', signal_id='%s|%s|%s|%d' % (rule['rule'], state['entity'], state['business_date'],
                                                                   state['seq']),
                         open_since_ts=None, clear_since_ts=None)
            transition = 'OPEN'
        return transition

    @staticmethod
    def _save_state(conn, state):
        columns = ('rule', 'entity', 'business_date', 'state', 'seq', 'signal_id', 'last_event_ts', 'open_since_ts',
                   'escalate_since_ts', 'clear_since_ts', 'cooldown_until_ts')
        conn.execute('INSERT OR REPLACE INTO signal_state (%s) VALUES (%s)' % (', '.join(columns), ', '.join('?' * len(columns))),
                     tuple(state[c] for c in columns))


def review_signal(store, signal_id, verdict, now_ts, reviewer='', note=''):
    """Record a human review of a shadow signal (feeds signal_false_positive_review)."""
    with store.write() as conn:
        if conn.execute('SELECT 1 FROM signal_event WHERE signal_id = ?', (signal_id,)).fetchone() is None:
            raise KeyError(signal_id)
        conn.execute('INSERT OR REPLACE INTO signal_review VALUES (?, ?, ?, ?, ?)',
                     (signal_id, verdict, reviewer, note, int(now_ts)))


def events_for_day(store, day):
    with store.read() as conn:
        rows = conn.execute('SELECT * FROM signal_event WHERE business_date = ? ORDER BY created_ts, event_key', (day,)).fetchall()
    return [dict(r, detail=json.loads(r['detail']), production_alert=False, provisional=True) for r in rows]
