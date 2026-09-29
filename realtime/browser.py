"""Dedicated realtime browser: its own port, profile, lock and process.

It serves only the configured realtime sources (store realtime, ads intraday). It never
reuses a manual-browsing profile, never takes a reserved port (such as the shared 9330
browser) and only ever stops the process it launched itself.
"""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROFILE_MARKER = '.realtime_browser_profile'
ID_SWITCH = '--realtime-browser-id'


class BrowserGuardError(RuntimeError):
    pass


class BrowserContention(RuntimeError):
    """The dedicated browser lock was not free within the timeout."""


class LoginRequired(RuntimeError):
    """Raised by a source adapter when the platform shows a login page."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.%d.tmp' % os.getpid())
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(temp, path)


def port_in_use(port, host='127.0.0.1'):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        if probe.connect_ex((host, port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def choose_port(port_range, reserved, host='127.0.0.1'):
    low, high = port_range
    for port in range(int(low), int(high) + 1):
        if port not in reserved and not port_in_use(port, host):
            return port
    raise BrowserGuardError('no free port in %s-%s outside reserved %s' % (low, high, sorted(reserved)))


def pid_alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        # os.kill(pid, 0) would terminate the process on Windows; query it instead.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        state = Path('/proc/%d/stat' % int(pid)).read_text().rsplit(')', 1)[1].split()[0]
        return state != 'Z'
    except (OSError, IndexError):
        return True


def process_cmdline(pid):
    """Command line of a process where the platform exposes it (Linux), else None."""
    try:
        return Path('/proc/%d/cmdline' % int(pid)).read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
    except (OSError, ValueError):
        return None


def default_profile_dirs():
    home = Path.home()
    local = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
    return [home / '.config' / 'google-chrome', home / '.config' / 'chromium',
            home / 'Library' / 'Application Support' / 'Google' / 'Chrome',
            home / 'Library' / 'Application Support' / 'Microsoft Edge',
            local / 'Google' / 'Chrome' / 'User Data', local / 'Microsoft' / 'Edge' / 'User Data']


def find_chrome():
    for candidate in (os.environ.get('CHROME_PATH'), shutil.which('google-chrome'), shutil.which('google-chrome-stable'),
                      shutil.which('chromium'), shutil.which('chromium-browser'), shutil.which('chrome'),
                      '/opt/pw-browsers/chromium',
                      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                      os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
                      os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe')):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def _lock_fd(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """Exclusive lock on one file, independent of any other browser's lock."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def acquire(self, timeout, holder=''):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            try:
                _lock_fd(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.05)
        self._fd = fd
        write_json_atomic(self.path.with_name(self.path.name + '.holder'),
                          {'pid': os.getpid(), 'holder': holder, 'acquired_at': _now_iso()})
        return True

    def release(self):
        if self._fd is not None:
            try:
                _unlock_fd(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


class BrowserSession:
    """Handle given to source adapters while they hold the dedicated browser lock."""

    def __init__(self, browser, entry, purpose):
        self.browser, self.purpose = browser, purpose
        self.browser_id, self.port = entry['browser_id'], entry['port']
        self.endpoint = 'http://%s:%d' % (browser.host, entry['port'])

    def health(self):
        return self.browser.probe(self.purpose)


class DedicatedBrowser:
    def __init__(self, *, registry_path, lock_path, profile_dir, purposes, reserved_ports=(9222, 9330),
                 port_range=(9340, 9399), host='127.0.0.1', chrome_path=None, headless=False, extra_args=(),
                 startup_timeout=20, restart_min_interval=60, max_restarts_per_hour=6,
                 login_url_markers=(), login_checks=None, clock=time.time):
        self.registry_path, self.lock_path = Path(registry_path), Path(lock_path)
        self.profile_dir = Path(profile_dir)
        self.purposes = sorted(purposes)
        self.reserved = {int(p) for p in reserved_ports}
        self.port_range, self.host = tuple(port_range), host
        self.chrome_path, self.headless, self.extra_args = chrome_path, headless, list(extra_args)
        self.startup_timeout = startup_timeout
        self.restart_min_interval, self.max_restarts_per_hour = restart_min_interval, max_restarts_per_hour
        self.login_url_markers = list(login_url_markers)
        self.login_checks = dict(login_checks or {})
        self.clock = clock
        self.contention_n = 0
        self._proc = None

    @classmethod
    def from_config(cls, cfg, login_checks=None, clock=time.time):
        conf = cfg['browser']
        return cls(registry_path=cfg.path('browser_registry'), lock_path=cfg.path('browser_lock'),
                   profile_dir=cfg.path('browser_profile'), purposes=cfg.catalog.browser_purposes(),
                   reserved_ports=conf['reserved_ports'], port_range=conf['port_range'], host=conf['host'],
                   chrome_path=conf['chrome_path'], headless=conf['headless'], extra_args=conf['extra_args'],
                   startup_timeout=conf['startup_timeout_seconds'],
                   restart_min_interval=conf['restart_min_interval_seconds'],
                   max_restarts_per_hour=conf['max_restarts_per_hour'],
                   login_url_markers=conf['login_url_markers'], login_checks=login_checks, clock=clock)

    # -- registration -----------------------------------------------------

    def registry(self):
        return _read_json(self.registry_path)

    def _check_profile(self):
        profile = self.profile_dir.resolve()
        if any(profile == d.resolve() for d in default_profile_dirs()):
            raise BrowserGuardError('the realtime browser must not reuse a manual browsing profile: %s' % profile)
        if profile.exists() and any(profile.iterdir()) and not (profile / PROFILE_MARKER).exists():
            raise BrowserGuardError('%s is an existing profile not created for the realtime browser' % profile)
        profile.mkdir(parents=True, exist_ok=True)
        (profile / PROFILE_MARKER).write_text('dedicated realtime browser profile\n', encoding='utf-8')

    def register(self):
        """Keep the registered port if still ours or free, else pick a conflict-free one; record it."""
        self._check_profile()
        current = self.registry() or {}
        port = current.get('port')
        own = bool(port) and self.is_own_process(current.get('pid'), port)
        keep = bool(port) and port not in self.reserved and (own or not port_in_use(port, self.host))
        if not keep:
            port = choose_port(self.port_range, self.reserved, self.host)
        entry = {
            'browser_id': current.get('browser_id') or 'rt-browser-' + uuid.uuid4().hex[:8],
            'port': port,
            'host': self.host,
            'profile_dir': str(self.profile_dir.resolve()),
            'lock_path': str(self.lock_path.resolve()),
            'purposes': self.purposes,
            'reserved_ports': sorted(self.reserved),
            'process_tag': ID_SWITCH,
            'registered_at': current.get('registered_at') or _now_iso(),
            'pid': current.get('pid') if own else None,
            'launched_at': current.get('launched_at') if own else None,
            'restarts': [t for t in current.get('restarts', []) if self.clock() - t < 3600],
        }
        write_json_atomic(self.registry_path, entry)
        return entry

    def is_own_process(self, pid, port):
        if not pid_alive(pid):
            return False
        cmdline = process_cmdline(pid)
        if cmdline is None:
            # No command line on this platform: trust the registered pid only while the port answers.
            return self._cdp(port, '/json/version') is not None
        return '--remote-debugging-port=%d' % port in cmdline and str(self.profile_dir.resolve()) in cmdline

    # -- process ----------------------------------------------------------

    def launch_command(self, entry):
        chrome = self.chrome_path or find_chrome()
        if not chrome:
            raise BrowserGuardError('set browser.chrome_path; no Chrome/Chromium found')
        command = [str(chrome), '--remote-debugging-port=%d' % entry['port'], '--user-data-dir=%s' % entry['profile_dir'],
                   '%s=%s' % (ID_SWITCH, entry['browser_id']), '--no-first-run', '--no-default-browser-check']
        if self.headless:
            command.append('--headless=new')
        return command + self.extra_args + ['about:blank']

    def launch(self):
        entry = self.register()
        if entry['port'] in self.reserved:
            raise BrowserGuardError('refusing reserved port %s' % entry['port'])
        if entry.get('pid') and self.is_own_process(entry['pid'], entry['port']):
            return entry
        options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        self._proc = subprocess.Popen(self.launch_command(entry), **options)
        entry.update(pid=self._proc.pid, launched_at=_now_iso())
        write_json_atomic(self.registry_path, entry)
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._cdp(entry['port'], '/json/version') is not None:
                return entry
            if self._proc.poll() is not None:
                raise BrowserGuardError('realtime browser exited during startup (code %s)' % self._proc.returncode)
            time.sleep(0.2)
        raise BrowserGuardError('realtime browser did not open port %s in time' % entry['port'])

    def stop(self, timeout=10):
        """Stop the dedicated browser. Only the registered, verified process is signalled."""
        entry = self.registry()
        if not entry or not entry.get('pid'):
            return False
        pid, port = entry['pid'], entry['port']
        if port in self.reserved:
            raise BrowserGuardError('refusing to stop a browser on reserved port %s' % port)
        if not self.is_own_process(pid, port):
            raise BrowserGuardError('pid %s is not the dedicated realtime browser' % pid)
        if self._proc is not None and self._proc.pid == pid:
            self._proc.terminate()
        elif os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/T'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and pid_alive(pid):
            if self._proc is not None:
                self._proc.poll()
            time.sleep(0.1)
        if pid_alive(pid):
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGKILL)
        if self._proc is not None:
            self._proc.wait(timeout=5)
            self._proc = None
        entry.update(pid=None)
        write_json_atomic(self.registry_path, entry)
        return True

    # -- health -----------------------------------------------------------

    def _cdp(self, port, path, timeout=2.0):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open('http://%s:%d%s' % (self.host, port, path), timeout=timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except (OSError, ValueError, http.client.HTTPException):
            return None

    def _login_state(self, entry, purpose):
        check = self.login_checks.get(purpose)
        if check is not None:
            try:
                return check('http://%s:%d' % (self.host, entry['port']))
            except Exception:
                return 'UNKNOWN'
        pages = self._cdp(entry['port'], '/json/list') or []
        urls = [p.get('url', '') for p in pages if isinstance(p, dict) and p.get('type') == 'page']
        if any(marker in url for url in urls for marker in self.login_url_markers):
            return 'LOGGED_OUT'
        return 'UNKNOWN'

    def probe(self, purpose=None):
        """Reachability and login health: HEALTHY, LOGIN_REQUIRED, UNREACHABLE or NOT_REGISTERED."""
        entry = self.registry()
        if not entry:
            return {'status': 'NOT_REGISTERED', 'reachable': False, 'login': 'UNKNOWN', 'checked_at': _now_iso()}
        version = self._cdp(entry['port'], '/json/version')
        result = {'browser_id': entry['browser_id'], 'port': entry['port'], 'checked_at': _now_iso(),
                  'reachable': version is not None, 'browser': (version or {}).get('Browser'),
                  'own_process': self.is_own_process(entry.get('pid'), entry['port'])}
        if version is None:
            return dict(result, status='UNREACHABLE', login='UNKNOWN')
        if not result['own_process']:
            return dict(result, status='FOREIGN_PROCESS', login='UNKNOWN')
        login = self._login_state(entry, purpose)
        return dict(result, login=login, status='LOGIN_REQUIRED' if login == 'LOGGED_OUT' else 'HEALTHY')

    def ensure_running(self):
        """Recover an unreachable or foreign-held browser, with restart backoff."""
        health = self.probe()
        if health['status'] in ('HEALTHY', 'LOGIN_REQUIRED'):
            return dict(health, action='NONE')
        entry = self.register()
        now = self.clock()
        restarts = entry['restarts']
        if (restarts and now - restarts[-1] < self.restart_min_interval) or len(restarts) >= self.max_restarts_per_hour:
            return dict(health, action='BACKOFF')
        entry['restarts'] = restarts + [now]
        write_json_atomic(self.registry_path, entry)
        try:
            self.launch()
        except BrowserGuardError as exc:
            return dict(self.probe(), action='RESTART_FAILED', error=str(exc))
        return dict(self.probe(), action='RESTARTED')

    @contextmanager
    def session(self, purpose, timeout=30):
        if purpose not in self.purposes:
            raise BrowserGuardError('%s may not use the dedicated realtime browser' % purpose)
        entry = self.registry()
        if not entry:
            raise BrowserGuardError('dedicated realtime browser is not registered')
        lock = FileLock(self.lock_path)
        if not lock.acquire(timeout, holder=purpose):
            self.contention_n += 1
            raise BrowserContention('dedicated realtime browser busy')
        try:
            yield BrowserSession(self, entry, purpose)
        finally:
            lock.release()
