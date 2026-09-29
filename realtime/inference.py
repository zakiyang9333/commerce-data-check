"""MiMo inference layer: event-driven queue, MIMO_INFERENCE_CACHE_V1, circuit breaker and a
daily token budget, in its own database so model work can never block or fail facts.

Only signal OPEN/ESCALATE/RESOLVE, the settled daily batch and a user "why" enter the
queue. Inputs are limited to verified facts, deterministic signals, permitted aggregates
and sanitized customer-service evidence. Output is MODEL_INFERENCE: never executed and
stripped of fields owned by deterministic code (GMV, ROI, fee rate, freshness,
thresholds, STOP, budget execution). Submitting only writes a queue row; a separate
worker calls the model.
"""
import hashlib
import json
import math
import re
import socket
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal

from . import contract, timeutil
from .db import Database

SCHEMA = '''
CREATE TABLE IF NOT EXISTS inference_job (
    job_id TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    feature TEXT NOT NULL,
    priority INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    entity TEXT NOT NULL,
    date_window TEXT NOT NULL,
    signal_state TEXT NOT NULL,
    business_date TEXT NOT NULL,
    payload TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    components TEXT NOT NULL,
    status TEXT NOT NULL,
    cache_hit INTEGER NOT NULL,
    cache_miss_reason TEXT,
    enqueued_ts REAL NOT NULL,
    started_ts REAL,
    finished_ts REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    hard_timeout_s REAL NOT NULL,
    error_code TEXT,
    auto_execution INTEGER NOT NULL DEFAULT 0 CHECK (auto_execution = 0)
);
CREATE INDEX IF NOT EXISTS job_pending ON inference_job (status, priority, enqueued_ts);
CREATE INDEX IF NOT EXISTS job_view ON inference_job (lane, feature, entity, date_window, enqueued_ts);
CREATE INDEX IF NOT EXISTS job_key ON inference_job (cache_key, status);
CREATE INDEX IF NOT EXISTS job_day ON inference_job (business_date, lane);

CREATE TABLE IF NOT EXISTS inference_attempt (
    job_id TEXT NOT NULL REFERENCES inference_job (job_id),
    attempt_no INTEGER NOT NULL,
    lane TEXT NOT NULL,
    business_date TEXT NOT NULL,
    started_ts REAL NOT NULL,
    latency_ms REAL NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('OK', 'TIMEOUT', 'ERROR')),
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    reported_model_name TEXT,
    error_code TEXT,
    dropped_fields TEXT,
    PRIMARY KEY (job_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS attempt_day ON inference_attempt (business_date, lane);

CREATE TABLE IF NOT EXISTS inference_cache (
    cache_key TEXT PRIMARY KEY,
    lane TEXT NOT NULL,
    feature TEXT NOT NULL,
    entity TEXT NOT NULL,
    date_window TEXT NOT NULL,
    components TEXT NOT NULL,
    output TEXT NOT NULL,
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inference_cache_index (
    lane TEXT NOT NULL,
    feature TEXT NOT NULL,
    entity TEXT NOT NULL,
    date_window TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    components TEXT NOT NULL,
    PRIMARY KEY (lane, feature, entity, date_window)
);

CREATE TABLE IF NOT EXISTS circuit_state (
    lane TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    opened_ts REAL,
    probe_in_flight INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS circuit_event (
    lane TEXT NOT NULL,
    business_date TEXT NOT NULL,
    event TEXT NOT NULL,
    at_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS token_ledger (
    job_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    lane TEXT NOT NULL,
    priority INTEGER NOT NULL,
    reserved INTEGER NOT NULL,
    charged INTEGER,
    settled INTEGER NOT NULL DEFAULT 0,
    at_ts REAL NOT NULL,
    PRIMARY KEY (job_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS ledger_day ON token_ledger (business_date);

CREATE TABLE IF NOT EXISTS gate_count (
    business_date TEXT NOT NULL,
    trigger TEXT NOT NULL,
    reason TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (business_date, trigger, reason)
);

CREATE TABLE IF NOT EXISTS model_registry (
    lane TEXT PRIMARY KEY,
    requested_model TEXT,
    reported_model_name TEXT,
    build_fingerprint TEXT,
    updated_ts REAL NOT NULL
);
'''

