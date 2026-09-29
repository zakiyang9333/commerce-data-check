import json
import tempfile
import time
import unittest

from realtime import contract
from realtime.inference import InferenceQueue, InferenceRequest, InferenceStore
from realtime.payload import apply_settled, build_realtime_payload, settled_overwrites
from realtime.refresh import Step, run_chain
from realtime.store import RealtimeFactStore
from realtime_support import DAY, TZ, at, clock_at, make_config, shop_obs


def batch_request():
    return [InferenceRequest('settled_daily_batch', 'SETTLED_DAILY_BATCH', 'store', '2026-09-28',
                             {'facts': [{'metric': 'gmv', 'value': 1.0, 'basis': 'SETTLED', 'verified': True}]})]


class RefreshChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(self.tmp.name)
        self.clock = clock_at('09:00')
        self.store = RealtimeFactStore.from_config(self.cfg)
        self.istore = InferenceStore(self.cfg.path('inference_store'))
        self.queue = InferenceQueue.from_config(self.cfg, self.istore, self.clock)
        self.order = []

    def tearDown(self):
        self.store.close()
        self.istore.close()
        self.tmp.cleanup()

    def step(self, name, kind='fact'):
        return Step(name, kind, lambda: self.order.append(name))

    def run_chain(self, steps, queue='default'):
        return run_chain(steps, queue=self.queue if queue == 'default' else queue, store=self.store, tz=TZ, clock=self.clock)

    def test_facts_complete_before_and_without_waiting_for_mimo(self):
        def precompute():
            self.order.append('mimo')
            return batch_request()

        # The old chain ran MiMo precompute in the middle; later fact steps must not wait for it.
        started = time.monotonic()
        run = self.run_chain([self.step('collect'), Step('mimo', 'inference', precompute), self.step('facts'),
                              self.step('materialize', 'materialize')])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.order, ['collect', 'facts', 'materialize', 'mimo'])
        self.assertEqual((run['fact_refresh'], run['dashboard_facts'], run['inference_status']), ('PASS', 'AVAILABLE', 'PENDING'))
        self.assertEqual((run['blocked_by_inference'], run['update_complete']), (0, True))
        labels = [row['label'] for row in run['progress']]
        self.assertEqual(labels[-2:], ['更新完成', contract.LABEL_AI_RUNNING])
        self.assertFalse(run['progress'][-1]['blocking'])
        with self.istore.read() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inference_job WHERE status = 'PENDING'").fetchone()[0], 2)
        with self.store.read() as conn:
            self.assertEqual(conn.execute('SELECT blocked_by_inference FROM refresh_run').fetchone()[0], 0)

    def test_model_side_failures_never_fail_the_refresh(self):
        def broken():
            raise RuntimeError('queue down')

        for steps, queue in (([self.step('facts'), Step('mimo', 'inference', broken)], 'default'),
                             ([self.step('facts'), Step('mimo', 'inference', batch_request)], None)):
            run = self.run_chain(steps, queue=queue)
            self.assertEqual((run['fact_refresh'], run['dashboard_facts'], run['inference_status']), ('PASS', 'AVAILABLE', 'FAILED'))
            self.assertTrue(run['update_complete'])

    def test_degraded_step_is_reported_without_failing(self):
        run = self.run_chain([Step('实时补采', 'fact', lambda: {'state': 'FAILED'}), self.step('facts')])
        self.assertEqual((run['fact_refresh'], run['steps'][0]['state']), ('PASS', 'FAILED'))

    def test_fact_failure_keeps_previous_dashboard_and_skips_mimo(self):
        def broken():
            raise ValueError('source file missing')

        run = self.run_chain([Step('facts', 'fact', broken), Step('mimo', 'inference', batch_request)])
        self.assertEqual((run['fact_refresh'], run['dashboard_facts'], run['inference_status']), ('FAIL', 'PREVIOUS', 'NOT_REQUESTED'))
        self.assertEqual(run['progress'][-1]['label'], '更新失败')


