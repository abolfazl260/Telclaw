"""Website CSV admission preserves provenance and never creates Telegram jobs."""
import base64
import csv
import io
import re
import sqlite3
from datetime import timedelta

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import backoffice_data
import backoffice_import as importer
import backoffice_web
import config
from integrations.database_reader import DatabaseReader
from storage import database


@pytest.fixture
def import_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_NAME', str(tmp_path / 'imports.sqlite'))
    database.initialize_db()


def payload(rows, headers=None):
    headers = headers or list(rows[0])
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, headers)
    writer.writeheader()
    writer.writerows(rows)
    return ('\ufeff' + stream.getvalue()).encode('utf-8')


def listing(**fields):
    return dict(source_url='https://example.org/ad/42', description='متن کامل\nToronto — شغل واقعی', **fields)


@pytest.mark.parametrize('category', database.CATEGORY_TABLES)
def test_import_has_real_category_but_no_telegram_parent_or_queue(import_db, category):
    data = payload([listing()])
    result = importer.import_csv(category, data, 12)
    assert result == [dict(line=2, id=1, status='imported')]
    conn = database.get_connection()
    try:
        row = conn.execute(f'SELECT * FROM {category}').fetchone()
        assert row['processed_message_id'] is None
        assert row['description'] == 'متن کامل\nToronto — شغل واقعی'
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM advertio_jobs_deliveries').fetchone()[0] == 0
        assert conn.execute(f'SELECT COUNT(*) FROM {category} c JOIN messages m ON m.id=c.processed_message_id').fetchone()[0] == 0
        source = conn.execute('SELECT * FROM external_category_sources').fetchone()
        assert source['source_type'] == 'website' and source['imported_by'] == 12
        assert source['source_date'] is None
    finally:
        conn.close()
    assert importer.import_csv(category, data, 12)[0]['status'] == 'duplicate'
    # Restart keeps parentless records; AI can query both data and provenance.
    database.initialize_db()
    reader = DatabaseReader(config.DB_NAME)
    assert reader.query(f'''SELECT c.description,s.source_url FROM {category} c
        JOIN external_category_sources s ON s.category_record_id=c.id AND s.category=?''', [category])['row_count'] == 1
    assert backoffice_data.page(category)['total'] == 1
    assert backoffice_data.page('external_category_sources')['editable'] == []


def test_duplicate_does_not_overwrite_and_same_url_can_have_other_category(import_db):
    importer.import_csv('joblist', payload([listing()]), 12)
    changed = listing(); changed['description'] = 'different'
    repeated = importer.import_csv('joblist', payload([changed, changed]), 13)
    assert all(r['status'] == 'duplicate' for r in repeated)
    assert backoffice_data.page('joblist')['rows'][0]['description'] != 'different'
    assert importer.import_csv('housinglist', payload([changed]), 13)[0]['status'] == 'imported'


@pytest.mark.parametrize('change', [
    {'source_url': 'file:///etc/passwd'}, {'source_url': 'https://user:secret@example.org'},
    {'source_url': 'https://example.org/a b'}, {'source_url': ''},
    {'description': ''}, {'description': 'NULL'}, {'source_date': 'yesterday'},
    {'salary': 'NaN'}, {'is_job_listing': 'yes'}, {'remote': 'maybe'},
    {'message_id': '123'}, {'id': '22'}, {'processed_message_id': '12'},
    {'description': 'x'*20001},
])
def test_invalid_rows_reject_whole_file_before_writing(import_db, change):
    good = listing(); bad = {**good, **change}
    with pytest.raises(ValueError):
        importer.import_csv('joblist', payload([good, bad], list(bad)), 12)
    assert backoffice_data.page('joblist')['total'] == 0


def test_types_nulls_original_text_and_source_dates(import_db):
    data = payload([listing(salary='21.5', is_job_listing='true', remote='0', city='NULL',
                            raw_text='<script>alert(1)</script>\nمتن', source_date='2026-10-01')])
    importer.import_csv('joblist', data, 12)
    row = backoffice_data.page('joblist')['rows'][0]
    assert row['salary'] == 21.5 and row['is_job_listing'] == 1 and row['remote'] == 0
    assert row['city'] is None
    source = backoffice_data.page('external_category_sources')['rows'][0]
    assert source['raw_text'] == '<script>alert(1)</script>\nمتن' and source['source_date'] == '2026-10-01'


