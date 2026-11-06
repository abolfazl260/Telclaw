"""Server-rendered administration UI. Every interpolated user value is escaped."""
import json
from datetime import datetime
from html import escape
from urllib.parse import urlencode

from backoffice_layout import SHARED_STYLE, page_header
from message_manager import recurrence
from message_manager.validation import content


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
    return f'<div class="telegram-preview">{media_html}<div dir="auto" style="white-space:pre-wrap">{safe}</div>{keyboard}</div>'


def render(runtime, csrf, nonce, query, message=None, draft=None, batch=None, error=''):
    repo = runtime.repo
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
    page_query['offset'] = max(0, int(query.get('offset', 0))) + 100
    body = f'''<section class="backoffice-panel"><h2>Messages</h2>{filters}
      <table><thead><tr><th>Title</th><th>Draft state</th><th>Updated · Tehran</th><th>Next publication · Tehran</th></tr></thead><tbody>{table}</tbody></table>
      <a href="/message-manager?{e(urlencode(page_query))}">Next 100</a> · <a href="/message-manager">New draft / reset filters</a></section>'''
    body += f'''<section class="backoffice-panel"><h2>{'Edit draft #' + str(mid) if mid else 'Create draft'}</h2>
      <form method="post" action="/message-manager/save">{hidden('csrf',csrf)}{hidden('id',mid)}{hidden('version',message['version'] if message else '')}
      <label>Title <input name="title" value="{e(title)}" maxlength="200" required></label>
      <label>Formatting <select name="parse_mode"><option {'selected' if payload['parse_mode']=='HTML' else ''}>HTML</option><option {'selected' if payload['parse_mode']=='plain' else ''}>plain</option></select></label>
      <label>Message / caption <textarea name="text" rows="8" required>{e(payload['text'])}</textarea></label>
      <p class="backoffice-muted">HTML supports b, i, u, s, a, code, pre, blockquote and tg-spoiler. Text: 4096 units; media caption: 1024.</p>
      <label>Primary media asset key <input id="asset-key" name="asset_key" value="{e(payload.get('asset_key',''))}"></label>
      <label>URL keyboard JSON (array of rows) <textarea name="buttons" rows="3">{e(json.dumps(payload['buttons'],ensure_ascii=False))}</textarea></label>
      <p class="backoffice-muted">Example: [[{{"text":"Open", "url":"https://t.me/example"}}]]</p>
      <button formaction="/message-manager/preview">Preview</button> <button>Save draft</button></form>
      <label>Upload private media <input type="file" id="media-file" accept=".jpg,.jpeg,.png,.mp4,.pdf,.txt,.zip,.docx,.xlsx"></label>
      <button id="upload" type="button">Upload</button><p id="upload-result" role="status"></p>'''
    if payload['text']:
        body += '<h3>Telegram preview</h3>' + preview(payload, repo)
    body += '</section>'
    if message:
        fields = hidden('id', mid)
        body += '<section class="backoffice-panel"><h2>Publish and schedule</h2>'
        if message['state'] == 'draft':
            selectors = f'<label>Destinations (select multiple)<select multiple size="5" name="targets" required>{options}</select></label>'
            body += form(csrf, 'publish', 'Queue Publish Now', fields + selectors, 'Publish this saved draft now to all selected channels')
            body += '<h3>Schedule / reschedule saved draft</h3>' + form(csrf, 'schedule', 'Activate schedule', fields + selectors + '''
             <label>Start date · Tehran (Gregorian)<input name="date" type="date" required></label>
             <label>Time · Tehran<input name="time" type="time" required></label>
             <label>Recurrence<select name="recurrence"><option value="once">One time</option><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="days">Every N days</option><option value="weeks">Every N weeks</option></select></label>
             <label>Interval N<input name="interval" type="number" min="1" max="365" value="1"></label>
             <label>Selected weekdays (0=Mon … 6=Sun; comma separated)<input name="weekdays" placeholder="0,2,4"></label>
             <label>Optional end date<input name="end_date" type="date"></label>
             <label>Optional total occurrences<input name="end_count" type="number" min="1" max="100000"></label>
             <label>Replace active/paused schedule ID (optional)<input name="replace_id" type="number" min="1"></label>''', 'Activate this schedule for all selected channels')
        body += form(csrf, 'duplicate', 'Duplicate as new draft', fields)
        body += form(csrf, 'archive', 'Archive and cancel pending publications', fields, 'Archive this message and cancel its pending deliveries')
        schedules, deliveries, attempts = repo.details(mid)
        body += '<h3>Schedules</h3>'
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
        body += '</table></details></section>'
    body += '<section class="backoffice-panel"><h2>Message Manager destinations</h2><p>Eligibility is independent of automatic ad rules. Channel permissions are checked on activation and again before sending.</p>'
    for t in destinations:
        body += f'<p>{e(t["label"])} · {e(t["chat_id"])} · {"Eligible" if t["mm_eligible"] else "Not eligible"} · {e(t["connection_status"])}</p>'
        body += form(csrf, 'target', 'Disable' if t['mm_eligible'] else 'Check and enable', hidden('target_id',t['id']) + hidden('enabled','no' if t['mm_eligible'] else 'yes'))
    body += form(csrf, 'target', 'Register, check and enable channel', '<label>Label<input name="label" maxlength="200" required></label><label>@channel / negative ID<input name="chat_id" required></label>') + '</section>'
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
    body += '</details></section><section class="backoffice-panel"><details><summary>Recent audit events</summary><table><tr><th>Time · Tehran</th><th>Actor</th><th>Action / entity</th><th>Details</th></tr>'
    for a in audits:
        body += f'<tr><td>{e(local(a["created_at"]))}</td><td>{e(a["actor"])}</td><td>{e(a["action"])} / {e(a["entity"])}</td><td>{e(a["details"])}</td></tr>'
    body += '</table></details></section>'
    style = '''<style>table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #dce4ef;padding:.5rem;text-align:left;vertical-align:top}td{overflow-wrap:anywhere}form{display:flex;flex-wrap:wrap;gap:.7rem;align-items:end;margin:.8rem 0}label{display:flex;flex-direction:column;gap:.2rem}textarea{width:min(900px,95vw)}label:has(textarea){width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}code{overflow-wrap:anywhere}.telegram-preview{background:#eef7ed;max-width:600px;padding:1rem;border-radius:12px}.telegram-preview img,.telegram-preview video{max-width:100%;max-height:400px}.keyboard{display:flex;gap:.5rem;margin-top:.5rem}.keyboard a{background:white;padding:.4rem;border-radius:6px}tg-spoiler{background:#ccc}td form{font-size:.85rem}section{overflow-x:auto}</style>'''
    script = '''document.getElementById('upload').addEventListener('click',async()=>{const file=document.getElementById('media-file').files[0];const out=document.getElementById('upload-result');if(!file){out.textContent='Select a file first';return}out.textContent='Uploading…';try{const r=await fetch('/message-manager/media',{method:'POST',headers:{'X-CSRF-Token':CSRF,'X-File-Name':encodeURIComponent(file.name),'Content-Type':file.type||'application/octet-stream'},body:file});const text=await r.text();if(!r.ok)throw new Error(text);const data=JSON.parse(text);document.getElementById('asset-key').value=data.asset_key;out.textContent='Uploaded. Asset key: '+data.asset_key+' · save the draft to attach it.'}catch(e){out.textContent=e.message}});'''
    script = script.replace('CSRF',json.dumps(csrf))
    return '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Telclaw · Message Manager</title>' + SHARED_STYLE + style + '</head><body>' + page_header('message-manager','Message Manager') + '<p>All schedule times use Asia/Tehran. The independent worker runs in the Telclaw runtime.</p>' + (f'<p class="backoffice-error">{e(error)}</p>' if error else '') + body + f'<script nonce="{e(nonce)}">{script}</script></body></html>'
