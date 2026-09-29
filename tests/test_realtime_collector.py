import contextlib
import tempfile
import threading
import unittest

from realtime.browser import BrowserContention, LoginRequired
from realtime.collector import Collector
from realtime.store import RealtimeFactStore
from realtime_support import DAY, at, make_config, obs, shop_obs


class FakeBrowser:
    def __init__(self, status='HEALTHY', busy=False):
        self.status, self.busy, self.sessions = status, busy, []

    @contextlib.contextmanager
    def session(self, purpose, timeout=30):
        if self.busy:
            raise BrowserContention('busy')
        self.sessions.append(purpose)
        yield self

    browser_id = 'fake-browser'

    def health(self):
        return {'status': self.status}


class Adapter:
    def __init__(self, fn):
        self.fn = fn

    def collect(self, session, slot_ts):
        return self.fn(session, slot_ts)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(self.tmp.name)
        self.store = RealtimeFactStore.from_config(self.cfg)
        self.slot = at('10:00')
        self.now = self.slot + 20

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def collect(self, shop, ads=None, browser=None, timeout=5):
        adapters = {'shop': Adapter(shop)}
        if ads is not None:
            adapters['ads'] = Adapter(ads)
        collector = Collector(self.store, self.cfg.catalog, adapters, browser=browser or FakeBrowser(),
                              source_timeout=timeout, lock_timeout=0.1, clock=lambda: self.now)
        return {r['source']: r for r in collector.run_slot(self.slot)}

    def test_success_through_dedicated_browser(self):
        browser = FakeBrowser()
        results = self.collect(lambda s, t: shop_obs(self.slot), browser=browser)
        self.assertEqual(results['shop']['status'], 'SUCCESS')
        self.assertEqual(browser.sessions, ['shop'])
        self.assertEqual((results['ads']['status'], results['ads']['error_code']), ('SKIPPED', 'NO_ADAPTER'))

    def test_failures_are_classified_and_keep_latest_good(self):
        self.collect(lambda s, t: shop_obs(self.slot))

        def boom(session, slot):
            raise RuntimeError('page changed')

        def login(session, slot):
            raise LoginRequired()

        def slow(session, slot):
            raise TimeoutError()

        cases = [(boom, None, 'ADAPTER_ERROR:RuntimeError'), (login, None, 'AUTH_REQUIRED'),
                 (slow, None, 'SOURCE_TIMEOUT'),
                 (lambda s, t: shop_obs(self.slot), FakeBrowser(status='LOGIN_REQUIRED'), 'AUTH_REQUIRED'),
                 (lambda s, t: shop_obs(self.slot), FakeBrowser(status='UNREACHABLE'), 'BROWSER_UNREACHABLE')]
        for fn, browser, code in cases:
            with self.subTest(code=code):
                result = self.collect(fn, browser=browser)['shop']
                self.assertEqual((result['status'], result['error_code'], result['erased_good']), ('FAILED', code, 0))
                self.assertEqual(self.store.latest_good('shop', 'gmv', 'store', DAY)['value'], '1000')

    def test_contention_is_skipped(self):
        result = self.collect(lambda s, t: shop_obs(self.slot), browser=FakeBrowser(busy=True))['shop']
        self.assertEqual((result['status'], result['error_code']), ('SKIPPED', 'BROWSER_CONTENTION'))

    def test_hung_adapter_is_abandoned_and_its_late_result_discarded(self):
        release = threading.Event()

        def hang(session, slot):
            release.wait(5)
            return shop_obs(self.slot)

        result = self.collect(hang, timeout=0.2)['shop']
        self.assertEqual((result['status'], result['error_code']), ('FAILED', 'SOURCE_TIMEOUT'))
        release.set()
        self.assertIsNone(self.store.latest_good('shop', 'gmv', 'store', DAY))

    def test_partial_and_forbidden_metrics(self):
        def partial(session, slot):
            return shop_obs(self.slot)[:2] + [obs('search_keywords', 'store', 5, self.slot)]

        result = self.collect(partial)['shop']
        self.assertEqual((result['status'], result['rejected']), ('PARTIAL', ['NOT_REALTIME_ELIGIBLE']))

    def test_source_without_browser_needs_no_session(self):
        cfg = make_config(self.tmp.name, sources={'feed': {'metrics': [
            {'metric': 'warehouse_orders', 'entity_type': 'warehouse', 'required': True, 'definition': 'd'}]}})
        store = RealtimeFactStore(self.tmp.name + '/feed.db', cfg.catalog, cfg.tz)
        seen = []

        def collect(session, slot):
            seen.append(session)
            return [obs('warehouse_orders', 'warehouse', 3, self.slot)]

        collector = Collector(store, cfg.catalog, {'feed': Adapter(collect)}, clock=lambda: self.now)
        self.assertEqual(collector.run_slot(self.slot)[0]['status'], 'SUCCESS')
        self.assertEqual(seen, [None])
        store.close()


if __name__ == '__main__':
    unittest.main()
