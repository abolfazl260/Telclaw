"""Server-rendered administration UI. Every interpolated user value is escaped."""
import json
from datetime import datetime
from html import escape
from urllib.parse import urlencode

from backoffice_layout import SHARED_STYLE, page_header
from message_manager import recurrence
from message_manager.validation import content, ValidationError
from backoffice_message_manager_assets import STYLE, SCRIPT


def e(value):
    return escape(str(value if value is not None else ''), quote=True)


def local(value):
    if not value:
        return '—'
    return datetime.fromisoformat(value).astimezone(recurrence.TEHRAN).strftime('%Y-%m-%d %H:%M:%S')


def form(csrf, action, label, fields='', confirmation=''):
    confirm = (f'<label><input type="checkbox" name="confirmed" value="yes" required> {e(confirmation)}</label> ' if confirmation else '')
    return (f'<form method="post" action="/message-manager/{action}"><input type="hidden" name="csrf" value="{e(csrf)}">'
            + fields + confirm + f'<button>{e(label)}</button></form>')


def hidden(name, value):
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


def preview(payload, repo):
    _, safe = content(payload['text'], payload['parse_mode'], bool(payload.get('asset_key')))
    media_html = ''
    if payload.get('asset_key'):
        asset = repo.media(payload['asset_key'])
        src = '/message-manager/media/' + asset['asset_key']
        if asset['kind'] == 'photo':
            media_html = f'<img src="{e(src)}" alt="Attached photo">'
        elif asset['kind'] == 'video':
            media_html = f'<video src="{e(src)}" controls></video>'
        else:
            media_html = f'<p><a href="{e(src)}">{e(asset["original_name"])}</a></p>'
    keyboard = ''.join('<div class="keyboard">' + ''.join(f'<a href="{e(b["url"])}" rel="noreferrer">{e(b["text"])}</a>' for b in row) + '</div>' for row in payload['buttons'])
    return f'<div class="telegram-preview"><div class="mm-telegram-header">Telegram · Channel message</div>{media_html}<div dir="auto" style="white-space:pre-wrap">{safe}</div>{keyboard}</div>'


