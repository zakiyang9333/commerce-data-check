import copy
import json
import tempfile
import threading
import unittest

from realtime import config, contract, inference
from realtime.inference import InferenceQueue, InferenceRequest, InferenceStore, InferenceWorker
from realtime_support import DAY, LANES, SOURCES, FakeModel, clock_at

PROD, SHADOW = 'production', 'MIMO26_REALTIME_SHADOW_V1'


def fact(value, **extra):
    return dict({'metric': 'gmv', 'entity': 'store', 'value': value, 'basis': 'SETTLED', 'verified': True}, **extra)


def batch(value=52341.0, **extra):
    return InferenceRequest('settled_daily_batch', 'SETTLED_DAILY_BATCH', 'store', '2026-09-28',
                            {'facts': [fact(value, **extra)]}, **{k: v for k, v in extra.items() if k == 'prompt_version'})


class InferenceCase(unittest.TestCase):
    lanes = LANES

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = clock_at('10:00')
        self.configure()

    def configure(self, inference_overrides=None, lanes=None):
        conf = {'lanes': copy.deepcopy(lanes or self.lanes)}
        conf.update(inference_overrides or {})
        self.cfg = config.load({'sources': SOURCES, 'inference': conf}, base_dir=self.tmp.name)
        self.store = InferenceStore(self.cfg.path('inference_store'))
        self.queue = InferenceQueue.from_config(self.cfg, self.store, self.clock)
        self.prod, self.shadow = FakeModel(self.clock), FakeModel(self.clock)
        self.worker = InferenceWorker.from_config(self.cfg, self.store, {PROD: self.prod, SHADOW: self.shadow}, self.clock)
        self.worker.grace_s = 0.5

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def jobs(self, **where):
        clause = ' AND '.join('%s = ?' % k for k in where) or '1 = 1'
        with self.store.read() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM inference_job WHERE ' + clause, tuple(where.values()))]

    def by_lane(self, results):
        return {r['lane']: r for r in results}


class Admission(InferenceCase):
    def test_only_contract_events_enter_the_queue(self):
        for trigger in ('PAGE_LOAD', 'SLOT_TICK', 'EVERY_5_MIN', None):
            result = self.queue.submit(InferenceRequest('on_demand_why', trigger, 'store', DAY, {}))
            self.assertEqual(result[0]['status'], 'NO_MIMO_CALL')
        self.assertEqual(self.jobs(), [])
        with self.store.read() as conn:
            self.assertEqual(conn.execute("SELECT SUM(n) FROM gate_count WHERE reason = 'TRIGGER_NOT_ALLOWED'").fetchone()[0], 4)

    def test_input_guard(self):
        rejected = {
            'UNVERIFIED_FACT': {'facts': [{'metric': 'gmv', 'value': 1, 'basis': 'SETTLED'}]},
            'REALTIME_FACT_NOT_PROVISIONAL': {'facts': [fact(1, basis='REALTIME_ESTIMATE')]},
            'UNPERMITTED_INPUT:raw_orders': {'raw_orders': [{'buyer': 'x'}]},
            'NON_AGGREGATE_INPUT': {'aggregates': {'rows': [1, 2]}},
            'NOT_A_DETERMINISTIC_SIGNAL': {'signals': [{'rule': 'GMV_DROP'}]},
            'UNSANITIZED_CS_EVIDENCE': {'cs_evidence': [{'text': '客户电话13812345678', 'sanitized': True}]},
        }
        for reason, payload in rejected.items():
            with self.subTest(reason):
                result = self.queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, payload))
                self.assertEqual((result[0]['status'], result[0]['reason']), ('NO_MIMO_CALL', reason))
        evidence = inference.sanitized_evidence('客户电话13812345678，邮箱a.b@example.com，订单号 250929123456789')
        self.assertEqual(inference.find_pii(evidence['text']), [])
        ok = self.queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, {'cs_evidence': [evidence]}))
        self.assertEqual(ok[0]['status'], 'PENDING')

    def test_disabled_challenger_lane_is_not_called(self):
        lanes = copy.deepcopy(LANES)
        lanes[SHADOW]['model'] = None
        self.configure(lanes=lanes)
        results = self.by_lane(self.queue.submit(batch()))
        self.assertEqual((results[SHADOW]['status'], results[SHADOW]['reason']), ('NO_MIMO_CALL', 'LANE_DISABLED'))
        self.assertEqual(results[PROD]['status'], 'PENDING')


