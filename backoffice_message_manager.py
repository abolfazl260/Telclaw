"""Authenticated routes; publishing is queued, never performed by HTTP handlers."""
import asyncio
import sqlite3
from urllib.parse import unquote, urlencode
import json

from aiohttp import web
import aiohttp
from message_manager.telegram_sender import PreflightRetry
import config
import routing_rules
from backoffice_message_manager_ui import render
from message_manager.validation import ValidationError

MM_KEY = web.AppKey('message_manager', object)


def actor(request):
    return int(request['session']['admin_id'])


def confirmed(form):
    return form.get('confirmed') == 'yes'


def redirect(message_id=None, batch_id=None, view=None, notice=''):
    location = '/message-manager'
    if message_id:
        location += '?id=' + str(int(message_id))
    elif view:
        location += "?view=" + view
    elif batch_id:
        location += '?batch=' + str(batch_id)
    if notice:
        location += ('&' if '?' in location else '?') + urlencode({'notice':notice})
    raise web.HTTPSeeOther(location)


async def page(request, *, draft=None, error=''):
    mm = request.app[MM_KEY]
    try:
        message = mm.repo.get_message(request.query['id']) if request.query.get('id') else None
        batch = mm.excel.batch(request.query['batch']) if request.query.get('batch') else None
        body = render(mm, request['session']['csrf'], request['csp_nonce'], request.query, message, draft, batch, error)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.Response(text=body, content_type='text/html')


async def action(request):
    mm, command = request.app[MM_KEY], request.match_info['action']
    data = await request.post()
    admin = actor(request)
    try:
        mid = int(data['id']) if data.get('id') else None
        if command == 'save':
            redirect(mm.service.save(data, admin), notice='save')
        elif command == 'preview':
            payload = mm.service.payload(data)
            payload['title'] = str(data.get('title') or '')
            # Render current draft data without saving or publishing it.
            if data.get('fragment') == 'yes':
                from backoffice_message_manager_ui import preview
                return web.json_response({'html': preview(payload, mm.repo)})
            query = {'id': str(mid)} if mid else {'view': 'editor'}
            message = mm.repo.get_message(mid) if mid else None
            return web.Response(text=render(mm, request['session']['csrf'], request['csp_nonce'], query, message, payload), content_type='text/html')
        elif command in {'publish', 'schedule'}:
            await mm.service.activate(mid, data, data.getall('targets', []), admin, confirmed(data),
                immediate=command == 'publish', replace_id=int(data['replace_id']) if data.get('replace_id') else None)
            redirect(mid, notice=command)
        elif command == 'duplicate':
            redirect(mm.service.duplicate(mid, admin), notice=command)
        elif command == 'archive':
            if not confirmed(data):
                raise ValidationError('Confirm archiving and cancellation')
            mm.repo.archive(mid, admin)
            redirect(mid, notice=command)
        elif command == 'schedule-action':
            mm.repo.schedule_action(int(data['schedule_id']), data.get('action'), admin)
            redirect(mid, notice=command)
        elif command == 'retry':
            mm.repo.manual_retry(int(data['delivery_id']), admin, confirmed(data))
            redirect(mid, notice=command)
        elif command == 'target':
            target_id = int(data['target_id']) if data.get('target_id') else None
            enabled = data.get('enabled', 'yes') == 'yes'
            if target_id is None:
                routing_rules.save_target(str(data.get('label',''))[:200], str(data.get('chat_id','')), enabled=False)
                target_id = next(t['id'] for t in mm.repo.targets() if t['chat_id'] == data['chat_id'].strip())
            target = next((t for t in mm.repo.targets() if t['id'] == target_id), None)
            if not target:
                raise ValidationError('Unknown destination')
            if enabled:
                detail = await mm.sender.check_target(target)
                routing_rules.update_target_connection(target_id, 'connected', detail)
            mm.repo.eligible(target_id, enabled, admin)
            redirect(view='channels', notice=command)
        elif command == 'import':
            upload = data.get('workbook')
            if not isinstance(upload, web.FileField) or not upload.filename.lower().endswith('.xlsx'):
                raise ValidationError('Select an XLSX file')
            raw = upload.file.read(config.MESSAGE_MANAGER_XLSX_MAX_BYTES + 1)
            batch_id, _ = await asyncio.to_thread(mm.excel.preview, raw, str(data.get('namespace','')), admin)
            redirect(batch_id=batch_id)
        elif command == 'import-confirm':
            await asyncio.to_thread(mm.excel.confirm, data.get('batch_id'), admin, confirmed(data))
            redirect(batch_id=data.get('batch_id'), notice=command)
        elif command == 'import-activate':
            await mm.excel.activate(data.get('batch_id'), admin, confirmed(data))
            redirect(batch_id=data.get('batch_id'), notice=command)
        else:
            raise web.HTTPNotFound()
    except (aiohttp.ClientError, TimeoutError, PreflightRetry) as exc:
        return form_error(request, data, command, 'Telegram permission check unavailable; try again later', 'targets')
    except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as exc:
        return form_error(request, data, command, str(exc)[:2000])