def render(runtime, csrf, nonce, query, message=None, draft=None, batch=None, error='', feedback=None):
    repo = runtime.repo
    active = query.get('view') or ('editor' if message or draft else 'imports' if batch else 'messages')
    if active not in {'messages', 'editor', 'channels', 'imports'}:
        active = 'messages'
    sections = {}
    rows = repo.list_messages(query.get('q', ''), query.get('status', ''), query.get('target', ''),
        query.get('schedule', ''), query.get('from', ''), query.get('to', ''), offset=int(query.get('offset', 0)))
    destinations = repo.targets()
    mid = message['id'] if message else ''
    payload = draft or (message['content'] if message else dict(text='', parse_mode='HTML', buttons=[], asset_key=''))
    title = (draft or {}).get('title') or (message['title'] if message else '')
    options = ''.join(f'<option value="{t["id"]}">{e(t["label"])} ({e(t["chat_id"])})</option>' for t in destinations if t['mm_eligible'])
    table = ''
    for row in rows:
        with repo.connection() as c:
            upcoming = c.execute("SELECT next_due,state FROM mm_schedules WHERE message_id=? AND state IN ('active','paused') ORDER BY next_due LIMIT 1", (row['id'],)).fetchone()
        table += (f'<tr><td><a href="/message-manager?id={row["id"]}">{e(row["title"])}</a></td>'
                  f'<td>{e(row["state"])}</td><td>{e(local(row["updated_at"]))}</td>'
                  f'<td>{e(local(upcoming["next_due"])) if upcoming else "—"} {e(upcoming["state"]) if upcoming else ""}</td></tr>')
    filters = '''<form method="get"><input name="q" placeholder="Search title or content" value="%s">
    <select name="status"><option value="">All statuses</option>%s</select>
    <select name="target"><option value="">All destinations</option>%s</select>
    <select name="schedule"><option value="">All schedules</option>%s</select>
    <label>Created from (UTC) <input type="date" name="from" value="%s"></label>
    <label>to <input type="date" name="to" value="%s"></label><button>Filter</button></form>''' % (
        e(query.get('q', '')), ''.join(f'<option {"selected" if query.get("status")==s else ""}>{s}</option>' for s in ['draft','archived','waiting','sent','retry','failed','uncertain','missed','cancelled']),
        ''.join(f'<option value="{t["id"]}" {"selected" if query.get("target")==str(t["id"]) else ""}>{e(t["label"])}</option>' for t in destinations),
        ''.join(f'<option {"selected" if query.get("schedule")==s else ""}>{s}</option>' for s in ['active','paused','completed','cancelled']), e(query.get('from','')), e(query.get('to','')))
    page_query = dict(query)
    page_query.pop('id', None)
    page_query['view'] = 'messages'
    offset = max(0, int(query.get('offset', 0)))
    page_query['offset'] = offset + 100
    next_link = f'<a href="/message-manager?{e(urlencode(page_query))}">Next 100</a>' if len(rows) == 100 else ''
    previous_query = dict(page_query, offset=max(0, offset - 100))
    previous_link = f'<a href="/message-manager?{e(urlencode(previous_query))}">Previous</a>' if offset else ''
    empty = '<div class="mm-empty"><h3>No messages found</h3><p>Create your first message, or adjust the filters.</p><a class="mm-primary" href="/message-manager?view=editor">Create your first message</a></div>' if not rows else ''
    body = f'''<section class="backoffice-panel"><h2>Messages</h2>{filters}
      {empty}<div class="mm-table"><table><thead><tr><th>Title</th><th>Draft state</th><th>Updated · Tehran</th><th>Next publication · Tehran</th></tr></thead><tbody>{table}</tbody></table></div>
      <div class="mm-actions">{previous_link} {next_link}<a href="/message-manager?view=editor">New draft</a> · <a href="/message-manager">Reset filters</a></div></section>'''
    if not rows:
        body = body.replace('<div class="mm-table"><table>', '<div class="mm-table" hidden><table>', 1)
    health = repo.dashboard()
    cards = '<div class="mm-status-cards">' + ''.join(f'<div><strong>{health[key]}</strong><span>{label}</span></div>' for key,label in [('upcoming','Upcoming messages'),('sent','Sent deliveries'),('failed','Failed deliveries'),('uncertain','Uncertain deliveries')]) + '</div>'
    health_panel = f'<section class="backoffice-panel"><h2>Publishing status</h2>{cards}<p>Deliveries are counted per channel, across all history.</p><p>Worker: <strong>{e(health["worker_state"])}</strong> · Last queue check: {e(local(health["heartbeat"]))} · Telegram cooldown: {e(local(health["cooldown"])) if health["cooldown"] else "None"}</p><p>{e(health["worker_error"])}</p><p class="backoffice-muted">Unknown/stale means no recent worker heartbeat was recorded; this page does not start the worker. Refresh to update status.</p></section>'
    sections['messages'] = health_panel + body
    body = ''
    body += f'''<div class="mm-editor-grid"><section class="backoffice-panel"><h2>{'Edit draft #' + str(mid) if mid else 'Create draft'}</h2>
      <form id="message-editor" data-unsaved="{'true' if draft else 'false'}" method="post" action="/message-manager/save">{hidden('csrf',csrf)}{hidden('id',mid)}{hidden('version',message['version'] if message else '')}
      <fieldset><legend>1. Write your message</legend><label>Title <input name="title" value="{e(title)}" maxlength="200" required></label>
      <label>Formatting <select name="parse_mode"><option {'selected' if payload['parse_mode']=='HTML' else ''}>HTML</option><option {'selected' if payload['parse_mode']=='plain' else ''}>plain</option></select></label>
      <div class="mm-toolbar" role="group" aria-label="Text formatting"><button type="button" data-format="b"><b>Bold</b></button><button type="button" data-format="i"><i>Italic</i></button><button type="button" data-format="a">Link</button></div>
      <label>Message / caption <textarea dir="auto" id="message-text" name="text" rows="8" required>{e(payload['text'])}</textarea></label>
      <p class="backoffice-muted">Text: 4096 units; media caption: 1024. Select text and use the formatting buttons.</p></fieldset><fieldset><legend>2. Add a file (optional)</legend>
      <input type="hidden" id="asset-key" name="asset_key" value="{e(payload.get('asset_key',''))}">
      <label>Photo, video or document<input type="file" id="media-file" accept=".jpg,.jpeg,.png,.mp4,.pdf,.txt,.zip,.docx,.xlsx"></label>
      <div class="mm-actions"><button id="upload" type="button">Upload</button><button id="remove-media" type="button">Remove attachment</button></div>
      <p id="upload-result" role="status">{e(repo.media(payload['asset_key'])['original_name']) if payload.get('asset_key') else 'No file attached'}</p></fieldset>
      <fieldset><legend>3. Add buttons (optional)</legend><div id="button-builder"></div><button id="add-button" type="button">Add button</button>
      <details><summary>Advanced keyboard JSON</summary>
      <label>URL keyboard JSON (array of rows) <textarea id="keyboard-json" name="buttons" rows="3">{e(payload.get('buttons_raw', json.dumps(payload['buttons'],ensure_ascii=False)))}</textarea></label>
      <p class="backoffice-muted">Array of rows containing text and URL objects.</p></details></fieldset>
      <div class="mm-actions"><button formaction="/message-manager/preview">Preview</button> <button>Save draft</button></div>
      <p id="save-status" role="status">Save changes before publishing.</p></form></section>
      <aside class="backoffice-panel mm-preview-panel"><h2>Telegram preview</h2><p class="backoffice-muted">Preview updates while you write.</p><div id="live-preview">'''
    if payload['text']:
        try:
            body += preview(payload, repo)
        except ValidationError:
            body += '<p>Correct the highlighted fields to preview this message.</p>'
    else:
        body += '<p class="mm-empty">Your message preview will appear here.</p>'
    body += '</div><p id="preview-status" role="status"></p></aside></div>'
    if not message:
        body += '<section class="backoffice-panel"><h2>4. Destinations and timing</h2><p>Save your draft to select channels and choose Publish Now or a schedule.</p></section>'
    if message:
        fields = hidden('id', mid)
        body += '<section class="backoffice-panel mm-publish"><h2>4. Publish and schedule</h2><p>These actions publish the saved draft shown above. Save any edits first.</p>'
        if message['state'] == 'draft':
            selectors = f'<label>Destinations (select multiple)<select multiple size="5" name="targets" required>{options}</select></label>'
            body += form(csrf, 'publish', 'Queue Publish Now', fields + selectors, 'Publish this saved draft now to all selected channels')
            body += '<details class="mm-schedule"><summary>Schedule instead / reschedule</summary>' + form(csrf, 'schedule', 'Activate schedule', fields + selectors + '''
             <label>Start date · Tehran (Gregorian)<input name="date" type="date" required></label>
             <label>Time · Tehran<input name="time" type="time" required></label>
             <label>Recurrence<select name="recurrence"><option value="once">One time</option><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="days">Every N days</option><option value="weeks">Every N weeks</option></select></label>
             <label>Interval N<input name="interval" type="number" min="1" max="365" value="1"></label>
             <label>Selected weekdays (0=Mon … 6=Sun; comma separated)<input name="weekdays" placeholder="0,2,4"></label>
             <label>Optional end date<input name="end_date" type="date"></label>
             <label>Optional total occurrences<input name="end_count" type="number" min="1" max="100000"></label>
             <label>Replace active/paused schedule ID (optional)<input name="replace_id" type="number" min="1"></label>''', 'Activate this schedule for all selected channels') + '</details>'
        body += '<details><summary>More actions</summary>'
        body += form(csrf, 'duplicate', 'Duplicate as new draft', fields)
        body += form(csrf, 'archive', 'Archive and cancel pending publications', fields, 'Archive this message and cancel its pending deliveries') + '</details>'
        schedules, deliveries, attempts = repo.details(mid)
        body += '<details><summary>Schedules and delivery history</summary><h3>Schedules</h3>'
        for s in schedules:
            body += f'<p>#{s["id"]} · {e(s["state"])} · next {e(local(s["next_due"]))} · <code>{e(s["spec_json"])}</code></p>'
            sf = hidden('id', mid) + hidden('schedule_id', s['id'])
            for action, label, allowed in [('pause','Pause',s['state']=='active'),('resume','Resume',s['state']=='paused'),('cancel','Cancel',s['state']!='cancelled')]:
                if allowed:
                    body += form(csrf, 'schedule-action', label, sf + hidden('action', action))
        body += '<h3>Per-occurrence and destination deliveries (latest 200)</h3><table><tr><th>ID / schedule / occurrence</th><th>Due · Tehran</th><th>Channel</th><th>Status</th><th>Attempts / retry</th><th>Telegram / error / action</th></tr>'
        for d in deliveries:
            link = f'<a href="{e(d["message_link"])}">{e(d["telegram_message_id"])}</a>' if d['message_link'] else e(d['telegram_message_id'])
            retry = form(csrf, 'retry', 'Queue manual resend', fields + hidden('delivery_id', d['id']), 'I verified Telegram; resending may create a duplicate') if d['status'] in {'failed','uncertain'} else ''
            body += f'<tr><td>#{d["id"]} / {d["schedule_id"]} / {d["ordinal"]}</td><td>{e(local(d["due_at"]))}</td><td>{e(d["chat_id"])}</td><td>{e(d["status"])}</td><td>{d["attempts"]} / {e(local(d["next_retry"]))}</td><td>{link} {e(d["error"])}{retry}</td></tr>'
        body += '</table><details><summary>Attempt history (latest 500)</summary><table><tr><th>Delivery</th><th>Attempt</th><th>Started · Tehran</th><th>Finished · Tehran</th><th>Outcome</th><th>Error / Telegram ID</th></tr>'
        for a in attempts:
            body += f'<tr><td>{a["delivery_id"]}</td><td>{a["number"]}</td><td>{e(local(a["started_at"]))}</td><td>{e(local(a["finished_at"]))}</td><td>{e(a["outcome"])}</td><td>{e(a["error"])} {e(a["telegram_message_id"])}</td></tr>'
        body += '</table></details></details></section>'
    sections['editor'] = body
    body = '<section class="backoffice-panel"><h2>Channels</h2><p>Check channel permissions before enabling a destination.</p><div class="mm-table"><table><thead><tr><th>Channel</th><th>Message Manager</th><th>Connection</th><th>Action</th></tr></thead><tbody>'
    for t in destinations:
        state = 'Eligible' if t['mm_eligible'] else 'Not eligible'
        connection = t['connection_status']
        badge = 'good' if t['mm_eligible'] else 'neutral'
        connection_badge = 'good' if connection == 'connected' else 'bad' if connection == 'disconnected' else 'neutral'
        body += f'<tr><td><strong>{e(t["label"])}</strong><br>{e(t["chat_id"])}</td><td><span class="mm-badge {badge}">{state}</span></td><td><span class="mm-badge {connection_badge}">{e(connection)}</span></td><td>'
        body += form(csrf, 'target', 'Disable' if t['mm_eligible'] else 'Check and enable', hidden('target_id',t['id']) + hidden('enabled','no' if t['mm_eligible'] else 'yes')) + '</td></tr>'
    body += '</tbody></table></div><details><summary>Register a new channel</summary>'
    body += form(csrf, 'target', 'Register, check and enable channel', '<label>Label<input name="label" maxlength="200" required></label><label>@channel / negative ID<input name="chat_id" required></label>') + '</details></section>'
    sections['channels'] = body
    body = ''
    body += f'''<section class="backoffice-panel"><h2>XLSX import / delivery export</h2><p><a href="/message-manager/template.xlsx">Download XLSX template</a> · <a href="/message-manager/status.xlsx">Export latest 10,000 deliveries</a></p>
      <form method="post" action="/message-manager/import" enctype="multipart/form-data">{hidden('csrf',csrf)}
      <label>Import namespace<input name="namespace" maxlength="64" required></label><input type="file" name="workbook" accept=".xlsx" required><button>Validate and preview</button></form>
      <p>Import creates drafts only after confirmation. Imported schedules require a second explicit activation. Existing keys in a namespace are rejected.</p>'''
    if batch:
        body += f'<h3>Import {e(batch["id"])} · {e(batch["state"])} · {batch["preview"]["errors"]} row errors</h3>'
        for row in batch['preview']['rows']:
            body += f'<details open><summary>{e(row.get("sheet","Messages"))} row {row["row"]} · {e(row["key"])}</summary><p class="backoffice-error">{e("; ".join(row["errors"]))}</p><pre>{e(json.dumps(row,ensure_ascii=False,indent=2))}</pre>'
            if row.get('content') and not row['errors']:
                body += preview(row['content'],repo)
            body += '</details>'
        if batch['state'] == 'preview' and not batch['preview']['errors']:
            body += form(csrf, 'import-confirm', 'Create imported drafts', hidden('batch_id',batch['id']), 'Create all validated rows as drafts')
        elif batch['state'] == 'confirmed' and not batch['activated_at']:
            body += form(csrf, 'import-activate', 'Activate imported schedules', hidden('batch_id',batch['id']), 'Activate every previewed schedule for all listed destinations')
    with repo.connection() as c:
        recent_batches = list(c.execute('SELECT id,namespace,state,activated_at FROM mm_import_batches ORDER BY created_at DESC LIMIT 20'))
        audits = list(c.execute('SELECT * FROM mm_audit ORDER BY id DESC LIMIT 50'))
    body += '<details><summary>Recent imports</summary>'
    for b in recent_batches:
        body += f'<p><a href="/message-manager?batch={e(b["id"])}">{e(b["namespace"])} · {e(b["id"])}</a> · {e(b["state"])} · activated {e(local(b["activated_at"]))}</p>'
    body += '</details></section>'
    sections['imports'] = body
    body = '<section class="backoffice-panel"><details><summary>Recent audit events</summary><table><tr><th>Time · Tehran</th><th>Actor</th><th>Action / entity</th><th>Details</th></tr>'
    for a in audits:
        body += f'<tr><td>{e(local(a["created_at"]))}</td><td>{e(a["actor"])}</td><td>{e(a["action"])} / {e(a["entity"])}</td><td>{e(a["details"])}</td></tr>'
    body += '</table></details></section>'
    audit = body
    tabs = '<nav class="mm-tabs" aria-label="Message Manager sections">' + ''.join(
        f'<a href="/message-manager?view={key}"' + (' aria-current="page"' if active == key else '') + f'>{label}</a>'
        for key, label in [('messages','Messages'), ('editor','Create message'), ('channels','Channels'), ('imports','Excel and reports')]) + '</nav>'
    notices = {'save':'Draft saved successfully.', 'schedule':'Schedule activated successfully.', 'publish':'Publication queued successfully; check delivery status for the result.', 'duplicate':'Draft duplicated.', 'archive':'Message archived.', 'schedule-action':'Schedule updated.', 'retry':'Manual resend queued.', 'target':'Channel updated.', 'import':'Workbook validated. Review the preview.', 'import-confirm':'Imported drafts saved.', 'import-activate':'Imported schedules activated.'}
    notice = notices.get(query.get('notice'), '')
    banner = f'<p class="mm-success" role="status">{e(notice)}</p>' if notice else ''
    if feedback:
        banner += f'<div id="form-feedback" data-feedback="{e(json.dumps(feedback))}"></div>'
    script = SCRIPT.replace('__CSRF__', json.dumps(csrf))
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Telclaw · Message Manager</title>'
        + SHARED_STYLE + STYLE + '</head><body>' + page_header('message-manager','Message Manager')
        + '<p class="backoffice-muted">Create, schedule and track your channel messages · All publication times use Asia/Tehran.</p>'
        + tabs + banner + (f'<p class="backoffice-error" role="alert">{e(error)}</p>' if error else '') + sections[active]
        + (audit if active == 'messages' else '') + f'<script nonce="{e(nonce)}">{script}</script></body></html>')