class Cache(InferenceCase):
    def run_all(self):
        return self.worker.drain()

    def test_unchanged_meaningful_facts_hit_without_calls(self):
        self.queue.submit(batch(52341.0, event_time='2026-09-29T09:00:00+08:00'))
        self.run_all()
        calls = len(self.prod.calls)
        again = self.by_lane(self.queue.submit(batch(52210.0, event_time='2026-09-29T09:05:00+08:00')))
        self.assertEqual((again[PROD]['status'], again[PROD]['cache_hit']), ('CACHED', True))
        self.assertEqual(again[PROD]['output']['kind'], contract.MIMO_OUTPUT)
        self.run_all()
        self.assertEqual(len(self.prod.calls), calls)

    def test_miss_reasons(self):
        self.queue.submit(batch(52341.0))
        self.run_all()
        facts = self.by_lane(self.queue.submit(batch(61000.0)))
        self.assertEqual(facts[PROD]['cache_miss_reason'], 'FACTS_CHANGED')
        self.run_all()
        prompt = self.by_lane(self.queue.submit(InferenceRequest('settled_daily_batch', 'SETTLED_DAILY_BATCH', 'store',
                                                                 '2026-09-28', {'facts': [fact(61000.0)]},
                                                                 prompt='自定义提示', prompt_version='batch-v2')))
        self.assertEqual(prompt[PROD]['cache_miss_reason'], 'PROMPT_VERSION_CHANGED')

    def test_signal_state_change_misses(self):
        signal = {'rule': 'GMV_DROP', 'observed': 800.0, 'baseline': 4000.0, 'delta': -0.8}
        request = lambda state: InferenceRequest('signal_explanation', 'SIGNAL_OPEN', 'GMV_DROP:store', DAY,  # noqa: E731
                                                 {'signals': [signal]}, signal_state=state)
        self.queue.submit(request('OPEN'))
        self.run_all()
        result = self.by_lane(self.queue.submit(request('ESCALATED')))
        self.assertEqual(result[SHADOW]['cache_miss_reason'], 'SIGNAL_STATE_CHANGED')

    def test_reported_model_change_invalidates(self):
        self.queue.submit(batch())
        self.run_all()
        # The provider starts reporting a different model behind the same alias on another request.
        self.prod.reported = 'mimo-v2.5-pro-0930'
        self.queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, {'facts': [fact(1.0)]}))
        self.run_all()
        self.assertEqual(self.store.model_info(PROD)['reported_model_name'], 'mimo-v2.5-pro-0930')
        result = self.by_lane(self.queue.submit(batch()))
        self.assertEqual((result[PROD]['status'], result[PROD]['cache_miss_reason']), ('PENDING', 'MODEL_CHANGED'))

    def test_views_and_labels(self):
        self.assertEqual(self.queue.view(PROD, 'settled_daily_batch', 'store', '2026-09-28')['status'], 'NOT_REQUESTED')
        self.queue.submit(batch())
        pending = self.queue.view(PROD, 'settled_daily_batch', 'store', '2026-09-28')
        self.assertEqual((pending['label'], pending['progress_label']), (contract.LABEL_AI_PENDING, contract.LABEL_AI_RUNNING))
        self.run_all()
        self.assertEqual(self.queue.view(PROD, 'settled_daily_batch', 'store', '2026-09-28')['status'], 'READY')
        self.queue.submit(batch(90000.0))
        stale = self.queue.view(PROD, 'settled_daily_batch', 'store', '2026-09-28')
        self.assertEqual((stale['status'], stale['label']), ('STALE', contract.LABEL_AI_STALE))
        self.assertIn('output', stale)


