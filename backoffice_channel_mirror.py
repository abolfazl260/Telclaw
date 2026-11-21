"""Authenticated, CSRF-protected mirror administration; handlers never publish."""
import asyncio
from html import escape
import json
import sqlite3

from aiohttp import web
from telethon import utils, types

import config
from backoffice_layout import SHARED_STYLE, page_header
from channel_mirror import Repository, ValidationError
from channel_mirror.admin import redact
from channel_mirror.bot_api import BotAPI, APIError
from channel_mirror.models import caption, stamp
from channel_mirror.telegram_sender import Sender
from services.telegram_session_guard import session_guard

KEY = web.AppKey('channel_mirror', object)


def e(value):
    return escape(str(value if value is not None else ''), quote=True)


class Administration:
    def __init__(self, repo, operations):
        self.repo, self.operations = repo, operations

    async def validate(self, source, destination, username, footer):
        caption(username, footer)
        client = getattr(self.operations.console_ui, 'client', None) or self.operations._client
        if client is None:
            raise ValidationError('Connect an authorized crawler account in Operations first')
        async with session_guard(client):
            entity = await asyncio.wait_for(client.get_entity(source), 15)
        if not isinstance(entity, types.Channel) or getattr(entity, 'noforwards', False):
            raise ValidationError('Source must be an accessible, non-protected Telegram channel')
        source_id = utils.get_peer_id(entity)
        api = BotAPI(config.TELEGRAM_BOT_TOKEN)
        if not config.TELEGRAM_BOT_TOKEN:
            raise ValidationError('Configure the Telegram bot token first')
        chat = await api.call('getChat', {'chat_id': destination})
        target_id = chat['id']
        await Sender(self.repo, api).check_target({'destination_chat_id': target_id,
            'destination_username': username, 'caption': caption(username, footer)})
        return source_id, target_id

    def statuses(self):
        job = self.operations.crawler.scheduler.crawl_job
        statuses = {r['id']: r['crawl_status'] for r in job.mirror_status()}
        names = {str(r.get('username', '')).lower().lstrip('@') for cat in self.operations.channels.categories()
                 for r in self.operations.channels.channels_for_category(cat) if isinstance(r, dict)}
        return statuses, names


def form(csrf, action, fields, label, confirm=False):
    check = '<label><input type="checkbox" name="confirmed" value="yes" required> Confirm this action</label>' if confirm else ''
    return f'<form method="post" action="/channel-mirror/{e(action)}"><input type="hidden" name="csrf" value="{e(csrf)}">{fields}{check}<button>{e(label)}</button></form>'


