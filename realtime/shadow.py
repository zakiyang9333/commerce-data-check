"""Daily shadow report for the Phase 0 observation period (at least 14 days)."""
import json
import statistics
from pathlib import Path

from . import timeutil
from .payload import build_realtime_payload, settled_overwrites

COLLECTION_GRACE_S = 120


def _pct(values, pct):
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(sorted(values), n=100, method='inclusive')[pct - 1], 1)


def _lag_samples(store, catalog, schedule, day, until_ts, grace):
    """Viewer data age (minutes) right after each expected slot should have landed.

    For every slot, data_as_of is the oldest latest-good event time across required series
    as known at slot + grace. A failed or late slot therefore shows up as a larger lag.
    """
    series = [(s, m, e) for s in catalog.sources() for m, e in catalog.required_series(s)]
    history = {}
    with store.read() as conn:
        for key in series:
            history[key] = conn.execute("SELECT event_ts, ingest_ts FROM observation WHERE source = ? AND metric = ? "
                                        "AND entity = ? AND business_date = ? AND quality = 'GOOD' ORDER BY ingest_ts",
                                        key + (day,)).fetchall()
    samples, missing = [], 0
    for slot in schedule.expected_slots(day, until_ts):
        at = slot + grace
        if until_ts is not None and at > until_ts:
            break
        latest = []
        for key in series:
            seen = [row['event_ts'] for row in history[key] if row['ingest_ts'] <= at]
            if not seen:
                break
            latest.append(max(seen))
        if series and len(latest) == len(series):
            samples.append(max(at - min(latest), 0) / 60)
        else:
            missing += 1
    return samples, missing


def daily_report(cfg, store, schedule, day, *, inference_store=None, until_ts=None, settled=None,
                 grace=COLLECTION_GRACE_S):
    """One day of shadow metrics. settled: {(metric, entity): value} once the T+1 day has settled."""
    slots = schedule.expected_slots(day, until_ts)
    attempts = store.attempts_for_day(day)
    sources, slot_set = {}, set(slots)
    for source in cfg.catalog.sources():
        rows = [a for a in attempts if a['source'] == source and a['slot_ts'] in slot_set]
        by_slot = {}
        for row in rows:
            by_slot.setdefault(row['slot_ts'], []).append(row)
        success = sum(1 for s in slots if any(a['status'] == 'SUCCESS' for a in by_slot.get(s, [])))
        sources[source] = {'expected_slots': len(slots), 'attempted_slots': len(by_slot), 'success_slots': success,
                           'success_rate': round(success / len(slots), 4) if slots else None}
    samples, missing = _lag_samples(store, cfg.catalog, schedule, day, until_ts, grace)
    with store.read() as conn:
        evals = {r['outcome']: r['n'] for r in conn.execute(
            'SELECT outcome, SUM(n) AS n FROM signal_eval_count WHERE business_date = ? GROUP BY outcome', (day,))}
        opens = conn.execute("SELECT COUNT(*) FROM signal_event WHERE business_date = ? AND transition = 'OPEN'",
                             (day,)).fetchone()[0]
        reviews = {r['verdict']: r['n'] for r in conn.execute(
            'SELECT verdict, COUNT(*) AS n FROM signal_review r JOIN (SELECT DISTINCT signal_id FROM signal_event '
            'WHERE business_date = ?) e ON e.signal_id = r.signal_id GROUP BY verdict', (day,))}
        signal_ids = conn.execute('SELECT COUNT(DISTINCT signal_id) FROM signal_event WHERE business_date = ?',
                                  (day,)).fetchone()[0]
        refresh = conn.execute('SELECT COUNT(*), COALESCE(SUM(blocked_by_inference), 0), '
                               "COALESCE(SUM(fact_refresh != 'PASS'), 0) FROM refresh_run WHERE business_date = ?",
                               (day,)).fetchone()
    expected = sum(v['expected_slots'] for v in sources.values())
    success = sum(v['success_slots'] for v in sources.values())
    report = {
        'business_date': day,
        'expected_slots': expected,
        'attempted_slots': sum(v['attempted_slots'] for v in sources.values()),
        'success_slots': success,
        'success_rate': round(success / expected, 4) if expected else None,
        'p50_lag': _pct(samples, 50),
        'p95_lag': _pct(samples, 95),
        'lag_unit': 'minutes',
        'no_data_slots': missing,
        'browser_login_failure_n': sum(1 for a in attempts if a['error_code'] == 'AUTH_REQUIRED'),
        'browser_contention_n': sum(1 for a in attempts if a['error_code'] == 'BROWSER_CONTENTION'),
        'signal_open_n': opens,
        'signal_false_positive_review': {
            'signals': signal_ids, 'reviewed': sum(reviews.values()),
            'false_positive': reviews.get('FALSE_POSITIVE', 0), 'true_positive': reviews.get('TRUE_POSITIVE', 0),
            'unsure': reviews.get('UNSURE', 0)},
        'insufficient_history_n': evals.get('INSUFFICIENT_HISTORY', 0),
        'signal_evaluations': evals,
        'refresh_runs': refresh[0],
        'fact_refresh_blocked_by_mimo_n': refresh[1],
        'fact_refresh_fail_n': refresh[2],
        'failed_snapshot_erased_good_n': sum(a['erased_good_n'] for a in attempts),
        'settled_fact_overwrite_n': 0,
        'settled_check': 'STRUCTURAL',
        'sources': sources,
        '_lag_samples': samples,
    }
    if settled:
        payload = build_realtime_payload(store, cfg.catalog, cfg.tz, until_ts or timeutil.day_start(day, cfg.tz) + 86400,
                                         business_day=day, settled=settled)
        report['settled_fact_overwrite_n'] = settled_overwrites(payload['metrics'], settled)
        report['settled_check'] = 'PAYLOAD_MERGE'
    if inference_store is not None:
        report.update(inference_store.daily_metrics(day))
    return report