ACTIVE_JOB = ('PENDING', 'RUNNING')
FAILED_JOB = ('FAILED', 'TIMEOUT', 'WORKER_LOST')
VOLATILE_KEYS = frozenset({
    'event_time', 'event_ts', 'ingest_time', 'ingest_ts', 'freshness', 'freshness_minutes', 'lag_minutes',
    'data_as_of', 'last_success_at', 'generated_at', 'created_ts', 'checked_at', 'signal_id', 'attempt_id',
})
RATIO_HINTS = ('delta', 'ratio', 'rate', 'roi', 'share', 'pct', 'completeness')
PII_PATTERNS = (
    ('EMAIL', re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'), '[邮箱]'),
    ('ID_NUMBER', re.compile(r'(?<![0-9Xx])\d{17}[0-9Xx](?![0-9Xx])'), '[证件号]'),
    ('MOBILE', re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '[手机号]'),
    ('LONG_NUMBER', re.compile(r'(?<!\d)\d{10,}(?!\d)'), '[编号]'),
)

PROMPTS = {
    'signal_explanation': '你是电商经营看板的分析助手。下面是确定性规则在近实时（未结算）数据上产生的信号及已核验事实。'
                          '请用简体中文解释最可能的原因（不超过120字），并列出需要人工核查的数据点。',
    'settled_daily_batch': '你是电商经营看板的分析助手。下面是已结算的日度事实与聚合。请用简体中文总结要点与需要关注的变化。',
    'on_demand_why': '你是电商经营看板的分析助手。用户询问下面这项数据“为什么”变化。请基于给出的事实与信号，用简体中文给出可能原因与核查建议。',
    'background_enrichment': '你是电商经营看板的分析助手。请基于下面的已核验事实补充简短背景说明（简体中文）。',
}
GUARDRAIL = ('规则：输入中的数字均由确定性系统计算，近实时数据尚未结算。你只能解释可能原因并建议核查项；'
             '不得给出、改写或决定 GMV、ROI、费率、新鲜度、异常阈值、STOP/止损决定或预算执行；不要输出任何可执行动作。'
             '仅输出 JSON：{"summary": "...", "hypotheses": ["..."], "checks": ["..."]}')


class PayloadRejected(ValueError):
    pass


class InferenceTimeout(Exception):
    pass


class SystemClock:
    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()


@dataclass
class InferenceRequest:
    feature: str
    trigger: str
    entity: str
    date_window: str
    payload: dict
    signal_state: str = ''
    prompt: str = None
    prompt_version: str = None


@dataclass
class BackendResponse:
    text: str
    reported_model_name: str = None
    prompt_tokens: int = None
    completion_tokens: int = None
    total_tokens: int = None
    fingerprint: str = None


# -- inputs -------------------------------------------------------------------

def find_pii(text):
    return [kind for kind, pattern, _ in PII_PATTERNS if pattern.search(text or '')]


def sanitize_text(text):
    for _, pattern, replacement in PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitized_evidence(text, **extra):
    """Build a cs_evidence item: PII masked and marked sanitized."""
    return dict(extra, text=sanitize_text(text), sanitized=True)


def validate_payload(payload):
    """Accept only verified facts, deterministic signals, flat aggregates and sanitized CS evidence."""
    if not isinstance(payload, dict):
        raise PayloadRejected('PAYLOAD_NOT_OBJECT')
    extra = set(payload) - contract.PERMITTED_INFERENCE_INPUTS
    if extra:
        raise PayloadRejected('UNPERMITTED_INPUT:' + ','.join(sorted(extra)))
    for fact in payload.get('facts', []):
        if not isinstance(fact, dict) or fact.get('verified') is not True or \
                fact.get('basis') not in (contract.SETTLED_BASIS, contract.REALTIME_BASIS):
            raise PayloadRejected('UNVERIFIED_FACT')
        if fact['basis'] == contract.REALTIME_BASIS and fact.get('provisional') is not True:
            raise PayloadRejected('REALTIME_FACT_NOT_PROVISIONAL')
    for signal in payload.get('signals', []):
        if not isinstance(signal, dict) or not {'rule', 'observed', 'baseline', 'delta'} <= set(signal):
            raise PayloadRejected('NOT_A_DETERMINISTIC_SIGNAL')
    aggregates = payload.get('aggregates', {})
    if not isinstance(aggregates, dict) or any(isinstance(v, (dict, list, tuple)) for v in aggregates.values()):
        raise PayloadRejected('NON_AGGREGATE_INPUT')
    for item in payload.get('cs_evidence', []):
        if not isinstance(item, dict) or item.get('sanitized') is not True or find_pii(str(item.get('text', ''))):
            raise PayloadRejected('UNSANITIZED_CS_EVIDENCE')


def _round(key, value, digits, step):
    if isinstance(value, dict):
        return {k: _round(k, v, digits, step) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_round(key, v, digits, step) for v in value]
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float, Decimal)):
        return value
    number = float(value)
    if not math.isfinite(number):
        return str(number)
    if any(hint in str(key).lower() for hint in RATIO_HINTS):
        return round(round(number / step) * step, 6)
    return float('%.*g' % (digits, number))


