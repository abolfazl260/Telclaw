"""Bounded XLSX validation, persisted previews, confirmed drafts, separate activation."""
import hashlib
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime, date, time
from zipfile import ZipFile, BadZipFile
from xml.etree.ElementTree import ParseError
from defusedxml.common import DefusedXmlException

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
import config
from . import recurrence
from .models import stamp, utcnow
from .validation import ValidationError, buttons

MESSAGE_HEADERS = ['message_key', 'channels', 'message', 'date', 'time', 'recurrence',
                   'title', 'asset_key', 'weekdays', 'interval', 'end_date', 'end_count', 'parse_mode']
BUTTON_HEADERS = ['message_key', 'button_text', 'button_url', 'row', 'order']


def _string(value):
    if isinstance(value, (date, time, datetime)):
        return value.isoformat()
    return str(value) if value is not None else ''


def workbook_bytes(book):
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def template():
    book = Workbook()
    sheet = book.active
    sheet.title = 'Messages'
    sheet.append(MESSAGE_HEADERS)
    sheet['B1'].comment = Comment('Semicolon-separated stored @channels or target IDs. No new targets are created by import.', 'Telclaw')
    sheet['D1'].comment = Comment('Gregorian YYYY-MM-DD in Asia/Tehran; time HH:MM.', 'Telclaw')
    sheet['F1'].comment = Comment('once, daily, weekly, days, weeks. Weekdays: 0=Monday,...,6=Sunday. interval is used by days/weeks.', 'Telclaw')
    sheet['H1'].comment = Comment('Use an asset key from an authenticated Message Manager upload. URLs are never fetched.', 'Telclaw')
    book.create_sheet('Buttons').append(BUTTON_HEADERS)
    for ws in book:
        ws.freeze_panes = 'A2'
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 22
    return workbook_bytes(book)