async def page(request, error=''):
    admin = request.app[KEY]
    repo, csrf = admin.repo, request['session']['csrf']
    statuses, names = admin.statuses()
    try:
        selected = repo.get_rule(int(request.query['id'])) if request.query.get('id') else {}
        int(request.query.get('offset', '0'))
    except ValueError:
        raise web.HTTPBadRequest(text='Invalid rule or history page')
    fields = (f'<input type="hidden" name="id" value="{e(selected.get("id", ""))}">'
        f'<label>Source channel <input name="source" value="{e(selected.get("source_name", ""))}" required></label>'
        f'<label>Destination <input name="destination" value="{e(selected.get("destination_chat_id", ""))}" required></label>'
        f'<label>Public @username <input name="username" value="{e(selected.get("destination_username", ""))}" required></label>'
        f'<label>Fixed Footer <textarea name="footer" rows="4">{e(selected.get("footer", ""))}</textarea></label>'
        '<p>Media Only · Text Only / Text + Media: future, disabled.</p>')
    editor = form(csrf, 'save', fields, 'Validate permissions and save disabled rule / edit')
    editor += form(csrf, 'preview', fields, 'Preview exact caption')
    cards = ''
    for r in repo.dashboard():
        rid = r['id']
        counts = ' · '.join(f'{s}: {r["counts"].get(s, 0)}' for s in ('waiting', 'sending', 'sent', 'retry', 'failed', 'uncertain', 'skipped'))
        enrollment = 'Inventory enabled' if r['source_name'].lower().lstrip('@') in names else 'Not in enabled inventory — enroll below, then select source/category in Operations'
        cards += f'<section class="backoffice-panel"><h2>{e(r["source_name"])} → {e(r["destination_username"])}</h2><p>{e(r["state"])} · {e(statuses.get(rid, "waiting_not_scheduled"))} · {e(enrollment)}</p><p>Last successful crawl: {e(r["last_crawl"])} · Activation boundary: {e(r["activation_watermark"])}</p><p>{e(counts)}</p><p>{e(r["last_error"])}</p><a href="?id={rid}">Edit / history</a>'
        for state in ('active', 'paused', 'disabled'):
            cards += form(csrf, 'state', f'<input type="hidden" name="id" value="{rid}"><input type="hidden" name="state" value="{state}">', state, state == 'active')
        cards += f'<p>Live permissions last verified: {e(r["permission_checked"])}</p>'
        cards += form(csrf, 'check', f'<input type="hidden" name="id" value="{rid}">', 'Recheck live access and bot rights')
        cards += '</section>'
    history = ''
    if selected:
        offset = max(0, int(request.query.get('offset', '0')))
        for d in repo.history(selected['id'], offset):
            ids = json.loads(d['destination_message_ids'])
            links = ' '.join(f'<a href="https://t.me/c/{str(d["destination_chat_id"])[4:]}/{mid}" rel="noreferrer">{mid}</a>' for mid in ids if type(mid) is int and mid > 0 and str(d['destination_chat_id']).startswith('-100'))
            history += f'<article><p>Delivery {d["id"]} · {e(d["status"])} · attempts {d["attempts"]} · {e(redact(d["error"]))} · {links}</p>'
            if d['status'] in {'uncertain', 'failed'}:
                history += form(csrf, 'review', f'<input type="hidden" name="delivery_id" value="{d["id"]}"><select name="decision"><option>skipped</option><option>sent</option><option>retry</option></select><input name="message_ids" placeholder="All ordered destination IDs, comma-separated"><input name="reason" placeholder="What you verified in Telegram" required><label><input type="checkbox" name="risk" value="yes"> I inspected Telegram and acknowledge duplicate risk</label>', 'Record manual review', True)
            history += '</article>'
        history += f'<a href="?id={selected["id"]}&amp;offset={offset+50}">Next 50 deliveries</a>'
    audit = ''.join(f'<li>{e(a["created_at"])} · {e(a["actor"])} · {e(a["action"])} · {e(redact(a["details"]))}</li>' for a in repo.audit_log(limit=50))
    categories = ''.join(f'<option>{e(c)}</option>' for c in admin.operations.channels.categories())
    enrollment = form(csrf, 'enroll', f'<input name="source" placeholder="Public source username" required><select name="category">{categories}</select>', 'Enroll in existing crawler inventory', True)
    kill = form(csrf, 'switch', '<select name="enabled"><option value="no">Stop all mirror sends</option><option value="yes">Resume mirror queue</option></select>', 'Apply global switch', True)
    body = f'''<!doctype html><html><head><meta charset="utf-8"><title>Channel Mirror</title>{SHARED_STYLE}</head><body>{page_header('channel-mirror','Channel Mirror')}
        <p class="backoffice-error">{e(error)}</p><p>Global mirror: {'enabled' if repo.enabled() else 'STOPPED'}. A rule alone does not schedule crawling. <a href="/operations">Crawler settings / schedules</a></p>{kill}
        <section class="backoffice-panel"><h2>Rule configuration</h2>{editor}<p>Activation starts an exclusive current-time watermark: old posts are not backfilled. Resuming a paused rule starts a new boundary. Footer edits affect future queued posts; existing delivery captions stay frozen. Activation validates live access again.</p></section>
        {cards}<section class="backoffice-panel"><h2>Source enrollment</h2>{enrollment}<p>Enrollment does not start a timer. Select the category and start the existing crawler in Operations.</p></section>
        <section class="backoffice-panel"><h2>Delivery history</h2>{history}</section><details><summary>Last 50 audit events</summary><ul>{audit}</ul></details></body></html>'''
    return web.Response(text=body, content_type='text/html', status=400 if error else 200)