def rounded_fact_hash(payload, significant_digits=2, ratio_step=0.05):
    """Hash of meaningful facts: numbers rounded, timestamps and ids dropped, so the key only
    changes when the facts change in a way a reader would notice."""
    canonical = _round('', payload, significant_digits, ratio_step)
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def cache_key(components):
    text = json.dumps(components, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build_prompt(request, prompt_version):
    if request.prompt:
        return request.prompt
    return '\n'.join([PROMPTS[request.feature], GUARDRAIL, '版本：' + prompt_version,
                      '输入：' + json.dumps(request.payload, ensure_ascii=False, sort_keys=True, default=str)])


# -- outputs ------------------------------------------------------------------

def parse_model_text(text):
    body = (text or '').strip()
    if body.startswith('```'):
        body = body.strip('`')
        body = body[4:] if body.lower().startswith('json') else body
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    return parsed if isinstance(parsed, dict) else {'summary': (text or '').strip()}


def sanitize_output(content):
    """Drop keys the model does not own; return (clean content, dropped key paths)."""
    banned = contract.MODEL_FORBIDDEN_FIELDS | contract.EXECUTION_FIELDS
    dropped = []

    def clean(value, path):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if str(key).lower() in banned:
                    dropped.append(path + str(key))
                else:
                    out[key] = clean(item, path + str(key) + '.')
            return out
        if isinstance(value, list):
            return [clean(item, path) for item in value]
        return value

    return clean(content, ''), dropped


# -- store --------------------------------------------------------------------

class InferenceStore(Database):
    kind = 'MODEL_INFERENCE_STORE'
    schema = SCHEMA

    def model_info(self, lane, conn=None):
        rows = self.rows('SELECT * FROM model_registry WHERE lane = ?', (lane,), conn)
        return rows[0] if rows else None

    def count_gate(self, conn, day, trigger, reason):
        conn.execute('INSERT INTO gate_count VALUES (?, ?, ?, 1) ON CONFLICT (business_date, trigger, reason) '
                     'DO UPDATE SET n = n + 1', (day, trigger, reason))

    def daily_metrics(self, day, lanes=None):
        """Shadow metrics for one business day, optionally limited to some lanes."""
        with self.read() as conn:
            lane_filter, args = '', [day]
            if lanes:
                lane_filter = ' AND lane IN (%s)' % ','.join('?' * len(lanes))
                args += list(lanes)
            attempts = [dict(r) for r in conn.execute('SELECT * FROM inference_attempt WHERE business_date = ?' + lane_filter, args)]
            jobs = [dict(r) for r in conn.execute('SELECT status, cache_hit, cache_miss_reason FROM inference_job '
                                                  'WHERE business_date = ?' + lane_filter, args)]
            tokens = conn.execute('SELECT COALESCE(SUM(CASE WHEN settled = 1 THEN charged ELSE reserved END), 0) '
                                  'FROM token_ledger WHERE business_date = ?' + lane_filter, args).fetchone()[0]
            opens = conn.execute("SELECT COUNT(*) FROM circuit_event WHERE business_date = ? AND event = 'OPEN'" + lane_filter,
                                 args).fetchone()[0]
            gates = [dict(r) for r in conn.execute('SELECT trigger, reason, n FROM gate_count WHERE business_date = ?', (day,))]
        latencies = sorted(a['latency_ms'] for a in attempts)
        lookups = [j for j in jobs if j['status'] not in ('SUPERSEDED',)]
        hits = sum(1 for j in lookups if j['cache_hit'])
        misses = {}
        for job in lookups:
            if not job['cache_hit'] and job['cache_miss_reason']:
                misses[job['cache_miss_reason']] = misses.get(job['cache_miss_reason'], 0) + 1
        return {
            'mimo_call_n': len(attempts),
            'mimo_cache_hit_rate': round(hits / len(lookups), 4) if lookups else None,
            'mimo_cache_hit_n': hits,
            'mimo_cache_miss_reasons': misses,
            'mimo_p50_latency_ms': _percentile(latencies, 50),
            'mimo_p95_latency_ms': _percentile(latencies, 95),
            'mimo_timeout_n': sum(1 for a in attempts if a['outcome'] == 'TIMEOUT'),
            'mimo_error_n': sum(1 for a in attempts if a['outcome'] == 'ERROR'),
            'mimo_token_n': int(tokens),
            'mimo_circuit_open_n': opens,
            'mimo_budget_skip_n': sum(1 for j in jobs if j['status'] == 'SKIPPED_BUDGET'),
            'mimo_ownership_violation_n': sum(1 for a in attempts if a['dropped_fields'] not in (None, '[]')),
            'no_mimo_call': gates,
        }


def _percentile(values, pct):
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method='inclusive')[pct - 1], 1)


