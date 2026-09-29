"""Shared SQLite + WAL plumbing for the realtime databases."""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class StoreGuardError(RuntimeError):
    """Raised instead of writing into a database this layer does not own."""


class Database:
    kind = None
    schema = ''
    schema_version = '1'

    def __init__(self, path, *, busy_timeout_ms=5000, forbidden_paths=()):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._local, self._open, self._open_lock = threading.local(), [], threading.Lock()
        resolved = self.path.resolve()
        for forbidden in forbidden_paths:
            if resolved == Path(forbidden).resolve():
                raise StoreGuardError('refusing to write into %s' % forbidden)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self):
        # Connections are thread-local (see _connection); check_same_thread only blocks close() from another thread.
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout = %d' % int(self.busy_timeout_ms))
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA synchronous = NORMAL')
        return conn

    def _initialise(self):
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        conn = self._connect()
        try:
            if not fresh:
                try:
                    row = conn.execute("SELECT value FROM store_meta WHERE key = 'store_kind'").fetchone()
                except sqlite3.DatabaseError:
                    row = None
                if row is None or row[0] != self.kind:
                    raise StoreGuardError('%s is not a %s database; refusing to write' % (self.path, self.kind))
            if conn.execute('PRAGMA journal_mode = WAL').fetchone()[0].lower() != 'wal':
                raise StoreGuardError('WAL journal mode unavailable for %s' % self.path)
            conn.execute('CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            conn.executescript(self.schema)
            conn.execute("INSERT OR IGNORE INTO store_meta VALUES ('store_kind', ?)", (self.kind,))
            conn.execute("INSERT OR IGNORE INTO store_meta VALUES ('schema_version', ?)", (self.schema_version,))
        finally:
            conn.close()

    def _connection(self):
        """One autocommit connection per thread, reused across calls."""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._local.conn = self._connect()
            with self._open_lock:
                self._open.append(conn)
        return conn

    def close(self):
        with self._open_lock:
            for conn in self._open:
                conn.close()
            self._open.clear()
        self._local = threading.local()

    @contextmanager
    def read(self):
        yield self._connection()

    @contextmanager
    def write(self):
        conn = self._connection()
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        conn.execute('COMMIT')

    def rows(self, sql, args=(), conn=None):
        if conn is None:
            with self.read() as own:
                return [dict(r) for r in own.execute(sql, args).fetchall()]
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
