"""MCP's SQLite reader must not create, migrate, write or expose runtime state."""
import hashlib
import sqlite3

import pytest

from integrations.database_reader import DatabaseReader


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'database with spaces.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
        CREATE TABLE messages (id INTEGER PRIMARY KEY, message_id INTEGER,
            channel_username TEXT, raw_text TEXT, cleaned_text TEXT, ai_status TEXT,
            ai_error TEXT, processing_status TEXT, classification_status TEXT, media_reference TEXT);
        CREATE TABLE joblist (processed_message_id INTEGER, city TEXT, is_job_listing INTEGER);
        CREATE TABLE advertio_jobs_deliveries (processed_message_id INTEGER, status TEXT,
            error_code TEXT, worker_token TEXT);
        CREATE TABLE secrets (token TEXT);
        INSERT INTO messages VALUES (1,307021,'canadajobbanktg','Original Toronto job',
            'Toronto job','processed',NULL,'processed','processed','private file reference');
        INSERT INTO messages VALUES (2,307021,'other_channel','Other message',
            'Other message','failed','invalid_provider_output','processed','processed',NULL);
        INSERT INTO joblist VALUES (1,NULL,1);
        INSERT INTO advertio_jobs_deliveries VALUES (1,'blocked','missing_canadian_city','private token');
        INSERT INTO secrets VALUES ('never expose');
        ''')
    return path


def test_analysis_reads_source_extraction_delivery_and_parameterized_joins(db):
    reader = DatabaseReader(db)
    schema = reader.schema()
    assert 'secrets' not in schema
    assert 'media_reference' not in {f['name'] for f in schema['messages']}
    result = reader.inspect_message(1)
    assert result['messages']['rows'][0][3] == 'Original Toronto job'
    assert result['joblist']['rows'] == [[1, None, 1]]
    assert result['advertio_jobs_deliveries']['rows'] == [[1, 'blocked', 'missing_canadian_city']]
    assert reader.inspect_message(999)['messages']['rows'] == []
    joined = reader.query('''WITH jobs AS (SELECT m.id,j.city,d.error_code FROM messages m
        JOIN joblist j ON j.processed_message_id=m.id
        JOIN advertio_jobs_deliveries d ON d.processed_message_id=m.id
        WHERE m.channel_username=?) SELECT * FROM jobs''', ['canadajobbanktg'])
    assert joined['rows'] == [[1, None, 'missing_canadian_city']]
    assert reader.summary()['messages.ai_status']['row_count'] == 2


@pytest.mark.parametrize('sql', [
    'DELETE FROM messages', 'UPDATE messages SET raw_text="changed"',
    'WITH x AS (SELECT 1) DELETE FROM messages',
    "ATTACH DATABASE ':memory:' AS extra", 'PRAGMA writable_schema=ON',
    'SELECT * FROM messages; DELETE FROM messages', 'SELECT * FROM secrets',
    'SELECT * FROM sqlite_master', 'SELECT media_reference FROM messages',
    'SELECT worker_token FROM advertio_jobs_deliveries',
    "SELECT load_extension('/tmp/no')", "SELECT readfile('/etc/passwd')",
    'SELECT * FROM pragma_table_info(\'secrets\')',
])
def test_sql_restrictions_leave_database_unchanged(db, sql):
    before = hashlib.sha256(db.read_bytes()).digest()
    with pytest.raises((ValueError, sqlite3.Error)):
        DatabaseReader(db).query(sql)
    assert hashlib.sha256(db.read_bytes()).digest() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'absent.db'
    with pytest.raises(sqlite3.OperationalError):
        DatabaseReader(path).schema()
    assert not path.exists()


def test_result_and_runtime_bounds(db):
    reader = DatabaseReader(db)
    assert reader.query('SELECT id FROM messages ORDER BY id', limit=1)['truncated']
    assert not reader.query('SELECT id FROM messages ORDER BY id', limit=2)['truncated']
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE messages SET raw_text=? WHERE id=1', ('x' * 7000,))
    assert reader.query('SELECT raw_text FROM messages WHERE id=1')['truncated_cells'] == 1
    with pytest.raises(ValueError):
        reader.query('SELECT 1', limit=201)
    with pytest.raises(ValueError):
        reader.query('SELECT ?', parameters=[{}])
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        DatabaseReader(db, timeout=0.01).query('''WITH RECURSIVE x(n) AS
            (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT sum(n) FROM x''')


def test_reader_can_observe_new_committed_rows(db):
    reader = DatabaseReader(db)
    assert reader.query('SELECT count(*) FROM messages')['rows'] == [[2]]
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO messages(id) VALUES(3)")
    assert reader.query('SELECT count(*) FROM messages')['rows'] == [[3]]


@pytest.mark.asyncio
async def test_real_stdio_protocol(db):
    pytest.importorskip('mcp')
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'integrations.mcp_server', '--db', str(db)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {t.name for t in listed.tools} == {
                'database_schema', 'query_database', 'inspect_message', 'pipeline_summary'}
            assert all(t.annotations.readOnlyHint for t in listed.tools)
            result = await session.call_tool('inspect_message', {'local_id': 1})
            assert not result.isError
            assert 'Original Toronto job' in result.content[0].text
            blocked = await session.call_tool('query_database', {'sql': 'SELECT * FROM secrets'})
            assert blocked.isError


@pytest.mark.asyncio
async def test_http_auth_and_mcp_request(db):
    pytest.importorskip('mcp')
    import httpx
    from integrations.mcp_server import BearerAuth, create_server

    app = create_server(db).streamable_http_app()
    secured = BearerAuth(app, 'x' * 32)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=secured), base_url='http://127.0.0.1:8790') as client:
            headers = {'Accept': 'application/json, text/event-stream'}
            payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'query_database', 'arguments': {'sql': 'SELECT count(*) FROM messages'}}}
            assert (await client.post('/mcp', json=payload, headers=headers)).status_code == 401
            headers['Authorization'] = 'Bearer wrong'
            assert (await client.post('/mcp', json=payload, headers=headers)).status_code == 401
            headers['Authorization'] = 'Bearer ' + 'x' * 32
            response = await client.post('/mcp', json=payload, headers=headers)
            assert response.status_code == 200
            assert not response.json()['result']['isError']


@pytest.mark.parametrize('mode', ['missing', 'empty', 'http_without_token'])
def test_cli_refuses_missing_wrong_database_and_unauthenticated_http(db, tmp_path, mode):
    pytest.importorskip('mcp')
    import os
    import subprocess
    import sys

    path = db
    if mode == 'missing':
        path = tmp_path / 'missing.db'
    elif mode == 'empty':
        path = tmp_path / 'empty.db'
        sqlite3.connect(path).close()
    env = {k: v for k, v in os.environ.items()
           if k not in ('TELCLAW_MCP_TOKEN', 'TELEGRAM_API_ID', 'TELEGRAM_API_HASH')}
    result = subprocess.run([sys.executable, '-m', 'integrations.mcp_server',
                             '--db', str(path), '--transport', 'http'],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'TELCLAW_MCP_TOKEN' in result.stderr if mode == 'http_without_token' else 'Cannot read' in result.stderr
    if mode == 'missing':
        assert not path.exists()
