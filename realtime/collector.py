"""Realtime collector: one attempt per enabled source per slot.

Adapters are supplied by the dashboard (they know each platform's pages); this module
owns locking, timeouts, failure accounting and atomic storage. A slow adapter is
abandoned after the source timeout; its late result is discarded.
"""
import threading
import time

from .browser import BrowserContention, BrowserGuardError, LoginRequired


class Collector:
    def __init__(self, store, catalog, adapters, *, browser=None, source_timeout=90, lock_timeout=30, clock=time.time):
        self.store, self.catalog, self.adapters = store, catalog, dict(adapters)
        self.browser = browser
        self.source_timeout, self.lock_timeout = source_timeout, lock_timeout
        self.clock = clock

    @classmethod
    def from_config(cls, cfg, store, adapters, browser=None, clock=time.time):
        return cls(store, cfg.catalog, adapters, browser=browser, clock=clock,
                   source_timeout=cfg['collector']['source_timeout_seconds'],
                   lock_timeout=cfg['browser']['lock_timeout_seconds'])

    def run_slot(self, slot_ts):
        results = []
        for source in self.catalog.sources():
            adapter = self.adapters.get(source)
            if adapter is None:
                now = self.clock()
                results.append(self.store.record_attempt(source, slot_ts, started_ts=now, finished_ts=now,
                                                         failure=('SKIPPED', 'NO_ADAPTER')))
            else:
                results.append(self._collect(source, adapter, slot_ts))
        return results

    def _fetch(self, source, adapter, slot_ts, box):
        try:
            if not self.catalog.dedicated_browser[source]:
                box['observations'] = list(adapter.collect(None, slot_ts))
                return
            if self.browser is None:
                box['failure'] = ('FAILED', 'BROWSER_NOT_CONFIGURED')
                return
            with self.browser.session(source, timeout=self.lock_timeout) as session:
                box['browser_id'] = session.browser_id
                status = session.health()['status']
                if status == 'LOGIN_REQUIRED':
                    box['failure'] = ('FAILED', 'AUTH_REQUIRED')
                elif status != 'HEALTHY':
                    box['failure'] = ('FAILED', 'BROWSER_' + status)
                else:
                    box['observations'] = list(adapter.collect(session, slot_ts))
        except BrowserContention:
            box['failure'] = ('SKIPPED', 'BROWSER_CONTENTION')
        except LoginRequired:
            box['failure'] = ('FAILED', 'AUTH_REQUIRED')
        except BrowserGuardError:
            box['failure'] = ('FAILED', 'BROWSER_GUARD')
        except TimeoutError:
            box['failure'] = ('FAILED', 'SOURCE_TIMEOUT')
        except Exception as exc:
            box['failure'] = ('FAILED', 'ADAPTER_ERROR:' + type(exc).__name__)

    def _collect(self, source, adapter, slot_ts):
        started, box = self.clock(), {}
        worker = threading.Thread(target=self._fetch, args=(source, adapter, slot_ts, box),
                                  name='realtime-collect-' + source, daemon=True)
        worker.start()
        worker.join(self.source_timeout)
        if worker.is_alive():
            failure, observations = ('FAILED', 'SOURCE_TIMEOUT'), ()
        else:
            failure, observations = box.get('failure'), box.get('observations', ())
        return self.store.record_attempt(source, slot_ts, started_ts=started, finished_ts=self.clock(),
                                         observations=observations, failure=failure, browser_id=box.get('browser_id'))
