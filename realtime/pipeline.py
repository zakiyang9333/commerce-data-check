"""One realtime tick, in contract order:

collection -> completeness/freshness -> fact -> deterministic signal -> inference queue
-> inference cache -> dashboard materialization.

Signals only enqueue model work; materialization reads whatever inference is already
cached. Nothing here waits for a model, and a queue failure does not stop the tick.
"""
import time

from . import timeutil
from .inference import InferenceRequest
from .payload import build_realtime_payload, materialize, signal_facts
from .refresh import Step


def signal_request(event, facts):
    signal = {k: event[k] for k in ('rule', 'entity', 'transition', 'state', 'observed', 'baseline', 'delta', 'event_time',
                                    'evidence_n', 'completeness', 'freshness_minutes', 'provisional', 'basis',
                                    'window_minutes', 'mode')}
    return InferenceRequest(feature='signal_explanation', trigger='SIGNAL_' + event['transition'],
                            entity='%s:%s' % (event['rule'], event['entity']), date_window=event['business_date'],
                            signal_state=event['state'], payload={'signals': [signal], 'facts': facts})


class RealtimePipeline:
    def __init__(self, cfg, store, collector, engine, queue, schedule, *, browser=None, payload_path=None,
                 ui_lane=None, clock=time.time):
        self.cfg, self.store, self.collector, self.engine = cfg, store, collector, engine
        self.queue, self.schedule, self.browser = queue, schedule, browser
        self.payload_path, self.ui_lane, self.clock = payload_path, ui_lane, clock
        self.rules = {rule['rule']: rule for rule in cfg.rules}

    def tick(self, slot_ts=None):
        slot = slot_ts if slot_ts is not None else self.schedule.slot_at(int(self.clock()))
        attempts = self.collector.run_slot(slot)
        if self.browser is not None and any((a['error_code'] or '').startswith('BROWSER_') for a in attempts):
            self.browser.ensure_running()
        now = int(self.clock())
        events = self.engine.evaluate(now)
        queued, queue_errors = [], 0
        for event in events:
            try:
                facts = signal_facts(self.store, self.rules[event['rule']], event['entity'], event['business_date'], self.cfg.tz)
                queued.extend(self.queue.submit(signal_request(event, facts)))
            except Exception:
                queue_errors += 1
        payload = self.materialize(now)
        return {'slot_ts': slot, 'attempts': attempts, 'signal_events': events, 'inference': queued,
                'inference_queue_errors': queue_errors, 'payload': payload}

    def inference_view(self, feature='settled_daily_batch', entity='store', date_window=None, now=None):
        if self.ui_lane is None or self.queue is None:
            return None
        day = date_window or timeutil.business_date(int(now or self.clock()), self.cfg.tz)
        try:
            return self.queue.view(self.ui_lane, feature, entity, day)
        except Exception:
            return None

    def materialize(self, now=None):
        now = int(now if now is not None else self.clock())
        payload = build_realtime_payload(self.store, self.cfg.catalog, self.cfg.tz, now,
                                         inference=self.inference_view(now=now),
                                         stale_after_minutes=self.cfg['signals']['max_lag_minutes'])
        if self.payload_path is not None:
            materialize(self.payload_path, payload)
        return payload

    def catch_up(self):
        """'更新最新数据': collect realtime now. A failed source keeps its latest good values, so the
        step reports PARTIAL/FAILED for the progress sheet instead of failing the refresh."""
        result = self.tick(slot_ts=self.schedule.slot_at(int(self.clock())))
        statuses = [a['status'] for a in result['attempts']]
        if statuses and all(s == 'SUCCESS' for s in statuses):
            state = 'DONE'
        else:
            state = 'PARTIAL' if any(s in ('SUCCESS', 'PARTIAL') for s in statuses) else 'FAILED'
        return dict(result, state=state)

    def catch_up_step(self):
        return Step('实时补采', 'fact', self.catch_up)