class Payload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(self.tmp.name)
        self.store = RealtimeFactStore.from_config(self.cfg)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, source, ts, observations=(), failure=None):
        self.store.record_attempt(source, ts, started_ts=ts, finished_ts=ts + 30, observations=observations, failure=failure)

    def test_fields_labels_and_latest_good_after_failure(self):
        self.record('shop', at('14:30'), shop_obs(at('14:30') + 60, gmv=52000))
        self.record('ads', at('14:30'), [{'metric': 'ad_spend', 'entity': 'account', 'value': 800, 'event_time': at('14:31')},
                                         {'metric': 'ad_gmv', 'entity': 'account', 'value': 3000, 'event_time': at('14:31')}])
        self.record('shop', at('14:35'), failure=('FAILED', 'ADAPTER_ERROR:RuntimeError'))
        payload = build_realtime_payload(self.store, self.cfg.catalog, TZ, at('14:35') + 180)
        for key in ('data_as_of', 'last_success_at', 'lag_minutes', 'completeness', 'provisional', 'source_status'):
            self.assertIn(key, payload)
        self.assertEqual((payload['provisional'], payload['settled'], payload['completeness']), (True, False, 1.0))
        self.assertEqual(payload['latest_attempt_status'], 'FAILED')
        self.assertEqual(payload['source_status']['shop']['latest_attempt_status'], 'FAILED')
        self.assertEqual(payload['source_status']['shop']['last_success_at'], '2026-09-29T14:30:30+08:00')
        self.assertEqual(payload['data_as_of'], '2026-09-29T14:31:00+08:00')
        self.assertEqual(payload['lag_minutes'], 7)
        self.assertEqual(payload['display'], {'title': '今日近实时', 'updated_to': '更新至 14:31', 'lag': '延迟约 7 分钟',
                                              'settlement': '未结算'})
        gmv = next(m for m in payload['metrics'] if m['metric'] == 'gmv')
        self.assertEqual((gmv['value'], gmv['basis'], gmv['provisional']), ('52000', 'REALTIME_ESTIMATE', True))
        self.assertNotIn('实时采集中', json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload['realtime_ui_enabled'])

    def test_empty_day_is_honest(self):
        payload = build_realtime_payload(self.store, self.cfg.catalog, TZ, at('09:00'))
        self.assertEqual((payload['data_as_of'], payload['lag_minutes'], payload['stale']), (None, None, True))
        self.assertEqual(payload['display']['updated_to'], '暂无今日近实时数据')

    def test_settled_facts_always_win(self):
        self.record('shop', at('23:55'), shop_obs(at('23:55'), gmv=99000))
        settled = {('gmv', 'store'): '101234.5'}
        payload = build_realtime_payload(self.store, self.cfg.catalog, TZ, at('23:59'), settled=settled)
        gmv = next(m for m in payload['metrics'] if m['metric'] == 'gmv')
        self.assertEqual((gmv['value'], gmv['basis'], gmv['provisional'], gmv['realtime_estimate']),
                         ('101234.5', 'SETTLED', False, '99000'))
        self.assertEqual(settled_overwrites(payload['metrics'], settled), 0)
        self.assertEqual((payload['settled'], payload['provisional']), (False, True))
        everything = {(m['metric'], m['entity']): '1' for m in payload['metrics']}
        closed = build_realtime_payload(self.store, self.cfg.catalog, TZ, at('23:59'), settled=everything)
        self.assertEqual((closed['settled'], closed['provisional'], closed['display']['settlement']), (True, False, '已结算'))
        self.assertEqual(settled_overwrites([dict(gmv, value='99000')], settled), 1)
        self.assertEqual(apply_settled([], settled), [])

    def test_page_load_never_enqueues_inference(self):
        istore = InferenceStore(self.cfg.path('inference_store'))
        queue = InferenceQueue.from_config(self.cfg, istore, clock_at('10:00'))
        view = queue.view('production', 'settled_daily_batch', 'store', DAY)
        build_realtime_payload(self.store, self.cfg.catalog, TZ, at('10:00'), inference=view)
        with istore.read() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM inference_job').fetchone()[0], 0)
        self.assertEqual(view['label'], contract.LABEL_AI_PENDING)
        istore.close()


if __name__ == '__main__':
    unittest.main()