async def action(request):
    admin, data = request.app[KEY], await request.post()
    repo, actor = admin.repo, request['session']['admin_id']
    command = request.match_info['action']
    try:
        if command == 'preview':
            text = caption(data.get('username', ''), data.get('footer', ''))
            return web.Response(text=text, content_type='text/plain')
        if command == 'save':
            if data.get('mode', 'media_only') != 'media_only':
                raise ValidationError('Only Media Only is supported in V1')
            source, target = await admin.validate(data['source'], data['destination'], data['username'], data.get('footer', ''))
            if data.get('id'):
                old = repo.get_rule(int(data['id']))
                if (source, target) != (old['source_peer_id'], old['destination_chat_id']):
                    raise ValidationError('Stable identities cannot change; disable this rule and create a new mapping')
                repo.update_rule(old['id'], actor, source_name=data['source'], destination_username=data['username'], footer=data.get('footer', ''))
            else:
                rid = repo.create_rule(source, data['source'], target, data['username'], data.get('footer', ''), actor)
            if data.get('id'): rid = int(data['id'])
            with repo.connection(True) as conn: repo._audit(conn, actor, 'permission_verified', rid, {}, stamp())
        elif command == 'check':
            r = repo.get_rule(int(data['id']))
            source, target = await admin.validate(r['source_name'], r['destination_chat_id'], r['destination_username'], r['footer'])
            if (source, target) != (r['source_peer_id'], r['destination_chat_id']): raise ValidationError('Identity changed')
            with repo.connection(True) as conn: repo._audit(conn, actor, 'permission_verified', r['id'], {}, stamp())
        elif command == 'state':
            rid, state = int(data['id']), data['state']
            if state == 'active':
                if data.get('confirmed') != 'yes':
                    raise ValidationError('Confirm the activation watermark; no old-post backfill')
                r = repo.get_rule(rid)
                source, target = await admin.validate(r['source_name'], r['destination_chat_id'], r['destination_username'], r['footer'])
                if (source, target) != (r['source_peer_id'], r['destination_chat_id']):
                    raise ValidationError('Telegram identities changed')
                with repo.connection(True) as conn: repo._audit(conn, actor, 'permission_verified', rid, {}, stamp())
            repo.set_rule_state(rid, state, actor)
        elif command in {'switch', 'enroll', 'review'}:
            if data.get('confirmed') != 'yes':
                raise ValidationError('Explicit confirmation required')
            if command == 'switch':
                if data.get('enabled') not in {'yes','no'}: raise ValidationError('Invalid global switch')
                repo.global_switch(data['enabled'] == 'yes', actor)
            elif command == 'enroll':
                rule = next((r for r in repo.list_rules() if r['source_name'].lower().lstrip('@') == data['source'].lower().lstrip('@')), None)
                if rule is None: raise ValidationError('Configure a validated mirror rule before enrollment')
                await admin.validate(rule['source_name'], rule['destination_chat_id'], rule['destination_username'], rule['footer'])
                admin.operations.channels.enroll(data['category'], data['source'])
                with repo.connection(True) as conn: repo._audit(conn, actor, 'source_enrolled', 0, {'source': data['source'], 'category': data['category']}, stamp())
            else:
                ids = tuple(int(v.strip()) for v in data.get('message_ids','').split(',') if v.strip())
                repo.review(int(data['delivery_id']), data['decision'], actor, data.get('reason',''), message_ids=ids, confirm_duplicate_risk=data.get('risk') == 'yes')
        else:
            raise web.HTTPNotFound()
        raise web.HTTPSeeOther('/channel-mirror')
    except web.HTTPException:
        raise
    except ValidationError as exc:
        return await page(request, error=redact(exc))
    except Exception:
        # Raw Telegram/network exceptions may contain credentials or source data.
        try:
            with repo.connection(True) as conn:
                repo._audit(conn, actor, 'admin_action_failed', 0, {'action': command}, stamp())
        except sqlite3.Error:
            pass
        return await page(request, error='Unable to apply action. Verify fields, confirmation and live Telegram access; no automatic send was requested.')


def install(app, administration=None):
    if administration is None:
        repo = Repository()
        repo.initialize()
        administration = Administration(repo, app['operations'])
    app[KEY] = administration
    app.add_routes([web.get('/channel-mirror', page), web.post('/channel-mirror/{action}', action)])