class CircuitBreaker:
    """Per-lane breaker: N consecutive failures open it; after the cool-off one probe may run."""

    def __init__(self, threshold=3, cooloff_s=900):
        self.threshold, self.cooloff_s = threshold, cooloff_s

    @staticmethod
    def state(conn, lane):
        row = conn.execute('SELECT * FROM circuit_state WHERE lane = ?', (lane,)).fetchone()
        return dict(row) if row else {'lane': lane, 'state': 'CLOSED', 'consecutive_failures': 0,
                                      'opened_ts': None, 'probe_in_flight': 0}

    @staticmethod
    def _save(conn, row):
        conn.execute('INSERT OR REPLACE INTO circuit_state VALUES (?, ?, ?, ?, ?)',
                     (row['lane'], row['state'], row['consecutive_failures'], row['opened_ts'], row['probe_in_flight']))

    def gate(self, conn, lane, now):
        row = self.state(conn, lane)
        if row['state'] == 'CLOSED':
            return 'CLOSED'
        if row['probe_in_flight']:
            return 'OPEN'
        if row['state'] == 'HALF_OPEN' or now - row['opened_ts'] >= self.cooloff_s:
            return 'PROBE'
        return 'OPEN'

    def start_probe(self, conn, lane):
        row = self.state(conn, lane)
        row.update(state='HALF_OPEN', probe_in_flight=1)
        self._save(conn, row)

    def success(self, conn, lane, now, tz):
        row = self.state(conn, lane)
        if row['state'] != 'CLOSED':
            conn.execute('INSERT INTO circuit_event VALUES (?, ?, ?, ?)', (lane, timeutil.business_date(now, tz), 'CLOSE', now))
        self._save(conn, {'lane': lane, 'state': 'CLOSED', 'consecutive_failures': 0, 'opened_ts': None,
                          'probe_in_flight': 0})

    def failure(self, conn, lane, now, tz):
        """Record a failure; return True when the breaker is open afterwards."""
        row = self.state(conn, lane)
        row['consecutive_failures'] += 1
        if row['state'] == 'HALF_OPEN' or row['consecutive_failures'] >= self.threshold:
            if row['state'] != 'OPEN':
                conn.execute('INSERT INTO circuit_event VALUES (?, ?, ?, ?)', (lane, timeutil.business_date(now, tz), 'OPEN', now))
            row.update(state='OPEN', opened_ts=now, probe_in_flight=0)
            self._save(conn, row)
            return True
        self._save(conn, row)
        return row['state'] == 'OPEN'


class TokenBudget:
    """Daily token budget with priority bands: lower priorities stop first as the day fills up."""

    def __init__(self, daily_budget, shares):
        self.daily_budget = int(daily_budget)
        self.shares = {int(k): float(v) for k, v in shares.items()}

    def limit(self, priority):
        return int(self.daily_budget * self.shares[priority])

    @staticmethod
    def used(conn, day):
        return conn.execute('SELECT COALESCE(SUM(CASE WHEN settled = 1 THEN charged ELSE reserved END), 0) '
                            'FROM token_ledger WHERE business_date = ?', (day,)).fetchone()[0]

    def allows(self, conn, day, priority, estimate):
        return self.used(conn, day) + estimate <= self.limit(priority)

    def reserve(self, conn, job, attempt_no, estimate, day, now):
        if not self.allows(conn, day, job['priority'], estimate):
            return False
        conn.execute('INSERT INTO token_ledger (job_id, attempt_no, business_date, lane, priority, reserved, at_ts) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?)', (job['job_id'], attempt_no, day, job['lane'], job['priority'], estimate, now))
        return True

    @staticmethod
    def settle(conn, job_id, attempt_no, charged):
        conn.execute('UPDATE token_ledger SET charged = ?, settled = 1 WHERE job_id = ? AND attempt_no = ?',
                     (int(charged), job_id, attempt_no))


def _effective_model(conn, lane):
    row = conn.execute('SELECT reported_model_name FROM model_registry WHERE lane = ?', (lane['name'],)).fetchone()
    return (row[0] if row and row[0] else None) or lane['model']


# -- queue --------------------------------------------------------------------

