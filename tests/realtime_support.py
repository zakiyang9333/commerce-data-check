"""Shared synthetic fixtures for the realtime tests."""
import copy
import json

from realtime import config, inference, timeutil
from realtime.simulate import SimClock

DAY = '2026-09-29'
TZ = timeutil.business_tz(8)

SOURCES = {
    'shop': {'uses_dedicated_browser': True, 'metrics': [
        {'metric': 'gmv', 'entity_type': 'store', 'unit': 'CNY', 'required': True, 'definition': 'gmv-def'},
        {'metric': 'orders', 'entity_type': 'store', 'unit': 'count', 'required': True, 'definition': 'orders-def'},
        {'metric': 'visitors', 'entity_type': 'store', 'unit': 'count', 'required': True, 'definition': 'uv-def'},
    ]},
    'ads': {'uses_dedicated_browser': True, 'metrics': [
        {'metric': 'ad_spend', 'entity_type': 'account', 'unit': 'CNY', 'required': True, 'definition': 'spend-def'},
        {'metric': 'ad_gmv', 'entity_type': 'account', 'unit': 'CNY', 'required': True, 'definition': 'adgmv-def'},
        {'metric': 'ad_spend', 'entity_type': 'campaign', 'unit': 'CNY', 'definition': 'cspend-def'},
    ]},
}

LANES = {
    'production': {'model': 'mimo-v2.5-pro', 'role': 'PRODUCTION', 'serve_to_ui': True,
                   'features': ['settled_daily_batch', 'on_demand_why']},
    'MIMO26_REALTIME_SHADOW_V1': {'model': 'mimo-2.6-test', 'role': 'CHALLENGER',
                                  'features': ['signal_explanation', 'settled_daily_batch']},
}


def make_config(base_dir, **overrides):
    raw = {'sources': copy.deepcopy(SOURCES), 'inference': {'lanes': copy.deepcopy(LANES)}}
    for key, value in overrides.items():
        raw[key] = value
    return config.load(raw, base_dir=base_dir)


def at(hhmm, day=DAY):
    return timeutil.day_start(day, TZ) + timeutil.parse_hhmm(hhmm)


def obs(metric, entity, value, event_ts, **extra):
    return dict(extra, metric=metric, entity=entity, value=value, event_time=event_ts)


def shop_obs(ts, gmv=1000, orders=10, visitors=200):
    return [obs('gmv', 'store', gmv, ts), obs('orders', 'store', orders, ts), obs('visitors', 'store', visitors, ts)]


class FakeModel:
    """Scripted backend: each call pops the next behaviour ('ok', 'timeout', 'error', or a latency in seconds)."""

    def __init__(self, clock, script=('ok',), text=None, reported=None, fingerprint=None):
        self.clock, self.script = clock, list(script)
        self.text = text or json.dumps({'summary': '模拟解读', 'hypotheses': [], 'checks': []}, ensure_ascii=False)
        self.reported, self.fingerprint = reported, fingerprint
        self.calls = []

    def complete(self, prompt, *, model, max_tokens, timeout):
        behaviour = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        self.calls.append({'model': model, 'timeout': timeout, 'prompt': prompt})
        if behaviour == 'timeout':
            self.clock.advance(timeout)
            raise inference.InferenceTimeout('slow')
        if behaviour == 'error':
            self.clock.advance(1)
            raise ConnectionError('upstream 502')
        self.clock.advance(5 if behaviour == 'ok' else behaviour)
        return inference.BackendResponse(text=self.text, reported_model_name=self.reported or model,
                                         prompt_tokens=100, completion_tokens=50, total_tokens=150,
                                         fingerprint=self.fingerprint)


def clock_at(hhmm='10:00', day=DAY):
    return SimClock(at(hhmm, day))
