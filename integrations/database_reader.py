"""Bounded, read-only SQLite access for AI analysis. No runtime initialization."""
from contextlib import closing
from pathlib import Path
import json
import re
import sqlite3
import time

TABLES = frozenset({
    'external_category_sources', 'messages', 'housinglist', 'transferlist', 'joblist', 'transfer_locations',
    'advertio_jobs_deliveries', 'advertio_jobs_attempts',
    'advertio_cargo_deliveries', 'advertio_cargo_attempts', 'advertio_request_log',
})
HIDDEN = frozenset({'worker_token', 'media_reference', 'media_path', 'media_paths'})
FUNCTIONS = frozenset('count sum avg min max total coalesce nullif ifnull lower upper trim ltrim rtrim length substr substring replace instr round abs date datetime time strftime julianday unixepoch typeof like glob json_extract json_valid json_array_length'.split())


class DatabaseReader:
    def __init__(self, path, *, timeout=2.0):
        self.path = Path(path).expanduser().resolve()
        self.timeout = timeout

    def _connect(self):
        # mode=ro never creates a missing DB. Do not use immutable=1 on a live DB.
        conn = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.enable_load_extension(False)
        return conn

    def schema(self):
        """Only approved real tables; omit runtime credentials and worker state."""
        with closing(self._connect()) as conn:
            available = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: [dict(name=r['name'], type=r['type'], primary_key=bool(r['pk']))
                            for r in conn.execute(f'PRAGMA table_info("{table}")')
                            if r['name'] not in HIDDEN]
                    for table in sorted(TABLES & available)}

    @staticmethod
    def _authorize(action, first, second, database, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # SQLite reports database=None/column='' for count(*) and some
            # rowid scans. No temp/attached databases can be created here.
            readable = database == 'main' or (database is None and second == '')
            return (sqlite3.SQLITE_OK if readable and first in TABLES
                    and second not in HIDDEN else sqlite3.SQLITE_DENY)
        if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() in FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def query(self, sql, parameters=None, limit=50):
        """Execute one SELECT/CTE. SQL authorization, not prefix matching, enforces access."""
        if not isinstance(sql, str) or len(sql) > 12000 or not re.match(r'^\s*(SELECT|WITH)\b', sql, re.I):
            raise ValueError('Use one SELECT or WITH query (maximum 12000 characters).')
        if not 1 <= limit <= 200:
            raise ValueError('limit must be between 1 and 200')
        parameters = [] if parameters is None else parameters
        if not isinstance(parameters, list) or any(type(v) not in (str, int, float, bool, type(None)) for v in parameters):
            raise ValueError('parameters must be a list of scalar JSON values')
        with closing(self._connect()) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 12000)
            deadline = time.monotonic() + self.timeout
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            conn.set_authorizer(self._authorize)
            cursor = conn.execute(sql, parameters)
            columns = [d[0] for d in cursor.description]
            output, size, truncated, truncated_cells = [], 0, False, 0
            for index, row in enumerate(cursor):
                if index >= limit:
                    truncated = True
                    break
                values = []
                for value in row:
                    if isinstance(value, bytes):
                        value = '<binary omitted>'
                    if isinstance(value, str) and len(value) > 6000:
                        value = value[:6000] + ' [truncated]'
                        truncated_cells += 1
                    values.append(value)
                size += len(json.dumps(values, ensure_ascii=False).encode())
                if size > 256000:
                    truncated = True
                    break
                output.append(values)
            return dict(columns=columns, rows=output, row_count=len(output),
                        truncated=truncated, truncated_cells=truncated_cells)

    def inspect_message(self, local_id):
        schema = self.schema()
        if 'messages' not in schema:
            raise ValueError('messages table is unavailable')
        result = {}
        for table, fields in schema.items():
            names = {f['name'] for f in fields}
            key = 'id' if table == 'messages' else 'processed_message_id'
            if key not in names:
                continue
            columns = ','.join('"' + f['name'].replace('"', '""') + '"' for f in fields)
            result[table] = self.query(f'SELECT {columns} FROM "{table}" WHERE "{key}"=?', [local_id], 50)
        return result

    def summary(self):
        schema = self.schema()
        result = {}
        for table, field in [('messages', 'processing_status'), ('messages', 'classification_status'),
                             ('messages', 'ai_status'), ('messages', 'ai_error'),
                             ('advertio_jobs_deliveries', 'status'), ('advertio_jobs_deliveries', 'error_code'),
                             ('advertio_cargo_deliveries', 'status'), ('advertio_cargo_deliveries', 'error_code')]:
            if field in {f['name'] for f in schema.get(table, [])}:
                result[f'{table}.{field}'] = self.query(
                    f'SELECT "{field}", COUNT(*) AS count FROM "{table}" GROUP BY "{field}" ORDER BY count DESC', limit=200)
        return result