class InferenceQueue:
    """Admission: event gate, input guard, cache lookup, budget pre-check, then a PENDING row."""

    def __init__(self, store, conf, lanes, tz, clock=None):
        self.store, self.conf, self.lanes, self.tz = store, conf, lanes, tz
        self.clock = clock or SystemClock()
        self.budget = TokenBudget(conf['daily_token_budget'], conf['priority_budget_share'])

    @classmethod
    def from_config(cls, cfg, store, clock=None):
        return cls(store, cfg['inference'], cfg.lanes, cfg.tz, clock)

    def _gate(self, day, trigger, reason):
        with self.store.write() as conn:
            self.store.count_gate(conn, day, trigger, reason)
        return [{'lane': None, 'status': 'NO_MIMO_CALL', 'reason': reason}]

    def submit(self, request):
        """Queue work for every lane that serves this feature. Never calls a model."""
        now = self.clock.time()
        day = timeutil.business_date(now, self.tz)
        if request.trigger not in contract.INFERENCE_TRIGGERS:
            return self._gate(day, str(request.trigger), 'TRIGGER_NOT_ALLOWED')
        if request.feature not in contract.FEATURE_PRIORITY:
            raise ValueError('unknown feature %s' % request.feature)
        if request.prompt and not request.prompt_version:
            raise ValueError('a custom prompt needs its own prompt_version')
        try:
            validate_payload(request.payload)
        except PayloadRejected as exc:
            return self._gate(day, request.trigger, str(exc))
        lanes = [lane for lane in self.lanes.values() if request.feature in lane['features']]
        if not lanes:
            return self._gate(day, request.trigger, 'NO_LANE_FOR_FEATURE')
        results = []
        with self.store.write() as conn:
            for lane in lanes:
                if not lane['enabled']:
                    self.store.count_gate(conn, day, request.trigger, 'LANE_DISABLED:' + lane['name'])
                    results.append({'lane': lane['name'], 'status': 'NO_MIMO_CALL', 'reason': 'LANE_DISABLED'})
                else:
                    results.append(self._enqueue(conn, lane, request, now, day))
        return results

    def components(self, conn, lane, request):
        rounding = self.conf['rounding']
        return {
            'lane': lane['name'],
            'feature': request.feature,
            'entity': request.entity,
            'rounded_fact_hash': rounded_fact_hash(request.payload, rounding['significant_digits'], rounding['ratio_step']),
            'signal_state': request.signal_state or '',
            'date_window': request.date_window,
            'prompt_version': request.prompt_version or self.conf['prompt_versions'][request.feature],
            'reported_model_name': _effective_model(conn, lane),
        }

    def _enqueue(self, conn, lane, request, now, day):
        components = self.components(conn, lane, request)
        key = cache_key(components)
        base = {'lane': lane['name'], 'feature': request.feature, 'entity': request.entity}
        running = conn.execute("SELECT job_id, status FROM inference_job WHERE cache_key = ? AND status IN ('PENDING', 'RUNNING')",
                               (key,)).fetchone()
        if running is not None:
            return dict(base, status=running['status'], job_id=running['job_id'], coalesced=True)
        priority = contract.FEATURE_PRIORITY[request.feature]
        prompt = build_prompt(request, components['prompt_version'])
        job = {
            'job_id': uuid.uuid4().hex, 'lane': lane['name'], 'feature': request.feature, 'priority': priority,
            'trigger': request.trigger, 'entity': request.entity, 'date_window': request.date_window,
            'signal_state': components['signal_state'], 'business_date': day,
            'payload': json.dumps(request.payload, ensure_ascii=False, sort_keys=True, default=str), 'prompt': prompt,
            'cache_key': key, 'components': json.dumps(components, sort_keys=True), 'cache_hit': 0,
            'cache_miss_reason': None, 'enqueued_ts': now, 'finished_ts': None,
            'hard_timeout_s': self.conf['hard_timeout_seconds'][request.feature],
        }
        entry = conn.execute('SELECT output FROM inference_cache WHERE cache_key = ? AND expires_ts > ?', (key, now)).fetchone()
        if entry is not None:
            conn.execute('UPDATE inference_cache SET hit_count = hit_count + 1 WHERE cache_key = ?', (key,))
            self._insert(conn, dict(job, status='CACHED', cache_hit=1, finished_ts=now))
            return dict(base, status='CACHED', job_id=job['job_id'], cache_hit=True, output=json.loads(entry['output']))
        job['cache_miss_reason'] = self._miss_reason(conn, components, key)
        if not self.budget.allows(conn, day, priority, self.estimate(prompt, request.feature)):
            self._insert(conn, dict(job, status='SKIPPED_BUDGET', finished_ts=now))
            return dict(base, status='SKIPPED_BUDGET', job_id=job['job_id'], cache_hit=False)
        conn.execute("UPDATE inference_job SET status = 'SUPERSEDED', finished_ts = ? WHERE lane = ? AND feature = ? "
                     "AND entity = ? AND date_window = ? AND status = 'PENDING'",
                     (now, lane['name'], request.feature, request.entity, request.date_window))
        self._insert(conn, dict(job, status='PENDING'))
        return dict(base, status='PENDING', job_id=job['job_id'], cache_hit=False,
                    cache_miss_reason=job['cache_miss_reason'])

    def estimate(self, prompt, feature):
        return math.ceil(len(prompt) / self.conf['chars_per_token']) + self.conf['max_output_tokens'][feature]

    @staticmethod
    def _insert(conn, job):
        columns = ('job_id', 'lane', 'feature', 'priority', 'trigger', 'entity', 'date_window', 'signal_state',
                   'business_date', 'payload', 'prompt', 'cache_key', 'components', 'status', 'cache_hit',
                   'cache_miss_reason', 'enqueued_ts', 'finished_ts', 'hard_timeout_s')
        conn.execute('INSERT INTO inference_job (%s) VALUES (%s)' % (', '.join(columns), ', '.join('?' * len(columns))),
                     tuple(job[c] for c in columns))

    @staticmethod
    def _miss_reason(conn, components, key):
        row = conn.execute('SELECT cache_key, components FROM inference_cache_index WHERE lane = ? AND feature = ? '
                           'AND entity = ? AND date_window = ?', (components['lane'], components['feature'],
                                                                  components['entity'], components['date_window'])).fetchone()
        if row is None:
            return 'NO_PRIOR_ENTRY'
        if row['cache_key'] == key:
            return 'EXPIRED'
        prior = json.loads(row['components'])
        for name, reason in (('reported_model_name', 'MODEL_CHANGED'), ('prompt_version', 'PROMPT_VERSION_CHANGED'),
                             ('signal_state', 'SIGNAL_STATE_CHANGED'), ('rounded_fact_hash', 'FACTS_CHANGED')):
            if prior.get(name) != components.get(name):
                return reason
        return 'KEY_CHANGED'

    def view(self, lane, feature, entity, date_window):
        """What the dashboard may show for one inference slot. Reads only; never enqueues."""
        now = self.clock.time()
        with self.store.read() as conn:
            job = conn.execute('SELECT * FROM inference_job WHERE lane = ? AND feature = ? AND entity = ? AND date_window = ? '
                               "AND status != 'SUPERSEDED' ORDER BY enqueued_ts DESC LIMIT 1",
                               (lane, feature, entity, date_window)).fetchone()
            if job is None:
                return {'status': 'NOT_REQUESTED', 'label': contract.LABEL_AI_PENDING, 'progress_label': None}
            active = job['status'] in ACTIVE_JOB
            progress = contract.LABEL_AI_RUNNING if active else None
            entry = conn.execute('SELECT output FROM inference_cache WHERE cache_key = ? AND expires_ts > ?',
                                 (job['cache_key'], now)).fetchone()
            if entry is not None:
                return {'status': 'READY', 'label': None, 'progress_label': None, 'output': json.loads(entry['output'])}
            index = conn.execute('SELECT c.output FROM inference_cache_index i JOIN inference_cache c ON c.cache_key = i.cache_key '
                                 'WHERE i.lane = ? AND i.feature = ? AND i.entity = ? AND i.date_window = ? AND c.expires_ts > ?',
                                 (lane, feature, entity, date_window, now)).fetchone()
            if index is not None:
                return {'status': 'STALE', 'label': contract.LABEL_AI_STALE, 'progress_label': progress,
                        'output': json.loads(index['output'])}
            status = 'PENDING' if active else ('FAILED' if job['status'] in FAILED_JOB else job['status'])
            return {'status': status, 'label': contract.LABEL_AI_PENDING, 'progress_label': progress}


