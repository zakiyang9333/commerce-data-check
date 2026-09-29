"""Realtime fact store (realtime_fact.db): SQLite + WAL, separate from the settled store.

Every observation is a provisional realtime estimate. The store refuses settled
store paths and any existing database it did not create, so realtime data cannot
overwrite a T+1 settled fact. Reads keep latest_attempt, latest_success and
latest_good apart; failed or skipped attempts store nothing, so they can never
replace or erase a good value.
"""
from decimal import Decimal, InvalidOperation

from . import contract, timeutil
from .db import Database, StoreGuardError  # noqa: F401  (re-exported)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS attempt (
    attempt_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    slot_ts INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    started_ts INTEGER NOT NULL,
    finished_ts INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'PARTIAL', 'FAILED', 'SKIPPED')),
    error_code TEXT,
    expected_n INTEGER NOT NULL,
    received_n INTEGER NOT NULL DEFAULT 0,
    completeness REAL NOT NULL DEFAULT 0,
    browser_id TEXT,
    inserted_n INTEGER NOT NULL DEFAULT 0,
    duplicate_n INTEGER NOT NULL DEFAULT 0,
    rejected_n INTEGER NOT NULL DEFAULT 0,
    suspect_n INTEGER NOT NULL DEFAULT 0,
    late_n INTEGER NOT NULL DEFAULT 0,
    erased_good_n INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, slot_ts, attempt_no)
);
CREATE INDEX IF NOT EXISTS attempt_by_source ON attempt (source, finished_ts);
CREATE INDEX IF NOT EXISTS attempt_by_date ON attempt (business_date, source);

CREATE TABLE IF NOT EXISTS observation (
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    entity TEXT NOT NULL,
    business_date TEXT NOT NULL,
    event_ts INTEGER NOT NULL,
    tod_s INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    ingest_ts INTEGER NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    definition TEXT NOT NULL,
    source_status TEXT NOT NULL,
    completeness REAL NOT NULL,
    provisional INTEGER NOT NULL DEFAULT 1 CHECK (provisional = 1),
    quality TEXT NOT NULL CHECK (quality IN ('GOOD', 'SUSPECT')),
    quality_note TEXT,
    late INTEGER NOT NULL DEFAULT 0,
    attempt_id TEXT NOT NULL REFERENCES attempt (attempt_id),
    PRIMARY KEY (source, metric, entity, event_ts, revision)
);
CREATE INDEX IF NOT EXISTS observation_series ON observation (source, metric, entity, business_date, quality, event_ts);
CREATE INDEX IF NOT EXISTS observation_tod ON observation (source, metric, entity, quality, business_date, tod_s);
CREATE INDEX IF NOT EXISTS observation_attempt ON observation (attempt_id);

