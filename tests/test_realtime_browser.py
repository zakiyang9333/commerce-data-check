import os
import socket
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from realtime import browser as browser_module
from realtime.browser import BrowserContention, BrowserGuardError, DedicatedBrowser, FileLock, choose_port


def free_block(size=6):
    """First port of `size` consecutive free ports."""
    for start in range(20000, 60000, 97):
        if all(not browser_module.port_in_use(p) for p in range(start, start + size)):
            return start
    raise unittest.SkipTest('no free port block')


class BrowserFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = free_block()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **options):
        root = Path(self.tmp.name)
        defaults = dict(registry_path=root / 'reg.json', lock_path=root / 'rt.lock', profile_dir=root / 'profile',
                        purposes=['shop', 'ads'], reserved_ports=[self.base + 1], port_range=(self.base, self.base + 5))
        defaults.update(options)
        return DedicatedBrowser(**defaults)


class DedicatedBrowserTests(BrowserFixture):
    def test_port_choice_skips_reserved_and_busy_ports(self):
        with closing(socket.socket()) as busy:
            busy.bind(('127.0.0.1', self.base))
            busy.listen()
            self.assertEqual(choose_port((self.base, self.base + 5), {self.base + 1}), self.base + 2)
            entry = self.make().register()
        self.assertEqual(entry['port'], self.base + 2)
        self.assertNotIn(entry['port'], entry['reserved_ports'])
        self.assertEqual(self.make().register()['port'], self.base + 2)

    def test_never_uses_reserved_shared_port(self):
        with self.assertRaises(BrowserGuardError):
            choose_port((self.base + 1, self.base + 1), {self.base + 1})

    def test_profile_must_be_dedicated(self):
        manual = Path(self.tmp.name) / 'manual-profile'
        manual.mkdir()
        (manual / 'Cookies').write_text('x')
        with self.assertRaises(BrowserGuardError):
            self.make(profile_dir=manual).register()
        with self.assertRaises(BrowserGuardError):
            self.make(profile_dir=browser_module.default_profile_dirs()[0]).register()

    def test_lock_is_independent_and_contention_is_counted(self):
        browser = self.make()
        browser.register()
        with browser.session('shop', timeout=1):
            with self.assertRaises(BrowserContention):
                with browser.session('ads', timeout=0.2):
                    pass
            other = FileLock(Path(self.tmp.name) / 'shared-browser.lock')
            self.assertTrue(other.acquire(0))
            other.release()
        self.assertEqual(browser.contention_n, 1)
        with browser.session('ads', timeout=1) as session:
            self.assertEqual(session.port, self.base)

    def test_only_realtime_purposes(self):
        browser = self.make()
        browser.register()
        with self.assertRaises(BrowserGuardError):
            with browser.session('other-agent-task'):
                pass

    def test_launch_command_uses_dedicated_profile_and_port(self):
        browser = self.make(chrome_path='/usr/bin/true')
        entry = browser.register()
        command = browser.launch_command(entry)
        self.assertIn('--remote-debugging-port=%d' % entry['port'], command)
        self.assertIn('--user-data-dir=%s' % entry['profile_dir'], command)
        self.assertIn('%s=%s' % (browser_module.ID_SWITCH, entry['browser_id']), command)

    def test_stop_refuses_a_process_it_does_not_own(self):
        browser = self.make()
        entry = browser.register()
        entry['pid'] = os.getpid()
        browser_module.write_json_atomic(browser.registry_path, entry)
        with self.assertRaises(BrowserGuardError):
            browser.stop()

    def test_probe_reports_unreachable(self):
        browser = self.make()
        self.assertEqual(browser.probe()['status'], 'NOT_REGISTERED')
        browser.register()
        self.assertEqual(browser.probe()['status'], 'UNREACHABLE')


@unittest.skipUnless(browser_module.find_chrome() and os.name == 'posix', 'Chrome/Chromium not available')
class ChromiumIntegration(BrowserFixture):
    def test_launch_probe_recover_stop(self):
        args = ['--headless=new', '--disable-gpu'] + (['--no-sandbox'] if os.geteuid() == 0 else [])
        browser = self.make(extra_args=args, restart_min_interval=0, startup_timeout=30)
        try:
            entry = browser.launch()
            health = browser.probe('shop')
            self.assertEqual((health['status'], health['own_process']), ('HEALTHY', True))
            self.assertNotIn(entry['port'], entry['reserved_ports'])
            os.killpg(entry['pid'], 9)
            browser._proc.wait(10)
            self.assertEqual(browser.probe()['status'], 'UNREACHABLE')
            recovered = browser.ensure_running()
            self.assertEqual((recovered['action'], recovered['status']), ('RESTARTED', 'HEALTHY'))
            self.assertTrue(browser.stop())
            deadline = time.monotonic() + 10
            while browser.probe()['status'] != 'UNREACHABLE' and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertEqual(browser.probe()['status'], 'UNREACHABLE')
        finally:
            if browser._proc is not None and browser._proc.poll() is None:
                browser._proc.kill()


if __name__ == '__main__':
    unittest.main()