class Worker(InferenceCase):
    def test_output_is_inference_only(self):
        self.prod.text = json.dumps({'summary': 'x', 'GMV': 1, 'roi': 3, 'stop_decision': 'STOP',
                                     'detail': {'budget_execution': 'cut', 'fee_rate': 0.1}, 'actions': ['pause']})
        self.queue.submit(batch())
        self.worker.drain()
        output = self.queue.view(PROD, 'settled_daily_batch', 'store', '2026-09-28')['output']
        self.assertEqual((output['kind'], output['auto_execution']), ('MODEL_INFERENCE', 0))
        self.assertEqual(output['content'], {'summary': 'x', 'detail': {}})
        self.assertEqual(sorted(output['dropped_fields']),
                         ['GMV', 'actions', 'detail.budget_execution', 'detail.fee_rate', 'roi', 'stop_decision'])
        self.assertEqual(self.store.daily_metrics(DAY)['mimo_ownership_violation_n'], 1)

    def test_timeout_retries_once_then_fails_softly(self):
        self.prod.script = ['timeout']
        self.queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, {'facts': [fact(1.0)]}))
        result = self.worker.drain()
        self.assertEqual(result[0]['status'], 'TIMEOUT')
        self.assertEqual([c['timeout'] for c in self.prod.calls], [60, 60])
        metrics = self.store.daily_metrics(DAY)
        self.assertEqual((metrics['mimo_call_n'], metrics['mimo_timeout_n']), (2, 2))
        self.assertGreater(metrics['mimo_token_n'], 0)
        self.assertEqual(self.queue.view(PROD, 'on_demand_why', 'store', DAY)['label'], contract.LABEL_AI_PENDING)

    def test_batch_uses_longer_hard_timeout(self):
        self.queue.submit(batch())
        self.worker.drain()
        self.assertEqual(self.prod.calls[0]['timeout'], 120)

    def test_hung_backend_is_cut_off(self):
        self.configure({'hard_timeout_seconds': {'signal_explanation': 1, 'on_demand_why': 1,
                                                 'settled_daily_batch': 1, 'background_enrichment': 1},
                        'max_retries': 0})
        release = threading.Event()

        class Hung:
            def complete(self, prompt, **kwargs):
                release.wait(10)

        self.worker.backends[PROD] = Hung()
        self.worker.grace_s = 0.1
        self.queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, {'facts': [fact(1.0)]}))
        self.assertEqual(self.worker.run_once()['status'], 'TIMEOUT')
        release.set()

    def test_circuit_opens_after_three_failures_and_recovers(self):
        self.prod.script = ['error']
        why = lambda v: InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, {'facts': [fact(v)]})  # noqa: E731
        self.queue.submit(why(1.0))
        self.worker.drain()
        self.queue.submit(why(2000.0))
        self.worker.drain()
        self.assertEqual(len(self.prod.calls), 3)
        with self.store.read() as conn:
            self.assertEqual(self.worker.circuit.state(conn, PROD)['state'], 'OPEN')
        self.queue.submit(why(900000.0))
        self.assertEqual(self.worker.drain(), [])
        self.assertEqual(len(self.prod.calls), 3)
        self.prod.script = ['ok']
        self.clock.advance(16 * 60)
        self.assertEqual(self.worker.drain()[0]['status'], 'DONE')
        with self.store.read() as conn:
            self.assertEqual(self.worker.circuit.state(conn, PROD)['state'], 'CLOSED')
        self.assertEqual(self.store.daily_metrics(DAY)['mimo_circuit_open_n'], 1)

    def test_budget_sheds_low_priority_first(self):
        self.configure({'daily_token_budget': 3000})
        self.assertEqual(self.queue.budget.limit(3), 1800)
        with self.store.write() as conn:
            conn.execute('INSERT INTO token_ledger VALUES (?, 1, ?, ?, 2, 2000, 2000, 1, ?)', ('seed', DAY, PROD, self.clock.time()))
        why = self.by_lane(self.queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, {'facts': [fact(1.0)]})))
        self.assertEqual(why[PROD]['status'], 'SKIPPED_BUDGET')
        signal = {'rule': 'GMV_DROP', 'observed': 1.0, 'baseline': 2.0, 'delta': -0.5}
        p0 = self.by_lane(self.queue.submit(InferenceRequest('signal_explanation', 'SIGNAL_OPEN', 'GMV_DROP:store', DAY,
                                                             {'signals': [signal]}, signal_state='OPEN')))
        self.assertEqual(p0[SHADOW]['status'], 'PENDING')
        self.assertEqual(self.store.daily_metrics(DAY)['mimo_budget_skip_n'], 1)

    def test_shadow_lane_never_serves_the_dashboard(self):
        self.shadow.text = json.dumps({'summary': 'shadow answer'})
        self.prod.script = ['error']
        self.queue.submit(batch())
        self.worker.drain()
        self.assertNotEqual(self.queue.view(PROD, 'settled_daily_batch', 'store', '2026-09-28')['status'], 'READY')
        self.assertEqual(self.queue.view(SHADOW, 'settled_daily_batch', 'store', '2026-09-28')['status'], 'READY')

    def test_worker_leaves_other_lanes_to_their_own_worker(self):
        prod_only = InferenceWorker.from_config(self.cfg, self.store, {PROD: self.prod}, self.clock)
        self.queue.submit(batch())
        self.assertEqual([r['lane'] for r in prod_only.drain()], [PROD])
        self.assertEqual(self.jobs(lane=SHADOW)[0]['status'], 'PENDING')
        self.assertEqual([r['lane'] for r in self.worker.drain()], [SHADOW])

    def test_expired_and_lost_jobs(self):
        self.queue.submit(InferenceRequest('on_demand_why', 'USER_WHY', 'store', DAY, {'facts': [fact(1.0)]}))
        self.clock.advance(31 * 60)
        self.assertEqual(self.worker.drain(), [])
        self.assertEqual(self.jobs(lane=PROD)[0]['status'], 'EXPIRED')

    def test_promotion_readiness(self):
        report = inference.promotion_readiness(self.store, self.cfg.lanes[SHADOW])
        self.assertFalse(report['ready'])
        self.assertIn('DASHBOARD_PRODUCTION_VALIDATION_MISSING', report['blocking'])
        self.assertIn('BUILD_FINGERPRINT_EMPTY', report['blocking'])


class Hashing(unittest.TestCase):
    def test_rounded_hash_ignores_timestamps_and_small_moves(self):
        base = {'facts': [fact(52341.0, event_time='a', freshness_minutes=3)], 'signals': [
            {'rule': 'GMV_DROP', 'observed': 800.0, 'baseline': 4000.0, 'delta': -0.8, 'signal_id': 'x|1'}]}
        moved = {'facts': [fact(52499.0, event_time='b', freshness_minutes=9)], 'signals': [
            {'rule': 'GMV_DROP', 'observed': 804.0, 'baseline': 4010.0, 'delta': -0.79, 'signal_id': 'x|2'}]}
        changed = {'facts': [fact(71000.0)], 'signals': base['signals']}
        self.assertEqual(inference.rounded_fact_hash(base), inference.rounded_fact_hash(moved))
        self.assertNotEqual(inference.rounded_fact_hash(base), inference.rounded_fact_hash(changed))


if __name__ == '__main__':
    unittest.main()