# -- worker -------------------------------------------------------------------

class InferenceWorker:
    """Runs queued jobs one at a time, outside the refresh path."""

    def __init__(self, store, conf, lanes, tz, backends, clock=None, grace_s=5):
        self.store, self.conf, self.lanes, self.tz = store, conf, lanes, tz
        self.backends = dict(backends)
        self.clock = clock or SystemClock()
        self.grace_s = grace_s
        self.budget = TokenBudget(conf['daily_token_budget'], conf['priority_budget_share'])
        self.circuit = CircuitBreaker(conf['circuit_failure_threshold'], conf['circuit_cooloff_minutes'] * 60)

    @classmethod
    def from_config(cls, cfg, store, backends, clock=None):
        return cls(store, cfg['inference'], cfg.lanes, cfg.tz, backends, clock)

    def drain(self, max_jobs=100):
        done = []
        while len(done) < max_jobs:
            result = self.run_once()
            if result is None:
                break
            done.append(result)
        return done

    def run_forever(self, stop_event, idle_seconds=5):
        while not stop_event.is_set():
            if self.run_once() is None:
                stop_event.wait(idle_seconds)

    def run_once(self):
        job = self._claim()
        return None if job is None else self._execute(job)

    def _expire(self, conn, now):
        for feature, minutes in self.conf['max_queue_age_minutes'].items():
            conn.execute("UPDATE inference_job SET status = 'EXPIRED', finished_ts = ? WHERE status = 'PENDING' "
                         'AND feature = ? AND enqueued_ts < ?', (now, feature, now - minutes * 60))
        lost = conn.execute("SELECT job_id, lane FROM inference_job WHERE status = 'RUNNING' AND started_ts < ? - "
                            '3 * (hard_timeout_s + ?) * (1 + ?)', (now, self.grace_s, self.conf['max_retries'])).fetchall()
        for row in lost:
            conn.execute("UPDATE inference_job SET status = 'WORKER_LOST', finished_ts = ? WHERE job_id = ?", (now, row['job_id']))
            conn.execute('UPDATE circuit_state SET probe_in_flight = 0 WHERE lane = ?', (row['lane'],))

    def _claim(self):
        now = self.clock.time()
        with self.store.write() as conn:
            self._expire(conn, now)
            for row in conn.execute("SELECT * FROM inference_job WHERE status = 'PENDING' ORDER BY priority, enqueued_ts").fetchall():
                job = dict(row)
                lane = self.lanes.get(job['lane'])
                if lane is None or not lane['enabled']:
                    conn.execute("UPDATE inference_job SET status = 'SKIPPED_LANE_DISABLED', finished_ts = ? WHERE job_id = ?",
                                 (now, job['job_id']))
                    continue
                if job['lane'] not in self.backends:
                    continue  # served by another worker (e.g. the challenger lane's own evaluator)
                gate = self.circuit.gate(conn, job['lane'], now)
                if gate == 'OPEN':
                    continue
                if conn.execute('SELECT 1 FROM inference_cache WHERE cache_key = ? AND expires_ts > ?',
                                (job['cache_key'], now)).fetchone():
                    conn.execute("UPDATE inference_job SET status = 'CACHED', cache_hit = 1, cache_miss_reason = NULL, "
                                 'finished_ts = ? WHERE job_id = ?', (now, job['job_id']))
                    continue
                if gate == 'PROBE':
                    self.circuit.start_probe(conn, job['lane'])
                conn.execute("UPDATE inference_job SET status = 'RUNNING', started_ts = ? WHERE job_id = ?", (now, job['job_id']))
                return job
        return None

    def _invoke(self, backend, model, prompt, max_tokens, timeout):
        box = {}

        def call():
            try:
                box['response'] = backend.complete(prompt, model=model, max_tokens=max_tokens, timeout=timeout)
            except BaseException as exc:  # handed back to the worker thread below
                box['error'] = exc

        thread = threading.Thread(target=call, name='mimo-call', daemon=True)
        thread.start()
        thread.join(timeout + self.grace_s)
        if thread.is_alive():
            raise InferenceTimeout('hard timeout after %ss' % timeout)
        if 'error' in box:
            raise box['error']
        return box['response']

    def _execute(self, job):
        lane = self.lanes[job['lane']]
        backend = self.backends[job['lane']]
        max_tokens = self.conf['max_output_tokens'][job['feature']]
        estimate = math.ceil(len(job['prompt']) / self.conf['chars_per_token']) + max_tokens
        attempt_no, outcome, error = 0, None, None
        while attempt_no <= self.conf['max_retries']:
            attempt_no += 1
            now = self.clock.time()
            day = timeutil.business_date(now, self.tz)
            with self.store.write() as conn:
                allowed = self.budget.reserve(conn, job, attempt_no, estimate, day, now)
                conn.execute('UPDATE inference_job SET attempts = ? WHERE job_id = ?', (attempt_no, job['job_id']))
            if not allowed:
                return self._finish(job, 'SKIPPED_BUDGET', 'BUDGET_BAND_EXCEEDED')
            started = self.clock.monotonic()
            response, error = None, None
            try:
                response = self._invoke(backend, lane['model'], job['prompt'], max_tokens, job['hard_timeout_s'])
                outcome = 'OK'
            except InferenceTimeout:
                outcome, error = 'TIMEOUT', 'TIMEOUT'
            except (socket.timeout, TimeoutError):
                outcome, error = 'TIMEOUT', 'TIMEOUT'
            except Exception as exc:
                outcome, error = 'ERROR', type(exc).__name__
            latency_ms = (self.clock.monotonic() - started) * 1000
            if outcome == 'OK':
                return self._succeed(job, lane, attempt_no, response, latency_ms, estimate, day)
            with self.store.write() as conn:
                # A timeout may still have consumed tokens upstream: charge the estimate.
                TokenBudget.settle(conn, job['job_id'], attempt_no, estimate if outcome == 'TIMEOUT' else 0)
                self._record_attempt(conn, job, attempt_no, day, now, latency_ms, outcome, error=error)
                opened = self.circuit.failure(conn, job['lane'], self.clock.time(), self.tz)
            if opened:
                break
        return self._finish(job, 'TIMEOUT' if outcome == 'TIMEOUT' else 'FAILED', error)

    def _succeed(self, job, lane, attempt_no, response, latency_ms, estimate, day):
        now = self.clock.time()
        content, dropped = sanitize_output(parse_model_text(response.text))
        reported = response.reported_model_name or lane['model']
        total = response.total_tokens
        if total is None and response.prompt_tokens is not None:
            total = response.prompt_tokens + (response.completion_tokens or 0)
        with self.store.write() as conn:
            TokenBudget.settle(conn, job['job_id'], attempt_no, total if total is not None else estimate)
            self._record_attempt(conn, job, attempt_no, day, now, latency_ms, 'OK', response=response, total=total,
                                 dropped=dropped)
            prior = self.store.model_info(job['lane'], conn) or {}
            conn.execute('INSERT OR REPLACE INTO model_registry VALUES (?, ?, ?, ?, ?)',
                         (job['lane'], lane['model'], reported,
                          response.fingerprint or prior.get('build_fingerprint') or lane['build_fingerprint'], now))
            components = json.loads(job['components'])
            if components['reported_model_name'] != reported:
                components['reported_model_name'] = reported
            key = cache_key(components)
            output = {
                'kind': contract.MIMO_OUTPUT, 'auto_execution': contract.AUTO_EXECUTION, 'lane': job['lane'],
                'role': lane['role'], 'reported_model_name': reported, 'prompt_version': components['prompt_version'],
                'content': content, 'dropped_fields': dropped, 'generated_at': timeutil.iso(int(now), self.tz),
            }
            ttl = self.conf['cache_ttl_hours'][job['feature']] * 3600
            conn.execute('INSERT OR REPLACE INTO inference_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)',
                         (key, job['lane'], job['feature'], job['entity'], job['date_window'], json.dumps(components, sort_keys=True),
                          json.dumps(output, ensure_ascii=False), now, now + ttl))
            conn.execute('INSERT OR REPLACE INTO inference_cache_index VALUES (?, ?, ?, ?, ?, ?)',
                         (job['lane'], job['feature'], job['entity'], job['date_window'], key, json.dumps(components, sort_keys=True)))
            conn.execute("UPDATE inference_job SET status = 'DONE', finished_ts = ?, cache_key = ?, components = ? "
                         'WHERE job_id = ?', (now, key, json.dumps(components, sort_keys=True), job['job_id']))
            self.circuit.success(conn, job['lane'], now, self.tz)
        return {'job_id': job['job_id'], 'lane': job['lane'], 'status': 'DONE', 'latency_ms': latency_ms,
                'total_tokens': total, 'dropped_fields': dropped}

    def _record_attempt(self, conn, job, attempt_no, day, started_ts, latency_ms, outcome, error=None, response=None,
                        total=None, dropped=None):
        conn.execute('INSERT INTO inference_attempt VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (
            job['job_id'], attempt_no, job['lane'], day, started_ts, latency_ms, outcome,
            getattr(response, 'prompt_tokens', None), getattr(response, 'completion_tokens', None), total,
            getattr(response, 'reported_model_name', None), error, json.dumps(dropped) if dropped is not None else None))

    def _finish(self, job, status, error):
        now = self.clock.time()
        with self.store.write() as conn:
            conn.execute('UPDATE inference_job SET status = ?, error_code = ?, finished_ts = ? WHERE job_id = ?',
                         (status, error, now, job['job_id']))
            if status != 'DONE':
                conn.execute("UPDATE circuit_state SET probe_in_flight = 0 WHERE lane = ? AND state = 'HALF_OPEN'", (job['lane'],))
        return {'job_id': job['job_id'], 'lane': job['lane'], 'status': status, 'error': error}