def form_error(request, data, command, error, field=None):
    mm = request.app[MM_KEY]
    if command == 'preview' and data.get('fragment') == 'yes':
        return web.Response(text=error, status=400)
    if field is None:
        lower = error.lower()
        field = ('buttons' if any(x in lower for x in ['button','url','keyboard','link']) else
                 'asset_key' if 'media' in lower else 'title' if 'title' in lower else
                 'targets' if any(x in lower for x in ['destination','permission','activate','channel']) else
                 'date' if command == 'schedule' and 'date' in lower else 'time' if command == 'schedule' else
                 'namespace' if command.startswith('import') and 'namespace' in lower else 'workbook' if command.startswith('import') else
                 'target_id' if command == 'target' and data.get('target_id') else 'chat_id' if command == 'target' else 'text')
    query = {'view': 'channels' if command == 'target' else 'imports' if command.startswith('import') else 'editor'}
    message = None
    try:
        if data.get('id'):
            message = mm.repo.get_message(int(data['id']))
    except (ValueError, TypeError):
        pass
    draft = None
    if command in {'save','preview'}:
        try:
            keyboard = json.loads(data.get('buttons') or '[]')
            from message_manager.validation import buttons
            keyboard = buttons(keyboard)
        except (ValueError, TypeError):
            keyboard = []
        asset = data.get('asset_key') or ''
        try:
            if asset: mm.repo.media(asset)
        except ValueError:
            asset = ''
        draft = dict(title=data.get('title',''), text=data.get('text',''), parse_mode=data.get('parse_mode') or 'HTML',
                     asset_key=asset, buttons=keyboard, buttons_raw=data.get('buttons') or '[]')
    values = {key: data.getall(key) for key in data if key != 'csrf' and isinstance(data.get(key), str)}
    feedback = dict(action=command, values=values, errors={field:error})
    batch = None
    try:
        if data.get('batch_id'): batch = mm.excel.batch(data['batch_id'])
    except ValueError:
        pass
    body = render(mm, request['session']['csrf'], request['csp_nonce'], query, message, draft, batch,
                  error=error, feedback=feedback)
    return web.Response(text=body, content_type='text/html', status=400)


async def upload_media(request):
    # Security middleware verifies header CSRF before the streaming body is consumed.
    mm = request.app[MM_KEY]
    if request.content_length and request.content_length > mm.media.max_bytes:
        raise web.HTTPRequestEntityTooLarge(max_size=mm.media.max_bytes, actual_size=request.content_length)
    try:
        asset = await mm.media.upload(unquote(request.headers.get('X-File-Name','')), request.content_type,
            request.content.iter_chunked(65536), actor(request))
    except ValidationError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response({'asset_key': asset['asset_key'], 'sha256': asset['sha256'], 'kind': asset['kind'], 'name': asset['original_name']})


async def media_file(request):
    mm = request.app[MM_KEY]
    try:
        asset = mm.repo.media(request.match_info['key'])
        path = mm.media.path(asset)
    except ValidationError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    response = web.FileResponse(path)
    response.content_type = asset['mime']
    response.headers['Content-Disposition'] = ('inline' if asset['kind'] in {'photo','video'} else 'attachment') + '; filename="' + asset['filename'] + '"'
    return response


async def download(request):
    from message_manager.excel_service import template
    name = request.match_info['name']
    payload = await asyncio.to_thread(template if name == 'template' else request.app[MM_KEY].excel.export)
    return web.Response(body=payload, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="message-manager-{name}.xlsx"'})


def install(app, runtime=None):
    if not config.MESSAGE_MANAGER_ENABLED:
        return
    from message_manager.runtime import build_runtime
    app[MM_KEY] = runtime or build_runtime()
    app.add_routes([web.get('/message-manager', page),
        web.post('/message-manager/media', upload_media), web.get('/message-manager/media/{key}', media_file),
        web.get('/message-manager/{name:template|status}.xlsx', download),
        web.post('/message-manager/{action}', action)])
