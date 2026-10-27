"""Bounded CSV import of structured website listings, without Telegram identities."""
import asyncio
import base64
import binascii
import csv
import html
import io
import math
import sqlite3
from datetime import datetime
from urllib.parse import urlsplit

from aiohttp import web
from storage.database import CATEGORY_TABLES, get_connection

MAX_BYTES = 512 * 1024
MAX_ROWS = 500
METADATA = ('source_url', 'source_date', 'raw_text')


def parse_csv(category, payload):
    if category not in CATEGORY_TABLES:
        raise ValueError('دسته‌بندی نامعتبر است.')
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError('فایل باید حداکثر ۵۱۲ کیلوبایت باشد.')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('فایل باید CSV با کدگذاری UTF-8 باشد.') from exc
    reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
    allowed = {*CATEGORY_TABLES[category], *METADATA}
    try:
        headers = reader.fieldnames or []
        if not headers or len(set(headers)) != len(headers) or any(h not in allowed for h in headers):
            raise ValueError('ستون ناشناخته یا تکراری؛ فایل قالب همین دسته را دریافت کن.')
        if not {'source_url', 'description'} <= set(headers):
            raise ValueError('ستون‌های source_url و description الزامی هستند.')
        rows = []
        for index, raw in enumerate(reader, 2):
            if len(rows) >= MAX_ROWS:
                raise ValueError('در هر فایل حداکثر ۵۰۰ ردیف مجاز است.')
            try:
                if None in raw or any(v is None for v in raw.values()):
                    raise ValueError('تعداد سلول‌ها با ستون‌ها برابر نیست.')
                if any(len(v) > 20000 or '\x00' in v for v in raw.values()):
                    raise ValueError('مقدار سلول نامعتبر یا بیشتر از ۲۰۰۰۰ نویسه است.')
                source = raw['source_url'].strip()
                url = urlsplit(source)
                if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or any(c.isspace() for c in source):
                    raise ValueError('source_url باید آدرس کامل HTTP یا HTTPS بدون اطلاعات ورود باشد.')
                # Fragments identify locations inside one document, not new ads.
                source = url._replace(fragment='').geturl()
                if not raw['description'].strip() or raw['description'].strip().upper() == 'NULL':
                    raise ValueError('description نباید خالی باشد.')
                source_date = raw.get('source_date', '').strip()
                if source_date.upper() == 'NULL':
                    source_date = ''
                if source_date:
                    datetime.fromisoformat(source_date.replace('Z', '+00:00'))
                data = {}
                for field, kind in CATEGORY_TABLES[category].items():
                    value = raw.get(field, '')
                    if not value.strip() or value.strip().upper() == 'NULL':
                        data[field] = None
                    elif field in ('is_job_listing', 'remote'):
                        flag = value.strip().lower()
                        if flag not in ('true', 'false', '1', '0'):
                            raise ValueError(f'{field}: فقط true/false یا 1/0 مجاز است.')
                        data[field] = int(flag in ('true', '1'))
                    elif kind.startswith('INTEGER'):
                        number = int(value)
                        if not -(2**63) <= number < 2**63:
                            raise ValueError(f'{field}: عدد خارج از محدوده است.')
                        data[field] = number
                    elif kind.startswith('REAL'):
                        number = float(value)
                        if not math.isfinite(number):
                            raise ValueError(f'{field}: عدد باید متناهی باشد.')
                        data[field] = number
                    else:
                        data[field] = value
                raw_text = raw.get('raw_text', '')
                if not raw_text.strip() or raw_text.strip().upper() == 'NULL':
                    raw_text = None
                rows.append(dict(line=index, source_url=source, source_date=source_date or None,
                                 raw_text=raw_text, data=data))
            except ValueError as exc:
                raise ValueError(f'ردیف {index}: {exc}') from exc
    except csv.Error as exc:
        raise ValueError('ساختار CSV نامعتبر است.') from exc
    if not rows:
        raise ValueError('فایل هیچ ردیف داده‌ای ندارد.')
    return rows