def test_limits_bad_csv_and_duplicate_headers(import_db):
    for data in [b'', b'\xff', b'a,a\n1,2\n', b'source_url,description\n"unterminated',
                 b'x'*(importer.MAX_BYTES+1), payload([listing()] * 501),
                 b'source_url,description\nhttps://example.org,a,extra\n']:
        with pytest.raises(ValueError):
            importer.parse_csv('joblist', data)
    with pytest.raises(ValueError):
        importer.parse_csv('messages', payload([listing()]))


def test_database_failure_rolls_back_category_and_provenance(import_db):
    conn = database.get_connection()
    conn.execute("""CREATE TRIGGER reject_source BEFORE INSERT ON external_category_sources
        WHEN NEW.source_url LIKE '%bad%' BEGIN SELECT RAISE(ABORT,'test'); END""")
    conn.commit(); conn.close()
    other = listing(); other['source_url'] = 'https://example.org/bad'
    with pytest.raises(sqlite3.IntegrityError):
        importer.import_csv('joblist', payload([listing(), other]), 12)
    assert backoffice_data.page('joblist')['total'] == 0
    assert backoffice_data.page('external_category_sources')['total'] == 0


def test_nullable_migration_preserves_legacy_records_and_foreign_keys(import_db):
    conn = database.get_connection()
    try:
        conn.execute("INSERT INTO messages(id,channel_username,message_id,date) VALUES(70,'real',123,'2026-10-01')")
        for category, fields in database.CATEGORY_TABLES.items():
            conn.execute(f'DROP TABLE {category}')
            columns = ','.join(f'{key} {kind}' for key, kind in fields.items())
            conn.execute(f'''CREATE TABLE {category}(id INTEGER PRIMARY KEY AUTOINCREMENT,
                processed_message_id INTEGER NOT NULL UNIQUE,{columns},created_at TEXT,
                FOREIGN KEY(processed_message_id) REFERENCES messages(id) ON DELETE CASCADE)''')
            conn.execute(f"INSERT INTO {category}(id,processed_message_id,description,created_at) VALUES(93,70,'unchanged','original')")
        conn.commit()
    finally:
        conn.close()
    database.initialize_db()
    database.initialize_db()
    conn = database.get_connection()
    try:
        for category in database.CATEGORY_TABLES:
            row = conn.execute(f'SELECT id,processed_message_id,description,created_at FROM {category}').fetchone()
            assert tuple(row) == (93, 70, 'unchanged', 'original')
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'INSERT INTO {category}(processed_message_id) VALUES(9999)')
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'INSERT INTO {category}(processed_message_id) VALUES(70)')
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_authenticated_preview_apply_and_csrf(import_db):
    client = TestClient(TestServer(backoffice_web.create_app()))
    await client.start_server()
    try:
        assert (await client.get('/data/import')).status == 401
        conn = database.get_connection()
        conn.execute('INSERT INTO backoffice_sessions VALUES(?,?,?,?)',
                     (backoffice_web._digest('local-import-session'), 1485409432, 'csrf',
                      (backoffice_web._now() + timedelta(hours=1)).isoformat()))
        conn.commit(); conn.close()
        headers = {'Cookie': 'telclaw_admin=local-import-session'}
        assert (await client.get('/data/import', headers=headers)).status == 200
        template = await client.get('/data/import/template?category=joblist', headers=headers)
        assert template.status == 200 and 'source_url' in await template.text()
        form = FormData()
        form.add_field('csrf', 'csrf'); form.add_field('category', 'joblist')
        data = payload([listing(raw_text='<script>bad()</script>')])
        form.add_field('file', data, filename='jobs.csv', content_type='text/csv')
        preview = await client.post('/data/import', data=form, headers=headers)
        assert preview.status == 200
        markup = await preview.text()
        assert '&lt;script&gt;bad()&lt;/script&gt;' in markup
        assert '<script>bad()' not in markup
        assert backoffice_data.page('joblist')['total'] == 0
        encoded = re.search(r'name="payload" value="([^"]+)"', markup)[1]
        assert base64.b64decode(encoded) == data
        apply = dict(csrf='wrong', category='joblist', action='apply', payload=encoded)
        assert (await client.post('/data/import', data=apply, headers=headers)).status == 403
        apply['csrf'] = 'csrf'
        assert (await client.post('/data/import', data=apply, headers=headers)).status == 200
        assert backoffice_data.page('joblist')['total'] == 1
        assert (await client.post('/data/import', data=apply, headers=headers)).status == 200
        assert backoffice_data.page('joblist')['total'] == 1
        source = await client.get('/data?table=external_category_sources', headers=headers)
        assert source.status == 200
    finally:
        await client.close()
