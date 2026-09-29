"""Phase 0 acceptance on a short synthetic shadow run (SIMULATED evidence, not production)."""
import tempfile
import unittest

from realtime import contract, simulate


class Phase0Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.summary, cls.reports, cls.checks = simulate.run(cls.tmp.name, days=3, seed=11)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_contract_is_frozen(self):
        self.assertTrue(contract.FACT_REFRESH_NOT_BLOCKED_BY_MIMO)
        self.assertTrue(contract.MIMO_FAILURE_DOES_NOT_FAIL_DATA_REFRESH)
        self.assertEqual((contract.MIMO_OUTPUT, contract.AUTO_EXECUTION), ('MODEL_INFERENCE', 0))
        self.assertFalse(contract.PRODUCTION_ALERTS_ENABLED)
        self.assertFalse(contract.REALTIME_UI_ENABLED)
        self.assertTrue({'gmv', 'roi', 'fee_rate', 'freshness', 'anomaly_threshold', 'stop_decision',
                         'budget_execution'} <= contract.MODEL_FORBIDDEN_FIELDS)

    def test_prohibited_outcomes_never_happen(self):
        self.assertEqual(self.summary['fact_refresh_blocked_by_mimo_n'], 0)
        self.assertEqual(self.summary['settled_fact_overwrite_n'], 0)
        self.assertEqual(self.summary['failed_snapshot_erased_good_n'], 0)
        self.assertEqual(self.checks['page_load_mimo_jobs'], 0)
        self.assertGreater(self.checks['page_loads'], 0)
        self.assertEqual(self.checks['settled_store_guard'], 'REFUSED')
        self.assertEqual(self.checks['settled_rows_changed_by_realtime'], 0)
        self.assertFalse(self.checks['retired_label_used'])

    def test_model_outage_is_isolated(self):
        self.assertGreaterEqual(self.summary['mimo_circuit_open_n'], 1)
        self.assertTrue(all(r['fact_refresh_fail_n'] == 0 for r in self.reports))

    def test_daily_report_has_every_shadow_field(self):
        fields = ('expected_slots', 'attempted_slots', 'success_slots', 'success_rate', 'p50_lag', 'p95_lag',
                  'browser_login_failure_n', 'browser_contention_n', 'signal_open_n', 'signal_false_positive_review',
                  'insufficient_history_n', 'mimo_call_n', 'mimo_cache_hit_rate', 'mimo_p50_latency_ms',
                  'mimo_p95_latency_ms', 'mimo_timeout_n', 'mimo_token_n', 'mimo_circuit_open_n')
        for report in self.reports:
            for field in fields:
                self.assertIn(field, report)
        self.assertEqual(self.reports[0]['expected_slots'], 2 * 208)
        self.assertTrue(0 < self.summary['realtime_success_rate'] <= 1)
        self.assertEqual(self.summary['evidence'], 'SIMULATED')

    def test_challenger_is_not_promotable_yet(self):
        readiness = self.checks['shadow_lane_readiness']
        self.assertFalse(readiness['ready'])
        self.assertIn('BUILD_FINGERPRINT_EMPTY', readiness['blocking'])


if __name__ == '__main__':
    unittest.main()