def import_csv(category, payload, admin_id):
    rows = parse_csv(category, payload)
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        results = []
        for row in rows:
            existing = conn.execute('SELECT category_record_id FROM external_category_sources WHERE category=? AND source_url=?',
                                    (category, row['source_url'])).fetchone()
            if existing:
                results.append(dict(line=row['line'], id=existing[0], status='duplicate'))
                continue
            fields = list(row['data'])
            cursor = conn.execute(f"INSERT INTO {category} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                  list(row['data'].values()))
            record_id = cursor.lastrowid
            conn.execute('''INSERT INTO external_category_sources
                (category,category_record_id,source_url,source_date,raw_text,imported_by)
                VALUES(?,?,?,?,?,?)''', (category, record_id, row['source_url'], row['source_date'], row['raw_text'], admin_id))
            results.append(dict(line=row['line'], id=record_id, status='imported'))
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _page(request, content='', status=200):
    esc = html.escape
    csrf = esc(request['session']['csrf'], quote=True)
    options = ''.join(f'<option value="{c}">{c}</option>' for c in CATEGORY_TABLES)
    templates = ' · '.join(f'<a href="/data/import/template?category={c}">{c}</a>' for c in CATEGORY_TABLES)
    return web.Response(text=f'''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Telclaw · ورود اطلاعات وبسایت</title><style>body{{font:16px system-ui;max-width:1100px;margin:auto;padding:24px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:8px}}td{{white-space:pre-wrap;overflow-wrap:anywhere}}button{{padding:10px;margin:12px}}.scroll{{overflow:auto}}</style>
<nav><a href="/data">دیتابیس</a> · <a href="/data?table=external_category_sources">منابع واردشده</a> · <a href="/">انتشار</a> · <a href="/assistant">دستیار تحلیل</a></nav>
<h1>ورود اطلاعات استخراج‌شده از وبسایت</h1><p>فایل CSV با کدگذاری UTF-8؛ حداکثر ۵۰۰ ردیف و ۵۱۲ کیلوبایت. دریافت قالب: {templates}</p>
<p>source_url و description الزامی؛ source_date تاریخ اصلی آگهی به صورت ISO است و اختیاری است. raw_text متن اصلی اختیاری است.
نام سایر ستون‌ها باید مطابق قالب باشد. سلول خالی یا NULL مقدار تهی می‌شود. شناسه‌ها را وارد نکن.</p>
<p>اطلاعات فقط ذخیره می‌شود و خودکار منتشر یا برای AI ارسال نمی‌شود. شناسهٔ پیام و کاربر تلگرام ندارد.
آدرس تکراری در همان دسته رد می‌شود و رکورد قبلی تغییر نمی‌کند. هر آدرس باید متعلق به یک آگهی باشد.</p>
<form method="post" action="/data/import" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{csrf}">
<select name="category">{options}</select><input type="file" name="file" accept=".csv,text/csv" required>
<button>بررسی و پیش‌نمایش</button></form>{content}</html>''', content_type='text/html', status=status)


async def import_page(request):
    return _page(request)


async def import_template(request):
    category = request.query.get('category', '')
    if category not in CATEGORY_TABLES:
        raise web.HTTPBadRequest(text='Invalid category')
    output = io.StringIO(newline='')
    csv.writer(output).writerow([*METADATA, *CATEGORY_TABLES[category]])
    return web.Response(body=('\ufeff' + output.getvalue()).encode('utf-8'), content_type='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="telclaw-{category}-import.csv"'})


async def import_upload(request):
    form = await request.post()
    category = str(form.get('category', ''))
    try:
        apply = str(form.get('action', '')) == 'apply'
        if apply:
            encoded = str(form.get('payload', ''))
            if len(encoded) > (MAX_BYTES + 2) // 3 * 4:
                raise ValueError('فایل بیش از حد بزرگ است.')
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError('فایل پیش‌نمایش نامعتبر است.') from exc
        else:
            upload = form.get('file')
            if not isinstance(upload, web.FileField):
                raise ValueError('فایل CSV را انتخاب کن.')
            payload = upload.file.read(MAX_BYTES + 1)
        rows = parse_csv(category, payload)
        esc = html.escape
        if apply:
            results = await asyncio.to_thread(import_csv, category, payload, request['session']['admin_id'])
            count = sum(r['status'] == 'imported' for r in results)
            body = f'<h2>ثبت‌شده: {count} · تکراری: {len(results)-count}</h2><p>این تعداد به معنی انتشار نیست.</p><ul>'
            for row in results:
                link = f'/data?table={category}&op_id=eq&f_id={row["id"]}'
                label = 'ثبت شد' if row['status'] == 'imported' else 'تکراری؛ تغییر نکرد'
                body += f'<li>ردیف {row["line"]}: {label} · <a href="{link}">شناسه {row["id"]}</a></li>'
            body += '</ul>'
        else:
            columns = [*METADATA, *CATEGORY_TABLES[category]]
            body = f'<h2>پیش‌نمایش: {len(rows)} ردیف معتبر</h2><p>تکراری‌ها هنگام ثبت کنار گذاشته می‌شوند.</p><div class="scroll"><table><tr>'
            body += ''.join(f'<th>{c}</th>' for c in columns) + '</tr>'
            for row in rows[:5]:
                values = {**row['data'], **{key: row[key] for key in METADATA}}
                body += '<tr>' + ''.join(f'<td>{esc(str(values[c])) if values[c] is not None else "NULL"}</td>' for c in columns) + '</tr>'
            body += '</table></div><p>نمایش پنج ردیف اول؛ همهٔ ردیف‌ها بررسی شده‌اند.</p>'
            body += f'''<form method="post" action="/data/import"><input type="hidden" name="csrf" value="{esc(request['session']['csrf'], quote=True)}">
<input type="hidden" name="category" value="{esc(category, quote=True)}"><input type="hidden" name="action" value="apply">
<input type="hidden" name="payload" value="{base64.b64encode(payload).decode('ascii')}"><button>ثبت در دیتابیس</button></form>'''
        return _page(request, body)
    except ValueError as exc:
        return _page(request, f'<p role="alert">{html.escape(str(exc))}</p>', 400)
    except sqlite3.Error:
        return _page(request, '<p role="alert">ثبت انجام نشد؛ تغییرات این فایل برگشت داده شد. دیتابیس را بررسی کن.</p>', 503)
