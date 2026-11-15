"""SQLite state: one row per night, a short activity log, and a few remembered times."""
import json
import sqlite3
import threading
import time

NEW, SAMPLED, FETCHED, SCREENED, DONE = 'new', 'sampled', 'fetched', 'screened', 'done'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nights (
    id TEXT PRIMARY KEY, target TEXT, planet TEXT, first_frame TEXT, last_frame TEXT, frames INTEGER,
    stage TEXT, grade TEXT, headline TEXT, failures INTEGER DEFAULT 0, cleaned INTEGER DEFAULT 0,
    listing TEXT, report TEXT, updated REAL, uploaded TEXT);
CREATE TABLE IF NOT EXISTS log (ts REAL, night TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS times (what TEXT PRIMARY KEY, ts REAL);
"""
_COLUMNS = {'target', 'planet', 'first_frame', 'last_frame', 'frames', 'stage', 'grade', 'headline', 'failures',
            'cleaned', 'listing', 'report', 'uploaded'}
_JSON = {'listing', 'report'}


class Store:
    def __init__(self, path):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            if 'uploaded' not in [row['name'] for row in self._db.execute('PRAGMA table_info(nights)')]:
                self._db.execute('ALTER TABLE nights ADD COLUMN uploaded TEXT')      # a database from before v0.13

    @staticmethod
    def _row(row, full):
        d = dict(row)
        for key in _JSON:
            d[key] = json.loads(d[key]) if (full and d[key]) else None
        return d

    def get(self, night_id, full=True):
        with self._lock:
            row = self._db.execute('SELECT * FROM nights WHERE id = ?', (night_id,)).fetchone()
        return self._row(row, full) if row else None

    def all(self, full=False):
        with self._lock:
            rows = self._db.execute('SELECT * FROM nights ORDER BY first_frame').fetchall()
        return [self._row(r, full) for r in rows]

    def save(self, night_id, **fields):
        """Insert or update a night. Only known columns are accepted."""
        unknown = set(fields) - _COLUMNS
        if unknown:
            raise KeyError('unknown columns: %s' % sorted(unknown))
        values = {k: json.dumps(v) if k in _JSON and v is not None else v for k, v in fields.items()}
        values['updated'] = time.time()
        names = list(values)
        sql = ('INSERT INTO nights (id, %s) VALUES (?, %s) ON CONFLICT(id) DO UPDATE SET %s'
               % (', '.join(names), ', '.join('?' * len(names)), ', '.join('%s = excluded.%s' % (n, n) for n in names)))
        with self._lock, self._db:
            self._db.execute(sql, [night_id] + [values[n] for n in names])

    def log(self, night_id, message):
        with self._lock, self._db:
            self._db.execute('INSERT INTO log VALUES (?, ?, ?)', (time.time(), night_id, message))
            self._db.execute('DELETE FROM log WHERE ts < ?', (time.time() - 60 * 86400,))

    def when(self, what):
        """The time something was last done, or 0."""
        with self._lock:
            row = self._db.execute('SELECT ts FROM times WHERE what = ?', (what,)).fetchone()
        return row['ts'] if row else 0.0

    def mark(self, what):
        with self._lock, self._db:
            self._db.execute('INSERT OR REPLACE INTO times VALUES (?, ?)', (what, time.time()))

    def recent(self, count=20):
        with self._lock:
            rows = self._db.execute('SELECT * FROM log ORDER BY ts DESC LIMIT ?', (count,)).fetchall()
        return [dict(r) for r in rows]
