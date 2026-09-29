import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from realtime import config, contract, timeutil
from realtime.schedule import Schedule
from realtime.store import RealtimeFactStore, StoreGuardError
from realtime_support import DAY, TZ, at, make_config, obs, shop_obs

ROOT = Path(__file__).resolve().parents[1]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(self.tmp.name)
        self.store = RealtimeFactStore.from_config(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, ts, observations, failure=None, source='shop'):
        return self.store.record_attempt(source, ts, started_ts=ts, finished_ts=ts + 30,
                                         observations=observations, failure=failure)

    def good(self, metric='gmv', entity='store', source='shop'):
        return self.store.latest_good(source, metric, entity, DAY)


class Isolation(StoreCase):
    def test_wal_and_store_kind(self):
        with self.store.read() as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            kind = conn.execute("SELECT value FROM store_meta WHERE key = 'store_kind'").fetchone()[0]
        self.assertEqual(kind, contract.STORE_KIND)

    def test_refuses_configured_settled_store(self):
        settled = Path(self.tmp.name) / 'settled.db'
        with self.assertRaises(StoreGuardError):
            RealtimeFactStore(settled, self.cfg.catalog, TZ, settled_paths=[settled])
        self.assertFalse(settled.exists())

    def test_refuses_existing_foreign_database_without_touching_it(self):
        foreign = Path(self.tmp.name) / 'forecast.db'
        conn = sqlite3.connect(str(foreign))
        conn.execute('CREATE TABLE daily (d TEXT, gmv REAL)')
        conn.execute("INSERT INTO daily VALUES ('2026-09-28', 1.0)")
        conn.commit()
        conn.close()
        before = foreign.read_bytes()
        with self.assertRaises(StoreGuardError):
            RealtimeFactStore(foreign, self.cfg.catalog, TZ)
        self.assertEqual(foreign.read_bytes(), before)

    def test_rows_are_always_provisional(self):
        ts = at('10:00')
        self.record(ts, shop_obs(ts))
        with self.store.write() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute('UPDATE observation SET provisional = 0')