# -- backends -----------------------------------------------------------------

class OpenAICompatibleBackend:
    """Minimal chat-completions client; the API key is read from an environment variable."""

    def __init__(self, base_url, api_key_env):
        self.base_url, self.api_key_env = base_url.rstrip('/'), api_key_env

    def complete(self, prompt, *, model, max_tokens, timeout):
        import os
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError('missing API key in $%s' % self.api_key_env)
        body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                           'max_tokens': max_tokens, 'temperature': 0.2}).encode('utf-8')
        request = urllib.request.Request(self.base_url + '/chat/completions', data=body, method='POST', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise InferenceTimeout(str(exc.reason))
            raise
        usage = data.get('usage') or {}
        return BackendResponse(text=data['choices'][0]['message']['content'], reported_model_name=data.get('model'),
                               prompt_tokens=usage.get('prompt_tokens'), completion_tokens=usage.get('completion_tokens'),
                               total_tokens=usage.get('total_tokens'), fingerprint=data.get('system_fingerprint'))


def backends_from_config(cfg):
    """Backends for lanes configured with backend.type = openai_compatible."""
    out = {}
    for name, lane in cfg.lanes.items():
        conf = lane['backend']
        if lane['enabled'] and conf.get('type') == 'openai_compatible':
            out[name] = OpenAICompatibleBackend(conf['base_url'], conf['api_key_env'])
    return out


def promotion_readiness(store, lane):
    """Phase 1 input: why a challenger lane may not yet replace production."""
    info = store.model_info(lane['name']) or {}
    blocking = []
    if lane['role'] != 'CHALLENGER':
        blocking.append('NOT_A_CHALLENGER')
    if not lane['production_validation']:
        blocking.append('DASHBOARD_PRODUCTION_VALIDATION_MISSING')
    if not (info.get('build_fingerprint') or lane['build_fingerprint']):
        blocking.append('BUILD_FINGERPRINT_EMPTY')
    if not info.get('reported_model_name'):
        blocking.append('NO_SUCCESSFUL_SHADOW_CALL')
    return {'lane': lane['name'], 'ready': not blocking, 'blocking': blocking,
            'reported_model_name': info.get('reported_model_name'), 'build_fingerprint': info.get('build_fingerprint')}