def summarize(reports, inference_store=None, days=None):
    """Pool daily reports into the Phase 0 summary numbers."""
    expected = sum(r['expected_slots'] for r in reports)
    success = sum(r['success_slots'] for r in reports)
    lags = [x for r in reports for x in r.get('_lag_samples', [])]
    out = {
        'days': len(reports),
        'realtime_success_rate': round(success / expected, 4) if expected else None,
        'realtime_p50_lag': _pct(lags, 50),
        'realtime_p95_lag': _pct(lags, 95),
        'fact_refresh_blocked_by_mimo_n': sum(r['fact_refresh_blocked_by_mimo_n'] for r in reports),
        'settled_fact_overwrite_n': sum(r['settled_fact_overwrite_n'] for r in reports),
        'failed_snapshot_erased_good_n': sum(r['failed_snapshot_erased_good_n'] for r in reports),
        'browser_login_failure_n': sum(r['browser_login_failure_n'] for r in reports),
        'browser_contention_n': sum(r['browser_contention_n'] for r in reports),
        'signal_open_n': sum(r['signal_open_n'] for r in reports),
        'insufficient_history_n': sum(r['insufficient_history_n'] for r in reports),
    }
    if inference_store is not None and reports:
        with inference_store.read() as conn:
            marks = ','.join('?' * len(reports))
            day_args = [r['business_date'] for r in reports]
            latencies = [row[0] for row in conn.execute(
                'SELECT latency_ms FROM inference_attempt WHERE business_date IN (%s)' % marks, day_args)]
            jobs = conn.execute("SELECT COUNT(*), COALESCE(SUM(cache_hit), 0) FROM inference_job WHERE business_date IN (%s) "
                                "AND status != 'SUPERSEDED'" % marks, day_args).fetchone()
        out.update({
            'mimo_calls': len(latencies),
            'mimo_cache_hit_rate': round(jobs[1] / jobs[0], 4) if jobs[0] else None,
            'mimo_p50_latency_ms': _pct(latencies, 50),
            'mimo_p95_latency_ms': _pct(latencies, 95),
            'mimo_tokens': sum(r.get('mimo_token_n', 0) for r in reports),
            'mimo_timeout_n': sum(r.get('mimo_timeout_n', 0) for r in reports),
            'mimo_circuit_open_n': sum(r.get('mimo_circuit_open_n', 0) for r in reports),
            'mimo_budget_skip_n': sum(r.get('mimo_budget_skip_n', 0) for r in reports),
        })
    return out


def public(report):
    return {k: v for k, v in report.items() if not k.startswith('_')}


def append_log(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(public(report), ensure_ascii=False, sort_keys=True) + '\n')