class Semantics(StoreCase):
    def test_retry_is_idempotent(self):
        ts = at('10:00')
        first, second = self.record(ts, shop_obs(ts)), self.record(ts, shop_obs(ts))
        self.assertEqual((first['inserted'], second['inserted'], second['duplicate']), (3, 0, 3))
        self.assertEqual(second['status'], 'SUCCESS')
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        with self.store.read() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM observation').fetchone()[0], 3)

    def test_same_value_different_format_is_duplicate(self):
        ts = at('10:00')
        self.record(ts, [obs('gmv', 'store', '1000.50', ts)])
        self.assertEqual(self.record(ts, [obs('gmv', 'store', 1000.5, ts)])['duplicate'], 1)

    def test_restatement_adds_revision(self):
        ts = at('10:00')
        self.record(ts, shop_obs(ts, gmv=1000))
        self.record(ts, shop_obs(ts, gmv=1010))
        good = self.good()
        self.assertEqual((good['value'], good['revision'], good['quality_note']), ('1010', 2, 'RESTATED'))

    def test_late_arrival_is_kept_but_does_not_replace_latest(self):
        t1, t2 = at('10:00'), at('10:05')
        self.record(t2, shop_obs(t2, gmv=1200))
        result = self.record(t2, shop_obs(t1, gmv=1100))
        self.assertEqual(result['late'], 3)
        self.assertEqual(self.good()['value'], '1200')

    def test_failed_attempt_never_erases_latest_good(self):
        t1, t2 = at('10:00'), at('10:05')
        self.record(t1, shop_obs(t1))
        failed = self.record(t2, [], failure=('FAILED', 'ADAPTER_ERROR:RuntimeError'))
        self.assertEqual((failed['status'], failed['erased_good']), ('FAILED', 0))
        self.assertEqual(self.store.latest_attempt('shop')['status'], 'FAILED')
        self.assertEqual(self.store.latest_success('shop')['slot_ts'], t1)
        self.assertEqual(self.good()['value'], '1000')

    def test_failure_stores_no_observations_even_if_given(self):
        ts = at('10:00')
        result = self.record(ts, shop_obs(ts), failure=('SKIPPED', 'BROWSER_CONTENTION'))
        self.assertEqual((result['status'], result['inserted']), ('SKIPPED', 0))
        self.assertIsNone(self.good())

    def test_drop_to_zero_is_held_as_suspect(self):
        self.record(at('10:00'), shop_obs(at('10:00'), gmv=5000))
        for hhmm in ('10:05', '10:10', '10:15'):
            result = self.record(at(hhmm), shop_obs(at(hhmm), gmv=0))
            self.assertEqual((result['status'], result['error_code']), ('PARTIAL', 'SUSPECT_VALUE'))
        self.assertEqual(self.good()['value'], '5000')
        self.assertEqual(self.good('orders')['event_ts'], at('10:15'))

    def test_large_drop_needs_confirmation(self):
        self.record(at('10:00'), shop_obs(at('10:00'), gmv=5000))
        self.record(at('10:05'), shop_obs(at('10:05'), gmv=2000))
        self.assertEqual(self.good()['value'], '5000')
        self.record(at('10:10'), shop_obs(at('10:10'), gmv=2050))
        good = self.good()
        self.assertEqual((good['value'], good['quality_note']), ('2050', 'CONFIRMED_DROP'))

    def test_partial_attempt_completeness(self):
        ts = at('10:00')
        result = self.record(ts, shop_obs(ts)[:2])
        self.assertEqual((result['status'], result['error_code']), ('PARTIAL', 'MISSING_REQUIRED'))
        self.assertAlmostEqual(result['completeness'], 2 / 3)
        self.assertAlmostEqual(self.good()['completeness'], 2 / 3)

    def test_rejections(self):
        ts = at('10:00')
        cases = [
            (obs('search_uv', 'store', 1, ts), 'NOT_REALTIME_ELIGIBLE'),
            (obs('gmv', 'sku:123', 1, ts), 'NOT_REALTIME_ELIGIBLE'),
            (obs('price_min', 'store', 1, ts), 'NOT_REALTIME_ELIGIBLE'),
            (obs('cs_sessions', 'store', 1, ts), 'NOT_REALTIME_ELIGIBLE'),
            (obs('consumer_intent_score', 'store', 1, ts), 'NOT_REALTIME_ELIGIBLE'),
            (obs('refunds', 'store', 1, ts), 'NOT_IN_CATALOG'),
            (obs('gmv', 'store', 1, ts, definition='other'), 'DEFINITION_MISMATCH'),
            (obs('gmv', 'store', 'NaN', ts), 'INVALID_VALUE'),
            (obs('gmv', 'store', None, ts), 'INVALID_VALUE'),
            (obs('gmv', 'store', -1, ts), 'NEGATIVE_VALUE'),
            (obs('gmv', 'store', 1, None), 'MISSING_EVENT_TIME'),
            (obs('gmv', 'store', 1, '2026-09-29T10:00:00'), 'MISSING_EVENT_TIME'),
            (obs('gmv', 'store', 1, ts + 3600), 'FUTURE_EVENT_TIME'),
            (obs('gmv', 'store', 1, ts - 3 * 86400), 'STALE_EVENT_TIME'),
        ]
        for item, reason in cases:
            with self.subTest(reason=reason, metric=item['metric']):
                result = self.record(ts, [item])
                self.assertEqual(result['rejected'], [reason])
                self.assertEqual(result['status'], 'FAILED')
        self.assertIsNone(self.good())

    def test_retention_keeps_recent_days(self):
        old_day = timeutil.shift_day(DAY, -50)
        old, new = at('10:00', old_day), at('10:00')
        self.store.max_event_age = 100 * 86400
        self.record(old, shop_obs(old))
        self.record(new, shop_obs(new))
        result = self.store.apply_retention(DAY, 42)
        self.assertEqual(result['observations_deleted'], 3)
        self.assertIsNone(self.store.latest_good('shop', 'gmv', 'store', old_day))
        self.assertIsNotNone(self.good())

    def test_concurrent_writers_and_readers(self):
        errors, base = [], at('08:00')

        def writer(source, metric, entity):
            try:
                for i in range(40):
                    ts = base + i * 60
                    self.store.record_attempt(source, ts, started_ts=ts, finished_ts=ts + 1,
                                              observations=[obs(metric, entity, i + 1, ts)])
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def reader():
            try:
                for _ in range(60):
                    self.store.good_snapshot(DAY)
                    self.store.latest_attempt('shop')
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=('shop', 'gmv', 'store')),
                   threading.Thread(target=writer, args=('ads', 'ad_spend', 'account'))]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with self.store.read() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempt').fetchone()[0], 80)
        self.assertEqual(self.good()['value'], '40')


class ScheduleAndConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_slots(self):
        schedule = Schedule(self.cfg.windows, TZ)
        slots = schedule.slots(DAY)
        self.assertEqual(len(slots), 16 + 192)
        self.assertEqual(schedule.slot_at(at('07:45')), at('07:30'))
        self.assertEqual(schedule.slot_at(at('08:02')), at('08:00'))
        self.assertEqual(schedule.next_slot(at('07:59')), at('08:00'))
        self.assertEqual(schedule.next_slot(at('23:57')), at('00:00', timeutil.shift_day(DAY, 1)))
        self.assertEqual((schedule.interval_at(at('03:00')), schedule.interval_at(at('14:00'))), (1800, 300))

    def test_example_config_loads(self):
        cfg = config.load(ROOT / 'examples' / 'realtime_config.json', base_dir=self.tmp.name)
        self.assertEqual(cfg.catalog.browser_purposes(), ['jd_jingzhuntong_intraday', 'jd_shangzhi_realtime'])
        self.assertEqual(len(cfg.rules), 5)
        self.assertFalse(cfg.lanes['MIMO26_REALTIME_SHADOW_V1']['enabled'])
        self.assertEqual(cfg.lanes['production']['model'], 'mimo-v2.5-pro')

    def test_config_guards(self):
        metric = {'metric': 'gmv', 'entity_type': 'store', 'required': True, 'definition': 'd'}
        bad = {
            'pseudo realtime search': {'sources': {'s': {'metrics': [dict(metric, metric='search_uv')]}}},
            'pseudo realtime sku': {'sources': {'s': {'metrics': [dict(metric, entity_type='sku', entities=['sku:1'])]}}},
            'missing definition': {'sources': {'s': {'metrics': [dict(metric, definition='')]}}},
            'challenger serving ui': {'sources': {'s': {'metrics': [metric]}}, 'inference': {'lanes': {
                'x': {'model': 'm', 'role': 'CHALLENGER', 'serve_to_ui': True}}}},
            'no hysteresis': {'sources': {'s': {'metrics': [metric]}}, 'signals': {'rules': [
                {'rule': 'GMV_DROP', 'series': {'source': 's', 'metric': 'gmv', 'entity': 'store'},
                 'open_delta': -0.3, 'resolve_delta': -0.3}]}},
            'retention shorter than lookback': {'sources': {'s': {'metrics': [metric]}}, 'store': {'retention_days': 7},
                                                'signals': {'rules': [
                                                    {'rule': 'GMV_DROP', 'series': {'source': 's', 'metric': 'gmv', 'entity': 'store'}}]}},
            'windowed ratio': {'sources': {'s': {'metrics': [metric, dict(metric, metric='orders')]}}, 'signals': {'rules': [
                {'rule': 'ROI_COLLAPSE', 'basis': 'window', 'window_minutes': 60,
                 'numerator': {'source': 's', 'metric': 'gmv', 'entity': 'store'},
                 'denominator': {'source': 's', 'metric': 'orders', 'entity': 'store'}}]}},
            'schedule gap': {'sources': {'s': {'metrics': [metric]}}, 'schedule': {'windows': [
                {'start': '00:00', 'end': '08:00', 'interval_minutes': 30},
                {'start': '09:00', 'end': '24:00', 'interval_minutes': 5}]}},
        }
        for name, raw in bad.items():
            with self.subTest(name), self.assertRaises(config.ConfigError):
                config.load(raw, base_dir=self.tmp.name)


if __name__ == '__main__':
    unittest.main()