class ExcelService:
    def __init__(self, service):
        self.service, self.repo = service, service.repo

    def _read(self, raw):
        if len(raw) > config.MESSAGE_MANAGER_XLSX_MAX_BYTES:
            raise ValidationError('XLSX upload is too large')
        try:
            with ZipFile(io.BytesIO(raw)) as archive:
                info = archive.infolist()
                if len(info) > 2000 or sum(x.file_size for x in info) > 32 * 1024 * 1024:
                    raise ValidationError('XLSX expands beyond the safe limit')
                if any(x.filename.endswith('vbaProject.bin') for x in info):
                    raise ValidationError('Macro workbooks are not supported')
            book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        except ValidationError:
            raise
        except (BadZipFile, KeyError, ValueError, OSError, ParseError, DefusedXmlException) as exc:
            raise ValidationError('Invalid XLSX workbook') from exc
        try:
            if set(book.sheetnames) != {'Messages', 'Buttons'}:
                raise ValidationError('Workbook must contain exactly Messages and Buttons sheets')
            result = {}
            for name, required, allowed, limit in (
                ('Messages', MESSAGE_HEADERS[:6], MESSAGE_HEADERS, config.MESSAGE_MANAGER_XLSX_MAX_ROWS),
                ('Buttons', BUTTON_HEADERS[:3], BUTTON_HEADERS, config.MESSAGE_MANAGER_XLSX_MAX_ROWS * 8),
            ):
                ws = book[name]
                # Do not trust client-supplied worksheet dimensions.
                ws.reset_dimensions()
                rows = ws.iter_rows(max_col=len(allowed) + 1)
                header_cells = next(rows, ())
                header = [_string(c.value).strip() for c in header_cells]
                while header and not header[-1]:
                    header.pop()
                if not set(required).issubset(header) or len(header) != len(set(header)) or any(h not in allowed for h in header):
                    raise ValidationError(f'{name}: invalid, duplicate, or missing headers')
                parsed = []
                for row_number, row in enumerate(rows, 2):
                    if row_number > limit + 1:
                        raise ValidationError(f'{name}: row limit is {limit}')
                    if not any(c.value is not None for c in row):
                        continue
                    values = {_string(header[i]): c.value for i, c in enumerate(row[:len(header)])}
                    errors = []
                    if any(c.data_type == 'f' for c in row):
                        errors.append('Formulas are not accepted')
                    if any(len(_string(c.value)) > 32768 for c in row):
                        errors.append('Cell exceeds maximum length')
                    if any(c.value is not None for c in row[len(header):]):
                        errors.append('Unexpected data outside declared headers')
                    parsed.append((row_number, values, errors))
                result[name] = parsed
            return result
        except (ParseError, DefusedXmlException, BadZipFile, KeyError, RuntimeError) as exc:
            raise ValidationError("Invalid XLSX worksheet contents") from exc
        finally:
            book.close()

    def preview(self, raw, namespace, actor):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', namespace or ''):
            raise ValidationError('Import namespace must contain 1–64 letters, digits, _ or -')
        sheets = self._read(raw)
        targets = {str(t['id']): t for t in self.repo.targets() if t['mm_eligible']}
        targets.update({t['chat_id'].lower(): t for t in list(targets.values())})
        rows, keys, button_rows = [], set(), {}
        for row_number, values, errors in sheets['Messages']:
            key = _string(values.get('message_key')).strip()
            entry = {'row': row_number, 'key': key, 'errors': list(errors), 'title': _string(values.get('title') or key)}
            try:
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', key):
                    raise ValidationError('message_key needs 1–100 letters, digits, _, . or -')
                if key in keys:
                    raise ValidationError('Duplicate message_key in workbook')
                keys.add(key)
                with self.repo.connection() as c:
                    if c.execute('SELECT 1 FROM mm_messages WHERE import_namespace=? AND import_key=?', (namespace, key)).fetchone():
                        raise ValidationError('message_key already imported in this namespace')
                channels = [x.strip().lower() for x in _string(values.get('channels')).split(';') if x.strip()]
                if not channels or len(channels) > 50 or any(x not in targets for x in channels):
                    raise ValidationError('Unknown/ineligible channel; use stored target IDs or @channels')
                entry['target_ids'] = sorted({targets[x]['id'] for x in channels})
                entry['channels'] = [targets[x]['chat_id'] for x in channels]
                raw_time = values.get('time')
                if isinstance(raw_time, datetime):
                    raw_time = raw_time.time()
                spec = {name: _string(values.get(name)) for name in ['date', 'recurrence', 'weekdays', 'interval', 'end_date', 'end_count']}
                spec['date'] = spec['date'][:10]
                spec['end_date'] = spec['end_date'][:10]
                spec['time'] = _string(raw_time)
                entry['spec'] = recurrence.normalize(spec)
                entry['content'] = self.service.payload(dict(text=_string(values.get('message')), parse_mode=values.get('parse_mode') or 'HTML', asset_key=values.get('asset_key') or '', buttons=[]))
                if not 1 <= len(entry['title']) <= 200:
                    raise ValidationError('Title must contain 1–200 characters')
            except (ValueError, TypeError) as exc:
                entry['errors'].append(str(exc))
            rows.append(entry)
        for row_number, values, errors in sheets['Buttons']:
            key = _string(values.get('message_key')).strip()
            try:
                if errors:
                    raise ValidationError('; '.join(errors))
                if key not in keys:
                    raise ValidationError('Button has no matching message_key')
                row = int(values.get('row') or 1)
                entries = button_rows.setdefault(key, {})
                order = int(values.get('order') or (1 + max((p for r, p in entries if r == row), default=0)))
                if not 1 <= row <= 20 or not 1 <= order <= 8:
                    raise ValidationError('Button row/order is outside supported limits')
                if (row, order) in entries:
                    raise ValidationError('Duplicate button row/order')
                button = {'text': _string(values.get('button_text')), 'url': _string(values.get('button_url'))}
                buttons([[button]])
                entries[row, order] = button
            except (ValueError, TypeError) as exc:
                rows.append({'sheet': 'Buttons', 'row': row_number, 'key': key, 'errors': [str(exc)]})
        for row in rows:
            if row.get('sheet') == 'Buttons' or row['errors']:
                continue
            entries = button_rows.get(row['key'], {})
            rows_by_number = sorted({r for r, _ in entries})
            try:
                row['content']['buttons'] = buttons([[entries[r, p] for rr, p in sorted(entries) if rr == r] for r in rows_by_number])
            except ValidationError as exc:
                row['errors'].append(str(exc))
        if not sheets['Messages']:
            raise ValidationError('Workbook contains no messages')
        preview = {'rows': rows, 'errors': sum(bool(r['errors']) for r in rows)}
        batch_id = uuid.uuid4().hex
        with self.repo.connection(True) as c:
            c.execute('INSERT INTO mm_import_batches(id,namespace,sha256,preview_json,created_by,created_at) VALUES(?,?,?,?,?,?)',
                      (batch_id, namespace, hashlib.sha256(raw).hexdigest(), json.dumps(preview), actor, stamp()))
            self.repo.audit(c, actor, 'import_preview', batch_id, preview['errors'])
        return batch_id, preview

    def batch(self, batch_id):
        with self.repo.connection() as c:
            row = c.execute('SELECT * FROM mm_import_batches WHERE id=?', (batch_id,)).fetchone()
            if not row:
                raise ValidationError('Import preview not found')
            result = dict(row)
            result['preview'] = json.loads(row['preview_json'])
            return result

    def confirm(self, batch_id, actor, confirmed=False):
        if not confirmed:
            raise ValidationError('Confirm creation of imported drafts')
        batch = self.batch(batch_id)
        if batch['preview']['errors']:
            raise ValidationError('Correct all row errors before importing')
        try:
            with self.repo.connection(True) as c:
                current = c.execute('SELECT state FROM mm_import_batches WHERE id=?', (batch_id,)).fetchone()
                if current['state'] == 'confirmed':
                    return
                for row in batch['preview']['rows']:
                    payload = self.service.payload(row['content'])
                    self.repo.save_message(row['title'], payload, actor, conn=c, namespace=batch['namespace'], key=row['key'])
                c.execute("UPDATE mm_import_batches SET state='confirmed',confirmed_at=? WHERE id=?", (stamp(), batch_id))
                self.repo.audit(c, actor, 'import_drafts_confirmed', batch_id)
        except sqlite3.IntegrityError as exc:
            raise ValidationError('Duplicate import keys; no drafts were created') from exc

    async def activate(self, batch_id, actor, confirmed=False):
        if not confirmed:
            raise ValidationError('Confirm activation of all imported schedules')
        batch = self.batch(batch_id)
        if batch['state'] != 'confirmed' or batch['activated_at']:
            raise ValidationError('Import must be confirmed once, then activated once')
        rows = batch['preview']['rows']
        for row in rows:
            if recurrence.at(row['spec'], 0) <= utcnow():
                raise ValidationError(f"{row['key']}: start time has passed; schedule its draft manually")
        # Check each distinct channel once, rather than once per spreadsheet row.
        target_ids = {tid for row in rows for tid in row['target_ids']}
        checked = {t['id']: t for t in await self.service.checked_targets(target_ids)}
        prepared = [(row, [checked[tid] for tid in row['target_ids']]) for row in rows]
        with self.repo.connection(True) as c:
            current = c.execute('SELECT activated_at FROM mm_import_batches WHERE id=?', (batch_id,)).fetchone()
            if current['activated_at']:
                raise ValidationError('Import has already been activated')
            for row, targets in prepared:
                message = c.execute('SELECT * FROM mm_messages WHERE import_namespace=? AND import_key=?', (batch['namespace'], row['key'])).fetchone()
                if not message or message['version'] != 1 or json.loads(message['content_json']) != row['content']:
                    raise ValidationError('An imported draft has changed; schedule it individually')
                self.repo.schedule(message['id'], row['spec'], targets, actor, c)
            c.execute('UPDATE mm_import_batches SET activated_at=? WHERE id=?', (stamp(), batch_id))
            self.repo.audit(c, actor, 'import_schedules_activated', batch_id)

    def export(self):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Deliveries'
        headers = ['message_id', 'title', 'schedule_id', 'due_utc', 'channel', 'status', 'attempts', 'telegram_message_id', 'message_link', 'next_retry', 'error']
        sheet.append(headers)
        with self.repo.connection() as c:
            rows = c.execute('''SELECT m.id,m.title,s.id,o.due_at,d.chat_id,d.status,d.attempts,d.telegram_message_id,d.message_link,d.next_retry,d.error
              FROM mm_deliveries d JOIN mm_occurrences o ON o.id=d.occurrence_id JOIN mm_schedules s ON s.id=o.schedule_id
              JOIN mm_messages m ON m.id=s.message_id ORDER BY d.id DESC LIMIT 10000''')
            for row in rows:
                # Force text cells and escape dangerous prefixes, including leading whitespace.
                values = []
                for value in row:
                    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                        value = "'" + value
                    values.append(value)
                sheet.append(values)
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = 's'
        sheet.freeze_panes = 'A2'
        return workbook_bytes(book)
