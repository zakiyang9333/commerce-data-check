"""Realtime dashboard payload: latest good values with honest freshness, never '实时采集中'.

Building a payload only reads the realtime store and a precomputed inference view; it
never enqueues or calls a model, so a page load cannot trigger MiMo.
"""
from decimal import Decimal

from . import contract, timeutil
from .browser import write_json_atomic
from .store import decimal_text, parse_decimal

SCHEMA = 'REALTIME_PAYLOAD_V1'
_ATTEMPT_SEVERITY = {'SUCCESS': 0, 'PARTIAL': 1, 'SKIPPED': 2, 'FAILED': 3}


def apply_settled(metrics, settled):
    """Settled facts always win; a realtime value is kept only as a reference estimate."""
    out = []
    for metric in metrics:
        key = (metric['metric'], metric['entity'])
        if key in settled:
            out.append(dict(metric, value=decimal_text(parse_decimal(settled[key])), basis=contract.SETTLED_BASIS,
                            provisional=False, realtime_estimate=metric['value']))
        else:
            out.append(metric)
    return out


def settled_overwrites(metrics, settled):
    """Count metrics whose displayed value differs from a settled fact (must stay 0)."""
    return sum(1 for m in metrics if (m['metric'], m['entity']) in settled
               and parse_decimal(m['value']) != parse_decimal(settled[(m['metric'], m['entity'])]))


def build_realtime_payload(store, catalog, tz, now_ts, *, business_day=None, settled=None, inference=None,
                           stale_after_minutes=20):
    day = business_day or timeutil.business_date(now_ts, tz)
    snapshot = store.good_snapshot(day)
    sources, times, expected, present = {}, [], 0, 0
    for source in catalog.sources():
        attempt, success = store.latest_attempt(source), store.latest_success(source)
        required = catalog.required_series(source)
        good = [snapshot[(source, m, e)] for m, e in required if (source, m, e) in snapshot]
        expected, present = expected + len(required), present + len(good)
        times += [g['event_ts'] for g in good]
        sources[source] = {
            'latest_attempt_status': attempt['status'] if attempt else None,
            'latest_attempt_error': attempt['error_code'] if attempt else None,
            'latest_attempt_at': timeutil.iso(attempt['finished_ts'], tz) if attempt else None,
            'last_success_at': timeutil.iso(success['finished_ts'], tz) if success else None,
            'data_as_of': timeutil.iso(min(g['event_ts'] for g in good), tz) if good else None,
            'completeness': round(len(good) / len(required), 4) if required else None,
        }
    as_of = min(times) if times else None
    lag = round((now_ts - as_of) / 60) if as_of is not None else None
    metrics = [{
        'source': s, 'metric': m, 'entity': e, 'value': obs['value'], 'unit': obs['unit'], 'definition': obs['definition'],
        'event_time': timeutil.iso(obs['event_ts'], tz), 'basis': contract.REALTIME_BASIS, 'provisional': True,
    } for (s, m, e), obs in sorted(snapshot.items())]
    if settled:
        metrics = apply_settled(metrics, settled)
    attempts = [v['latest_attempt_status'] for v in sources.values() if v['latest_attempt_status']]
    successes = [v['last_success_at'] for v in sources.values()]
    all_settled = bool(metrics) and all(m['basis'] == contract.SETTLED_BASIS for m in metrics)
    display = {'title': contract.LABEL_MODE, 'settlement': contract.LABEL_SETTLED if all_settled else contract.LABEL_UNSETTLED,
               'updated_to': '更新至 %s' % timeutil.hhmm(as_of, tz) if as_of is not None else '暂无今日近实时数据',
               'lag': '延迟约 %d 分钟' % lag if lag is not None else None}
    return {
        'schema': SCHEMA,
        'mode': 'NEAR_REALTIME',
        'business_date': day,
        'generated_at': timeutil.iso(int(now_ts), tz),
        'data_as_of': timeutil.iso(as_of, tz),
        'last_success_at': None if None in successes or not successes else min(successes),
        'latest_attempt_status': max(attempts, key=_ATTEMPT_SEVERITY.get) if attempts else None,
        'lag_minutes': lag,
        'completeness': round(present / expected, 4) if expected else None,
        'provisional': not all_settled,
        'settled': all_settled,
        'stale': lag is None or lag > stale_after_minutes,
        'source_status': sources,
        'metrics': metrics,
        'display': display,
        'inference': inference or {'status': 'NOT_REQUESTED', 'label': contract.LABEL_AI_PENDING, 'progress_label': None},
        'realtime_ui_enabled': contract.REALTIME_UI_ENABLED,
    }


def materialize(path, payload):
    write_json_atomic(path, payload)
    return path


def signal_facts(store, rule, entity, business_day, tz):
    """Verified realtime facts behind one signal, in the shape the inference guard accepts."""
    parts = [rule['numerator'], rule['denominator']] if rule['kind'] == 'ratio' else [rule['series']]
    facts = []
    for series in parts:
        obs = store.latest_good(series['source'], series['metric'], series['entity'] or entity, business_day)
        if obs is not None:
            facts.append({'metric': obs['metric'], 'entity': obs['entity'], 'value': float(Decimal(obs['value'])),
                          'unit': obs['unit'], 'definition': obs['definition'], 'event_time': timeutil.iso(obs['event_ts'], tz),
                          'basis': contract.REALTIME_BASIS, 'provisional': True, 'verified': True})
    return facts
