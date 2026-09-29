import tempfile
import unittest

from realtime import config, timeutil
from realtime.schedule import Schedule
from realtime.signals import SignalEngine, events_for_day, review_signal
from realtime.store import RealtimeFactStore
from realtime_support import DAY, TZ, at

RATE = 500  # GMV per 5 minutes in the daytime baseline


def build(tmp, **rule):
    cfg = config.load({
        'sources': {'shop': {'metrics': [
            {'metric': 'gmv', 'entity_type': 'store', 'unit': 'CNY', 'required': True, 'definition': 'd'}]}},
        'signals': {'rules': [dict({'rule': 'GMV_DROP', 'series': {'source': 'shop', 'metric': 'gmv', 'entity': 'store'}},
                                   **rule)]},
    }, base_dir=tmp)
    store = RealtimeFactStore.from_config(cfg)
    return cfg, store, SignalEngine.from_config(cfg, store), Schedule(cfg.windows, TZ)


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg, self.store, self.engine, self.schedule = build(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def day(self, day, until='23:55', factor=lambda hhmm: 1.0, evaluate=False):
        """Write one day of cumulative GMV; factor scales the pace per slot. Returns emitted events."""
        events, total = [], 0.0
        for slot in self.schedule.slots(day):
            hhmm = timeutil.hhmm(slot, TZ)
            if hhmm > until:
                break
            step = self.schedule.interval_at(slot) / 300
            total += (RATE if hhmm >= '08:00' else RATE / 10) * step * factor(hhmm)
            self.store.record_attempt('shop', slot, started_ts=slot, finished_ts=slot + 30, observations=[
                {'metric': 'gmv', 'entity': 'store', 'value': round(total, 2), 'event_time': slot}])
            if evaluate:
                events += self.engine.evaluate(slot + 40)
        return events

    def history(self, days=4):
        for offset in range(days, 0, -1):
            self.day(timeutil.shift_day(DAY, -offset))

    def counts(self):
        with self.store.read() as conn:
            return {r['outcome']: r['n'] for r in conn.execute(
                'SELECT outcome, n FROM signal_eval_count WHERE business_date = ?', (DAY,))}

    def test_insufficient_history_opens_nothing(self):
        self.history(days=2)
        events = self.day(DAY, until='16:00', factor=lambda h: 0.05 if h >= '14:00' else 1.0, evaluate=True)
        self.assertEqual(events, [])
        self.assertGreater(self.counts()['INSUFFICIENT_HISTORY'], 0)

    def test_open_escalate_resolve_with_required_fields(self):
        self.history()
        outage = lambda h: 0.05 if '14:00' <= h < '16:00' else 1.0  # noqa: E731
        events = self.day(DAY, until='18:00', factor=outage, evaluate=True)
        self.assertEqual([e['transition'] for e in events], ['OPEN', 'ESCALATE', 'RESOLVE'])
        self.assertEqual(len({e['signal_id'] for e in events}), 1)
        opened = events[0]
        self.assertGreaterEqual(opened['event_ts'], at('14:40'))
        for event in events:
            for key in ('observed', 'baseline', 'delta', 'event_time', 'evidence_n', 'completeness', 'freshness_minutes',
                        'provisional'):
                self.assertIn(key, event)
            self.assertEqual((event['mode'], event['production_alert'], event['provisional']), ('SHADOW', False, True))
            self.assertEqual(event['evidence_n'], 4)
        self.assertLessEqual(opened['delta'], -0.5)
        self.assertGreaterEqual(events[-1]['delta'], -0.3)

    def test_short_dip_does_not_open(self):
        self.history()
        dip = lambda h: 0.0 if '14:00' <= h < '14:10' else 1.0  # noqa: E731
        self.assertEqual(self.day(DAY, until='16:00', factor=dip, evaluate=True), [])

    def test_re_evaluation_is_deduplicated(self):
        self.history()
        outage = lambda h: 0.05 if h >= '14:00' else 1.0  # noqa: E731
        events = self.day(DAY, until='15:30', factor=outage, evaluate=True)
        self.assertTrue(events)
        self.assertEqual(self.engine.evaluate(at('15:31')), [])
        self.assertEqual(self.engine.evaluate(at('15:31')), [])
        stored = events_for_day(self.store, DAY)
        self.assertEqual(len(stored), len({e['event_key'] for e in stored}))

    def test_cooldown_blocks_reopen(self):
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg, self.store, self.engine, self.schedule = build(self.tmp.name, cooldown_minutes=180)
        self.history()
        flap = lambda h: 0.05 if ('13:00' <= h < '14:00') or ('15:30' <= h < '17:00') else 1.0  # noqa: E731
        events = self.day(DAY, until='18:30', factor=flap, evaluate=True)
        self.assertEqual([e['transition'] for e in events].count('OPEN'), 1)

    def test_stale_data_is_suppressed(self):
        self.history()
        self.day(DAY, until='15:00', factor=lambda h: 0.05 if h >= '14:00' else 1.0)
        self.assertEqual(self.engine.evaluate(at('16:30')), [])
        self.assertEqual(self.counts().get('SUPPRESSED_STALE'), 1)

    def test_review_is_recorded(self):
        self.history()
        events = self.day(DAY, until='15:30', factor=lambda h: 0.05 if h >= '14:00' else 1.0, evaluate=True)
        review_signal(self.store, events[0]['signal_id'], 'FALSE_POSITIVE', at('16:00'), reviewer='analyst')
        with self.store.read() as conn:
            self.assertEqual(conn.execute('SELECT verdict FROM signal_review').fetchone()[0], 'FALSE_POSITIVE')
        with self.assertRaises(KeyError):
            review_signal(self.store, 'unknown', 'UNSURE', at('16:00'))


if __name__ == '__main__':
    unittest.main()