CREATE TABLE IF NOT EXISTS rejection (
    attempt_id TEXT NOT NULL REFERENCES attempt (attempt_id),
    metric TEXT,
    entity TEXT,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rejection_attempt ON rejection (attempt_id);

CREATE TABLE IF NOT EXISTS signal_state (
    rule TEXT NOT NULL,
    entity TEXT NOT NULL,
    business_date TEXT NOT NULL,
    state TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    signal_id TEXT,
    last_event_ts INTEGER,
    open_since_ts INTEGER,
    escalate_since_ts INTEGER,
    clear_since_ts INTEGER,
    cooldown_until_ts INTEGER,
    PRIMARY KEY (rule, entity, business_date)
);

CREATE TABLE IF NOT EXISTS signal_event (
    event_key TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    entity TEXT NOT NULL,
    business_date TEXT NOT NULL,
    transition TEXT NOT NULL CHECK (transition IN ('OPEN', 'ESCALATE', 'RESOLVE')),
    mode TEXT NOT NULL,
    production_alert INTEGER NOT NULL DEFAULT 0 CHECK (production_alert = 0),
    observed REAL NOT NULL,
    baseline REAL NOT NULL,
    delta REAL NOT NULL,
    event_ts INTEGER NOT NULL,
    evidence_n INTEGER NOT NULL,
    completeness REAL NOT NULL,
    freshness_minutes REAL NOT NULL,
    provisional INTEGER NOT NULL DEFAULT 1 CHECK (provisional = 1),
    detail TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS signal_event_by_date ON signal_event (business_date, rule);

CREATE TABLE IF NOT EXISTS signal_eval_count (
    business_date TEXT NOT NULL,
    rule TEXT NOT NULL,
    outcome TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (business_date, rule, outcome)
);

CREATE TABLE IF NOT EXISTS signal_review (
    signal_id TEXT PRIMARY KEY,
    verdict TEXT NOT NULL CHECK (verdict IN ('TRUE_POSITIVE', 'FALSE_POSITIVE', 'UNSURE')),
    reviewer TEXT,
    note TEXT,
    reviewed_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_run (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    business_date TEXT NOT NULL,
    started_ts REAL NOT NULL,
    facts_ready_ts REAL,
    completed_ts REAL NOT NULL,
    fact_refresh TEXT NOT NULL,
    dashboard_facts TEXT NOT NULL,
    inference_status TEXT NOT NULL,
    inference_wait_ms REAL NOT NULL,
    blocked_by_inference INTEGER NOT NULL,
    failed_step TEXT
);
'''

_LATEST_GOOD = ('SELECT * FROM observation WHERE source = ? AND metric = ? AND entity = ? AND business_date = ? '
                "AND quality = 'GOOD' ORDER BY event_ts DESC, revision DESC LIMIT 1")


def parse_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def decimal_text(number):
    return format(number.normalize(), 'f')


def observation_dict(row):
    item = dict(row)
    item['provisional'] = True
    item['late'] = bool(item['late'])
    return item


class RealtimeFactStore(Database):
    kind = contract.STORE_KIND
    schema = SCHEMA

    def __init__(self, path, catalog, tz, *, settled_paths=(), busy_timeout_ms=5000,
                 max_future_skew=300, max_event_age=26 * 3600):
        self.catalog, self.tz = catalog, tz
        self.max_future_skew, self.max_event_age = max_future_skew, max_event_age
        super().__init__(path, busy_timeout_ms=busy_timeout_ms, forbidden_paths=settled_paths)

    @classmethod
    def from_config(cls, cfg):
        conf = cfg['store']
        return cls(cfg.path('realtime_store'), cfg.catalog, cfg.tz, settled_paths=cfg.settled_paths(),
                   busy_timeout_ms=conf['busy_timeout_ms'], max_future_skew=conf['max_future_skew_seconds'],
                   max_event_age=conf['max_event_age_hours'] * 3600)

    # -- writes -----------------------------------------------------------

    def record_attempt(self, source, slot_ts, *, started_ts, finished_ts, observations=(),
                       failure=None, browser_id=None):
        """Record one collection attempt and its observations atomically.

        failure is None for a completed fetch, or (status, error_code) with status FAILED
        or SKIPPED; such attempts store no observations. Re-running a slot adds a new
        attempt; identical observations are deduplicated.
        """
        if source not in self.catalog.enabled:
            raise ValueError('unknown source %s' % source)
        required = self.catalog.required_series(source)
        day = timeutil.business_date(slot_ts, self.tz)
        counts = {'inserted': 0, 'duplicate': 0, 'suspect': 0, 'late': 0}
        with self.write() as conn:
            before = self._required_good(conn, source, required, day)
            number = conn.execute('SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempt WHERE source = ? AND slot_ts = ?',
                                  (source, slot_ts)).fetchone()[0]
            attempt_id = '%s|%d|%d' % (source, slot_ts, number)
            conn.execute('INSERT INTO attempt (attempt_id, source, slot_ts, attempt_no, business_date, started_ts, '
                         "finished_ts, status, expected_n, browser_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'FAILED', ?, ?)",
                         (attempt_id, source, slot_ts, number, day, int(started_ts), int(finished_ts),
                          len(required), browser_id))
            valid, rejected, good, suspect = [], [], set(), set()
            if failure is not None:
                status, error = failure
                if status not in ('FAILED', 'SKIPPED'):
                    raise ValueError('failure status must be FAILED or SKIPPED')
                completeness = 0.0
            else:
                valid, rejected = self._validate(source, observations, int(finished_ts))
                hint = 'SUCCESS' if {(i['metric'], i['entity']) for i in valid} >= set(required) else 'PARTIAL'
                for item in valid:
                    quality = self._insert(conn, attempt_id, source, item, int(finished_ts), hint, counts)
                    (good if quality == 'GOOD' else suspect).add((item['metric'], item['entity']))
                # Completeness counts required series that produced a GOOD value in this attempt.
                if required:
                    completeness = len(good & set(required)) / len(required)
                else:
                    completeness = 1.0 if good else 0.0
                if completeness >= 1:
                    status, error = 'SUCCESS', None
                elif valid:
                    status, error = 'PARTIAL', 'SUSPECT_VALUE' if suspect else 'MISSING_REQUIRED'
                else:
                    status, error = 'FAILED', 'NO_VALID_OBSERVATIONS'
                conn.execute('UPDATE observation SET completeness = ? WHERE attempt_id = ?', (completeness, attempt_id))
            received = good & set(required)
            for metric, entity, reason in rejected:
                conn.execute('INSERT INTO rejection VALUES (?, ?, ?, ?)', (attempt_id, metric, entity, reason))
            after = self._required_good(conn, source, required, day)
            erased = sum(1 for key, old in before.items() if old is not None and (after[key] is None or after[key] < old))
            conn.execute('UPDATE attempt SET status = ?, error_code = ?, received_n = ?, completeness = ?, inserted_n = ?, '
                         'duplicate_n = ?, rejected_n = ?, suspect_n = ?, late_n = ?, erased_good_n = ? WHERE attempt_id = ?',
                         (status, error, len(received), completeness, counts['inserted'], counts['duplicate'],
                          len(rejected), counts['suspect'], counts['late'], erased, attempt_id))
        return dict(counts, attempt_id=attempt_id, source=source, slot_ts=slot_ts, status=status, error_code=error,
                    completeness=completeness, expected=len(required), received=len(received),
                    rejected=[reason for _, _, reason in rejected], erased_good=erased)

    def _validate(self, source, observations, ingest_ts):
        valid, rejected, seen = [], [], set()
        for raw in observations:
            raw = raw if isinstance(raw, dict) else {}
            metric, entity = str(raw.get('metric') or ''), str(raw.get('entity') or '')
            entity_type = entity.split(':', 1)[0]

            def reject(reason):
                rejected.append((metric or None, entity or None, reason))

            if contract.not_realtime_class(metric, entity_type):
                reject('NOT_REALTIME_ELIGIBLE')
                continue
            spec = self.catalog.spec(source, metric, entity_type)
            if spec is None:
                reject('NOT_IN_CATALOG')
                continue
            if raw.get('definition') not in (None, spec['definition']):
                reject('DEFINITION_MISMATCH')
                continue
            value = parse_decimal(raw.get('value'))
            if value is None:
                reject('INVALID_VALUE')
                continue
            if value < 0 and spec['non_negative']:
                reject('NEGATIVE_VALUE')
                continue
            event_ts = timeutil.to_ts(raw.get('event_time'))
            if event_ts is None:
                reject('MISSING_EVENT_TIME')
                continue
            if event_ts > ingest_ts + self.max_future_skew:
                reject('FUTURE_EVENT_TIME')
                continue
            if event_ts < ingest_ts - self.max_event_age:
                reject('STALE_EVENT_TIME')
                continue
            key = (metric, entity, event_ts)
            if key in seen:
                reject('DUPLICATE_IN_BATCH')
                continue
            seen.add(key)
            valid.append({'metric': metric, 'entity': entity, 'event_ts': event_ts, 'value': value, 'spec': spec,
                          'source_status': str(raw.get('source_status') or '')})
        return valid, rejected

    def _insert(self, conn, attempt_id, source, item, ingest_ts, status, counts):
        """Insert one validated observation; return its quality (a duplicate keeps the stored quality)."""
        series = (source, item['metric'], item['entity'])
        event_ts, value, spec = item['event_ts'], item['value'], item['spec']
        prior = conn.execute('SELECT revision, value, quality FROM observation WHERE source = ? AND metric = ? '
                             'AND entity = ? AND event_ts = ? ORDER BY revision DESC LIMIT 1', series + (event_ts,)).fetchone()
        if prior is not None and Decimal(prior['value']) == value:
            counts['duplicate'] += 1
            return prior['quality']
        revision = prior['revision'] + 1 if prior else 1
        day = timeutil.business_date(event_ts, self.tz)
        late = conn.execute('SELECT 1 FROM observation WHERE source = ? AND metric = ? AND entity = ? '
                            'AND business_date = ? AND event_ts > ? LIMIT 1', series + (day, event_ts)).fetchone() is not None
        quality, note = self._quality(conn, series, day, event_ts, value, spec)
        if note is None and revision > 1:
            note = 'RESTATED'
        conn.execute('INSERT INTO observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)', series + (
            day, event_ts, event_ts - timeutil.day_start(day, self.tz), revision, ingest_ts, decimal_text(value),
            spec['unit'], spec['definition'], item['source_status'] or status, 0.0, quality, note,
            int(late), attempt_id))
        counts['inserted'] += 1
        counts['late'] += int(late)
        counts['suspect'] += quality == 'SUSPECT'
        return quality

    def _quality(self, conn, series, day, event_ts, value, spec):
        """Cumulative day totals should not fall. A fall is held as SUSPECT until confirmed;
        a fall to zero is never auto-confirmed (it is almost always a broken page)."""
        if not spec['cumulative']:
            return 'GOOD', None
        prev = conn.execute('SELECT value, event_ts FROM observation WHERE source = ? AND metric = ? AND entity = ? '
                            "AND business_date = ? AND quality = 'GOOD' AND event_ts < ? "
                            'ORDER BY event_ts DESC, revision DESC LIMIT 1', series + (day, event_ts)).fetchone()
        if prev is None or Decimal(prev['value']) <= 0:
            return 'GOOD', None
        floor = Decimal(prev['value']) * (1 - Decimal(str(spec['max_drop_ratio'])))
        if value == 0:
            return 'SUSPECT', 'DROP_TO_ZERO'
        if value >= floor:
            return 'GOOD', None
        need = spec['confirm_n'] - 1
        rows = conn.execute('SELECT quality, quality_note FROM observation WHERE source = ? AND metric = ? AND entity = ? '
                            'AND business_date = ? AND event_ts < ? AND event_ts > ? ORDER BY event_ts DESC, revision DESC '
                            'LIMIT ?', series + (day, event_ts, prev['event_ts'], max(need, 0))).fetchall()
        if len(rows) == need and all(r['quality'] == 'SUSPECT' and r['quality_note'] == 'CUMULATIVE_DROP' for r in rows):
            return 'GOOD', 'CONFIRMED_DROP'
        return 'SUSPECT', 'CUMULATIVE_DROP'

    def _required_good(self, conn, source, required, day):
        out = {}
        for metric, entity in required:
            row = conn.execute(_LATEST_GOOD, (source, metric, entity, day)).fetchone()
            out[(metric, entity)] = (row['event_ts'], row['revision']) if row else None
        return out

    def log_refresh(self, run):
        columns = ('run_id', 'kind', 'business_date', 'started_ts', 'facts_ready_ts', 'completed_ts', 'fact_refresh',
                   'dashboard_facts', 'inference_status', 'inference_wait_ms', 'blocked_by_inference', 'failed_step')
        with self.write() as conn:
            conn.execute('INSERT OR REPLACE INTO refresh_run (%s) VALUES (%s)' % (', '.join(columns), ', '.join('?' * len(columns))),
                         tuple(run.get(c) for c in columns))

    def apply_retention(self, today, keep_days, signal_keep_days=180):
        cutoff = timeutil.shift_day(today, -keep_days)
        signal_cutoff = timeutil.shift_day(today, -max(signal_keep_days, keep_days))
        with self.write() as conn:
            observations = conn.execute('DELETE FROM observation WHERE business_date < ?', (cutoff,)).rowcount
            conn.execute('DELETE FROM rejection WHERE attempt_id IN (SELECT attempt_id FROM attempt WHERE business_date < ?)',
                         (cutoff,))
            attempts = conn.execute('DELETE FROM attempt WHERE business_date < ? AND NOT EXISTS '
                                    '(SELECT 1 FROM observation o WHERE o.attempt_id = attempt.attempt_id)', (cutoff,)).rowcount
            conn.execute('DELETE FROM refresh_run WHERE business_date < ?', (cutoff,))
            for table in ('signal_state', 'signal_event', 'signal_eval_count'):
                conn.execute('DELETE FROM %s WHERE business_date < ?' % table, (signal_cutoff,))
            conn.execute('DELETE FROM signal_review WHERE signal_id NOT IN (SELECT signal_id FROM signal_event)')
        with self.read() as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return {'observations_deleted': observations, 'attempts_deleted': attempts, 'cutoff': cutoff}

    # -- reads ------------------------------------------------------------

    def latest_attempt(self, source, conn=None):
        rows = self.rows('SELECT * FROM attempt WHERE source = ? ORDER BY finished_ts DESC, slot_ts DESC, '
                         'attempt_no DESC LIMIT 1', (source,), conn)
        return rows[0] if rows else None

    def latest_success(self, source, conn=None):
        rows = self.rows("SELECT * FROM attempt WHERE source = ? AND status = 'SUCCESS' "
                         'ORDER BY finished_ts DESC, slot_ts DESC, attempt_no DESC LIMIT 1', (source,), conn)
        return rows[0] if rows else None

    def latest_good(self, source, metric, entity, business_day, conn=None):
        rows = self.rows(_LATEST_GOOD, (source, metric, entity, business_day), conn)
        return observation_dict(rows[0]) if rows else None

    def good_snapshot(self, business_day, conn=None):
        """Latest good observation of every series for a business day."""
        rows = self.rows('SELECT o.* FROM observation o JOIN (SELECT source, metric, entity, MAX(event_ts) AS e '
                         "FROM observation WHERE business_date = ? AND quality = 'GOOD' GROUP BY source, metric, entity) m "
                         'ON o.source = m.source AND o.metric = m.metric AND o.entity = m.entity AND o.event_ts = m.e '
                         "WHERE o.quality = 'GOOD' ORDER BY o.revision", (business_day,), conn)
        return {(r['source'], r['metric'], r['entity']): observation_dict(r) for r in rows}

    def good_at_or_before(self, conn, source, metric, entity, business_day, max_ts, min_ts):
        row = conn.execute('SELECT value, event_ts FROM observation WHERE source = ? AND metric = ? AND entity = ? '
                           "AND business_date = ? AND quality = 'GOOD' AND event_ts <= ? AND event_ts >= ? "
                           'ORDER BY event_ts DESC, revision DESC LIMIT 1',
                           (source, metric, entity, business_day, max_ts, min_ts)).fetchone()
        return (Decimal(row['value']), row['event_ts']) if row else None

    def good_by_time_of_day(self, conn, source, metric, entity, first_day, last_day, tod_from, tod_to):
        """{business_date: (value, tod_s)} using the last good value inside [tod_from, tod_to] per day."""
        out = {}
        for row in conn.execute('SELECT business_date, value, tod_s FROM observation WHERE source = ? AND metric = ? '
                                "AND entity = ? AND quality = 'GOOD' AND business_date >= ? AND business_date <= ? "
                                'AND tod_s >= ? AND tod_s <= ? ORDER BY business_date, tod_s, revision',
                                (source, metric, entity, first_day, last_day, tod_from, tod_to)):
            out[row['business_date']] = (Decimal(row['value']), row['tod_s'])
        return out

    def entities(self, conn, source, metric, entity_type, business_day):
        prefix = entity_type + ':'
        return [r[0] for r in conn.execute("SELECT DISTINCT entity FROM observation WHERE source = ? AND metric = ? "
                                           "AND business_date = ? AND quality = 'GOOD' AND substr(entity, 1, ?) = ? "
                                           'ORDER BY entity', (source, metric, business_day, len(prefix), prefix))]

    def attempts_for_day(self, business_day, conn=None):
        return self.rows('SELECT * FROM attempt WHERE business_date = ? ORDER BY slot_ts, attempt_no', (business_day,), conn)
