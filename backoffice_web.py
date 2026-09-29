"""Private rule back office, served directly or behind a proxy."""
import hashlib
import html
import ipaddress
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from aiohttp import web
import aiohttp

import config
import backoffice_data
import backoffice_settings
import backoffice_health
import routing_rules
from delivery.telegram_transfer_publisher import TelegramTransferPublisher, TransferTelegramPublishError
from monitoring.telegram_monitor import ADMIN_USER_IDS, get_telegram_monitor
from storage.database import get_connection


def _now():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initialize_auth():
    conn = get_connection()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS backoffice_links (
            token_hash TEXT PRIMARY KEY, admin_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL, used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS backoffice_sessions (
            token_hash TEXT PRIMARY KEY, admin_id INTEGER NOT NULL,
            csrf TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        """)
        conn.commit()
    finally:
        conn.close()


def public_origin():
    """Return the external origin, optionally adding its public proxy port."""
    base = config.BACKOFFICE_PUBLIC_URL.rstrip("/")
    parsed = urlparse(base)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Invalid back office public URL port") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise RuntimeError("TELCLAW_BACKOFFICE_PUBLIC_URL must be an HTTP IP or HTTPS origin")
    public_port = config.BACKOFFICE_PUBLIC_PORT
    if parsed.scheme == "http":
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise RuntimeError("Direct HTTP back office requires an IP address") from exc
        if (public_port or port != config.BACKOFFICE_PORT
                or config.BACKOFFICE_HOST not in {"0.0.0.0", "::", parsed.hostname}
                or config.BACKOFFICE_TLS_CERT or config.BACKOFFICE_TLS_KEY):
            raise RuntimeError("Direct HTTP requires the same public and local port, a public bind address, and no TLS certificate")
    if public_port:
        if port and port != public_port:
            raise RuntimeError("Public URL port conflicts with TELCLAW_BACKOFFICE_PUBLIC_PORT")
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        base = f"{parsed.scheme}://{hostname}:{public_port}"
    return base


def issue_link(admin_id):
    """Issue a five-minute, single-use link only to approved Telegram admins."""
    if int(admin_id) not in ADMIN_USER_IDS:
        raise PermissionError("Not an admin")
    base = public_origin()
    initialize_auth()
    token = secrets.token_urlsafe(32)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM backoffice_links WHERE expires_at < ? OR used_at IS NOT NULL",
                     (_now().isoformat(),))
        conn.execute("INSERT INTO backoffice_links VALUES(?,?,?,NULL)",
                     (_digest(token), int(admin_id), (_now() + timedelta(minutes=5)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    return f"{base}/login?token={token}"


def _session(request):
    token = request.cookies.get("telclaw_admin", "")
    if not token:
        return None
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM backoffice_sessions WHERE token_hash=? AND expires_at>?",
                           (_digest(token), _now().isoformat())).fetchone()
        return dict(row) if row and row["admin_id"] in ADMIN_USER_IDS else None
    finally:
        conn.close()


@web.middleware
async def _security(request, handler):
    request["csp_nonce"] = secrets.token_urlsafe(16)
    try:
        if request.path != "/login":
            session = _session(request)
            if not session:
                raise web.HTTPUnauthorized(text="Open a new back-office link from the Telegram bot")
            request["session"] = session
            if request.method == "POST":
                form = await request.post()
                if not secrets.compare_digest(str(form.get("csrf", "")), session["csrf"]):
                    raise web.HTTPForbidden(text="Invalid form token")
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = ("default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{request['csp_nonce']}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    return response


async def login(request):
    token = request.query.get("token", "")
    if len(token) < 32:
        raise web.HTTPForbidden(text="Invalid or expired link")
    initialize_auth()
    conn = get_connection()
    try:
        # Atomic consumption prevents a link from being redeemed twice.
        cursor = conn.execute("""UPDATE backoffice_links SET used_at=? WHERE token_hash=?
            AND used_at IS NULL AND expires_at>?""",
            (_now().isoformat(), _digest(token), _now().isoformat()))
        if cursor.rowcount != 1:
            conn.rollback()
            raise web.HTTPForbidden(text="Invalid or expired link")
        row = conn.execute("SELECT admin_id FROM backoffice_links WHERE token_hash=?",
                           (_digest(token),)).fetchone()
        if row["admin_id"] not in ADMIN_USER_IDS:
            conn.rollback()
            raise web.HTTPForbidden(text="Admin access revoked")
        session_token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                     (_digest(session_token), row["admin_id"], csrf,
                      (_now() + timedelta(hours=12)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    response = web.HTTPSeeOther("/")
    response.set_cookie("telclaw_admin", session_token, httponly=True,
                        secure=urlparse(public_origin()).scheme == "https",
                        samesite="Strict", max_age=43200, path="/")
    raise response


def _format_bytes(value):
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def _format_duration(seconds):
    total = max(0, int(seconds or 0))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, seconds = divmod(total, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def _escape(value):
    return html.escape(str(value or ""), quote=True)


def _select(name, values, selected):
    return f'<select name="{name}">' + "".join(
        f'<option value="{_escape(v)}" {"selected" if str(v)==str(selected) else ""}>{_escape(v)}</option>'
        for v in values) + "</select>"


async def index(request):
    csrf = _escape(request["session"]["csrf"])
    overview = backoffice_health.snapshot()["overview"]
    provider_rows = "".join(
        f'<span class="provider-status"><strong>{_escape(item["name"].title())}</strong> '
        f'<span class="badge {"connected" if item["status"] == "READY" else "disconnected"}">'
        f'{_escape(item["status"])}</span> '
        f'<span class="hint">priority #{int(item["priority"])} · {int(item["credential_count"])} credential(s)</span></span>'
        for item in overview["providers"]
    ) or '<span class="hint">No AI providers configured.</span>'
    system = backoffice_health.snapshot()["system"]
    targets, rules = routing_rules.list_targets(), routing_rules.list_rules()
    rows = []
    for target in targets:
        assigned = [rule for rule in rules if rule["target_id"] == target["id"]]
        rule_rows = "".join(_rule_panel(rule, target, csrf, request) for rule in assigned)
        history = routing_rules.recent_deliveries(30, target['id'])
        history_rows = ''.join(f'''<tr><td>#{item['message_id']}</td>
            <td>{_escape(item.get('delivery_kind') or 'original')}</td><td>{_escape(item['status'])}</td>
            <td>{_escape(item['telegram_message_id'] or '—')}</td><td>{_escape(item['updated_at'])}</td>
            <td>{_escape(item['error'])}</td><td>'''
            + (f'''<form method="post" action="/retry"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="message_id" value="{item['message_id']}">
            <input type="hidden" name="target_id" value="{target['id']}"><button
            {'data-confirm="Telegram may already have posted this ad. Check the channel before retrying."' if item['status']=='uncertain' else ''}
            >{'Review and retry' if item['status']=='uncertain' else 'Retry'}</button></form>'''
            if item.get('delivery_kind') == 'original' and item['status'] in {'retry', 'rejected', 'uncertain'} else '')
            + '</td></tr>' for item in history)
        state = target.get('connection_status') or 'unknown'
        opened = str(getattr(request, 'query', {}).get('channel', '')) == str(target['id'])
        rows.append(f'''<details class="channel" id="channel-{target['id']}" {'open' if opened else ''}><summary>
            <span class="channel-name">{_escape(target['label'])}</span>
            <code>{_escape(target['chat_id'])}</code>
            <span class="badge {state}">{_escape(state)}</span>
            <span class="hint">{len(assigned)} rules · {len(history)} deliveries</span></summary>
            <div class="channel-body"><p class="hint">{_escape(target.get('description'))}</p>
            <p class="hint">{_escape(target.get('connection_detail') or 'Connection not checked')}
            · checked: {_escape(target.get('checked_at') or 'never')}</p>
            <form method="post" action="/target/check" class="inline"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="target_id" value="{target['id']}"><button>Check connection</button></form>
            <details class="subsection"><summary>Edit channel details</summary>
            <form method="post" action="/target"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="id" value="{target['id']}">
            <label>Name<input name="label" value="{_escape(target['label'])}" required></label>
            <label>Telegram ID or username<input name="chat_id" value="{_escape(target['chat_id'])}" required></label>
            <label>Purpose / notes<textarea name="description" maxlength="500" rows="3">{_escape(target.get('description'))}</textarea></label>
            <label><input type="checkbox" name="enabled" {"checked" if target['enabled'] else ""}> Enabled</label>
            <button>Save channel / group</button></form></details>
            <div class="subsection"><h3>Publishing rules</h3>{rule_rows or '<p>No rules for this channel yet.</p>'}
            <details><summary>Add a rule for this channel</summary>{_rule_form(None, target, csrf)}</details></div>
            <details class="subsection"><summary>Delivery history ({len(history)})</summary><div class="scroll"><table>
            <thead><tr><th>Message</th><th>Attempt</th><th>Status</th><th>Telegram ID</th><th>Updated (UTC)</th><th>Error</th><th></th></tr></thead>
            <tbody>{history_rows or '<tr><td colspan="7">No deliveries yet.</td></tr>'}</tbody></table></div></details>
            </div></details>''')
    body = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Telclaw back office</title>
    <style>*{{box-sizing:border-box}}body{{font:15px/1.55 system-ui;max-width:1150px;margin:0 auto;padding:2rem 1rem;background:#f3f6fb;color:#192333}}
    section{{background:white;padding:1.5rem;margin:1.3rem 0;border:1px solid #e1e7ef;border-radius:16px;box-shadow:0 5px 22px #182b4510}}
    .grid{{display:block;margin:1rem 0}}
    .channel{{border:1px solid #dce4ef;border-radius:12px;background:#fbfcff;margin:.7rem 0;overflow:hidden}}
    .channel>summary{{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;padding:1rem;cursor:pointer;list-style:none}}
    .channel>summary::-webkit-details-marker{{display:none}}.channel>summary:before{{content:'▸';color:#1957b8}}
    .channel[open]>summary:before{{content:'▾'}}.channel-name{{font-weight:700;min-width:140px}}
    .channel-body{{padding:0 1rem 1rem;border-top:1px solid #e3e8ef}}.subsection{{border-top:1px solid #e3e8ef;padding:.8rem 0}}
    .subsection>summary{{cursor:pointer;color:#1957b8;font-weight:600}}.subsection h3{{margin:.4rem 0}}
    form{{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;border-bottom:1px solid #ddd;padding:.8rem 0}}
    dialog form{{display:grid}}.inline{{display:inline-flex;border:0;padding:0}}form input,form select,form textarea{{min-width:120px}}
    input,select,textarea,button{{font:inherit;padding:.55rem;max-width:100%;border-radius:7px}}
    input,select,textarea{{border:1px solid #c8d3e2}}button{{cursor:pointer;background:#1957b8;color:white;border:0;margin:.2rem}}
    dialog{{border:0;border-radius:16px;max-height:85vh;width:min(520px,calc(100vw - 2rem));max-width:calc(100vw - 2rem);box-shadow:0 20px 70px #182b4560}}
    dialog.wide{{width:900px}}dialog::backdrop{{background:#182b4590}}.scroll{{overflow:auto}}table{{border-collapse:collapse;min-width:650px;width:100%}}
    th,td{{padding:.6rem;text-align:left;border-bottom:1px solid #e3e8ef;overflow-wrap:anywhere}}code{{overflow-wrap:anywhere}}
    .badge{{font-size:.8rem;border-radius:30px;padding:.2rem .55rem;background:#e9eef5}}.badge.connected{{background:#d8f4e6;color:#16653e}}
    .badge.disconnected{{background:#ffe1db;color:#a32e1a}}.hint{{color:#526174}}
    .rule-panel{{padding:1rem;margin:.8rem 0;border:1px solid #dce4ef;border-radius:10px;background:white}}
    .rule-panel form{{border:0}}.rule-title{{display:flex;flex-wrap:wrap;align-items:center;gap:1rem}}
    .conditions-editor{{flex:1 1 100%;border:1px solid #dce4ef;border-radius:9px;padding:.7rem;background:#f8faff}}
    .condition-row{{display:grid;grid-template-columns:90px minmax(140px,1fr) 140px minmax(150px,1fr) auto;gap:.45rem;align-items:center;margin:.45rem 0}}
    .condition-row:first-child .condition-join{{visibility:hidden}}@media(max-width:760px){{.condition-row{{grid-template-columns:1fr}}.condition-row:first-child .condition-join{{display:none}}}}
    .danger{{background:#a53732}}.ad-row{{border-top:1px solid #e4e9ef;padding:.65rem 0}}
    .overview-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.8rem;margin:1rem 0}}
    .overview-card{{padding:1rem;border:1px solid #dce4ef;border-radius:12px;background:#f8faff}}
    .overview-card span{{display:block;color:#526174;font-size:.84rem}}
    .overview-card strong{{display:block;font-size:1.55rem;margin-top:.2rem}}
    .provider-status{{display:inline-flex;align-items:center;gap:.45rem;flex-wrap:wrap;margin:.2rem .8rem .2rem 0}}
    .ad-row p{{margin:.3rem 0}}.ad-row form{{display:inline-flex;padding:0}}
    .ad-row pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-height:350px;overflow:auto;background:#f5f7fb;padding:.7rem}}
    .tabs{{display:flex;gap:.6rem;margin:1rem 0}}.tabs a{{padding:.5rem .9rem;border-radius:8px;background:white;color:#1957b8;text-decoration:none}}
    .tabs a.active{{background:#1957b8;color:white}}
    </style></head><body><h1>Telclaw · Publishing rules</h1>
    <nav class="tabs" aria-label="Back office sections"><a href="/" class="active">Publishing</a>
    <a href="/data">Database</a><a href="/health">System Health</a><a href="/settings">Settings</a></nav>
    {('<p class="badge">Telegram paused this send; please retry after its rate limit clears.</p>'
      if getattr(request, 'query', {}).get('notice') == 'rate_limited' else '')}
    <p class="hint">Rules run by priority. Unmatched ads are not published. Each rule can use any stored field from its topic table.</p>
    <section><h2>Overview · {_escape(overview["date"])}</h2>
    <div class="overview-grid">
      <div class="overview-card"><span>New messages today</span><strong>{int(overview["new_messages"]):,}</strong></div>
      <div class="overview-card"><span>Duplicates removed today</span><strong>{int(overview["duplicates"]):,}</strong></div>
      <div class="overview-card"><span>Ads ready to publish</span><strong>{int(overview["ready_ads"]):,}</strong></div>
      <div class="overview-card"><span>AI extraction</span><strong>{'Enabled' if overview["provider_enabled"] else 'Disabled'}</strong></div>
    </div>
    <div><strong>AI Providers:</strong> {provider_rows}</div></section>
    <section><h2>System Health</h2>
    <div class="overview-grid">
      <div class="overview-card"><span>Daily ads</span><strong>{int(system["daily_ads"]):,}</strong></div>
      <div class="overview-card"><span>CPU</span><strong>{system["cpu_percent"]:.1f}%</strong></div>
      <div class="overview-card"><span>RAM</span><strong>{_format_bytes(system["ram_bytes"])}</strong></div>
      <div class="overview-card"><span>DB size</span><strong>{_format_bytes(system["db_size_bytes"])}</strong></div>
      <div class="overview-card"><span>Queue size</span><strong>{int(system["queue_size"]):,}</strong></div>
      <div class="overview-card"><span>Error rate</span><strong>{system["error_rate"]:.1f}%</strong></div>
      <div class="overview-card"><span>Uptime</span><strong>{_format_duration(system["uptime_seconds"])}</strong></div>
    </div></section>
    <section><h2>Channels & groups</h2><button type="button" data-open="target-new">+ Add channel / group</button>
    <div class="grid">{''.join(rows) or '<p>No publishing channels or groups yet.</p>'}</div>
    <dialog id="target-new"><button type="button" data-close>Close</button><h2>New channel or group</h2>
    <form method="post" action="/target"><input type="hidden" name="csrf" value="{csrf}">
    <label>Name<input name="label" placeholder="Name" required></label>
    <label>Telegram ID or username<input name="chat_id" placeholder="@channel or -100..." required></label>
    <label>Purpose / notes<textarea name="description" maxlength="500" rows="3"></textarea></label>
    <label><input type="checkbox" name="enabled" checked> Enabled</label><button>Save</button></form></dialog></section>
    <form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}"><button>Log out</button></form>
    <script nonce="{request['csp_nonce']}">
    const fields={json.dumps({key: list(value) for key,value in routing_rules.filter_fields().items()})};
    document.querySelectorAll('[data-open]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.open).showModal()));
    document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
    document.querySelectorAll('[data-confirm]').forEach(b=>b.closest('form').addEventListener('submit',e=>{{
      if(!confirm(b.dataset.confirm)) e.preventDefault();
    }}));
    const operators=[
      ['eq','='],['ne','≠'],['contains','contains'],['not_contains','does not contain'],
      ['gt','>'],['gte','≥'],['lt','<'],['lte','≤'],['empty','is empty'],['not_empty','is not empty']
    ];
    document.querySelectorAll('.conditions-editor').forEach(editor=>{{
      const form=editor.closest('form');const category=form.querySelector('select[name="category"]');
      const rows=editor.querySelector('.condition-rows');const hidden=editor.querySelector('input[name="conditions_json"]');
      const addButton=editor.querySelector('.add-condition');
      let conditions=[];try{{conditions=JSON.parse(editor.dataset.conditions||'[]')}}catch(error){{conditions=[]}}
      const option=(select,value,label)=>{{const item=document.createElement('option');item.value=value;item.textContent=label;select.append(item);}};
      async function suggestions(field,input,list){{
        list.replaceChildren();if(!field.value)return;
        try{{const url='/filter-values?category='+encodeURIComponent(category.value)+'&field='+encodeURIComponent(field.value);
          const response=await fetch(url,{{credentials:'same-origin'}});if(!response.ok)return;
          for(const value of await response.json()){{const item=document.createElement('option');item.value=value;list.append(item);}}
        }}catch(error){{}}
      }}
      function render(){{
        rows.replaceChildren();
        conditions.forEach((condition,index)=>{{
          const row=document.createElement('div');row.className='condition-row';
          const join=document.createElement('select');join.className='condition-join';option(join,'and','AND');option(join,'or','OR');join.value=condition.join||'and';
          const field=document.createElement('select');field.className='condition-field';
          for(const name of (fields[category.value]||[]))option(field,name,name.replaceAll('_',' '));
          field.value=condition.field||field.value;
          const operator=document.createElement('select');operator.className='condition-operator';
          for(const [value,label] of operators)option(operator,value,label);operator.value=condition.operator||'eq';
          const value=document.createElement('input');value.className='condition-value';value.value=condition.value||'';value.placeholder='Value';
          const list=document.createElement('datalist');list.id='condition-values-'+form.elements.target_id.value+'-'+(form.elements.id.value||'new')+'-'+index;
          value.setAttribute('list',list.id);
          const remove=document.createElement('button');remove.type='button';remove.className='danger';remove.textContent='Remove';
          const syncValue=()=>{{const noValue=new Set(['empty','not_empty']);value.disabled=noValue.has(operator.value);if(value.disabled)value.value='';}};
          field.addEventListener('change',()=>suggestions(field,value,list));operator.addEventListener('change',syncValue);
          remove.addEventListener('click',()=>{{conditions.splice(index,1);render();}});
          row.append(join,field,operator,value,list,remove);rows.append(row);syncValue();suggestions(field,value,list);
        }});
        sync();
      }}
      function sync(){{
        const next=[];rows.querySelectorAll('.condition-row').forEach((row,index)=>{{
          next.push({{join:index===0?'and':row.querySelector('.condition-join').value,
            field:row.querySelector('.condition-field').value,
            operator:row.querySelector('.condition-operator').value,
            value:row.querySelector('.condition-value').disabled?'':row.querySelector('.condition-value').value}});
        }});conditions=next;hidden.value=JSON.stringify(next);
      }}
      rows.addEventListener('change',sync);rows.addEventListener('input',sync);
      addButton.addEventListener('click',()=>{{const available=fields[category.value]||[];if(!available.length)return;
        conditions.push({{join:'and',field:available[0],operator:'eq',value:''}});render();}});
      category.addEventListener('change',()=>{{conditions=[];render();}});
      form.addEventListener('submit',sync);render();
    }});
    </script>
    </body></html>'''
    return web.Response(text=body, content_type="text/html")


def _rule_form(rule, target, csrf):
    rule = rule or {}
    conditions = routing_rules.effective_conditions(rule) if rule.get("id") else []
    encoded_conditions = _escape(json.dumps(conditions, ensure_ascii=False, separators=(",", ":")))
    default_category = routing_rules.categories()[0] if routing_rules.categories() else ""
    return f'''<form method="post" action="/rule"><input type="hidden" name="csrf" value="{csrf}">
        <input type="hidden" name="id" value="{rule.get('id','')}">
        <input type="hidden" name="target_id" value="{target['id']}">
        <label>Rule name<input name="name" placeholder="Rule name" value="{_escape(rule.get('name'))}" required></label>
        <label>Topic / category {_select('category', routing_rules.categories(), rule.get('category', default_category))}</label>
        <label>Source channel (optional)<input name="source_channel" placeholder="@source_channel" value="{_escape(rule.get('source_channel'))}"></label>
        <div class="conditions-editor" data-conditions="{encoded_conditions}">
        <p class="hint">Conditions use the real columns currently stored for the selected topic. Add as many as needed; AND/OR is evaluated from left to right.</p>
        <div class="condition-rows"></div>
        <button type="button" class="add-condition">+ Add condition</button>
        <input type="hidden" name="conditions_json" value="[]"></div>
        <label>Publishing <select name="delivery_mode">
            <option value="manual" {"selected" if rule.get('delivery_mode','manual')=='manual' else ''}>Manual selection</option>
            <option value="auto" {"selected" if rule.get('delivery_mode')=='auto' else ''}>Automatic</option>
        </select></label>
        <label>Priority<input name="priority" type="number" value="{rule.get('priority',100)}" required></label>
        <label><input type="checkbox" name="enabled" {"checked" if rule.get('enabled',1) else ""}> Enabled</label>
        <label><input type="checkbox" name="continue" {"checked" if not rule.get('stop_on_match',1) else ""}> Continue after match</label>
        <button>Save rule</button></form>'''


def _rule_panel(rule, target, csrf, request):
    page = 0
    if str(getattr(request, "query", {}).get("rule", "")) == str(rule["id"]):
        try:
            page = min(max(int(request.query.get("page", "0")), 0), 1000)
        except ValueError:
            pass
    _, counts, records = routing_rules.rule_matches(rule["id"], limit=20, offset=page * 20)
    mode = rule.get("delivery_mode") or "auto"
    paused = routing_rules.is_rate_limited()
    entries = []
    for record in records:
        snippet = str(next((record.get(key) for key in ("title", "job_title", "cleaned_text", "raw_text", "description")
                            if record.get(key)), "Ad details unavailable"))[:220]
        try:
            if record["ai_category"] == "transferlist":
                preview = TelegramTransferPublisher.format_ad(record, record)
            else:
                from routed_publisher import _plain_ad
                preview = _plain_ad(record)
        except (ValueError, KeyError, TypeError, TransferTelegramPublishError):
            preview = ""
        preview = preview or str(record.get("cleaned_text") or record.get("raw_text") or snippet)
        status = record.get("delivery_status") or "not sent"
        can_send = (mode == "manual" and rule["enabled"] and target["enabled"]
                    and target["connection_status"] != "disconnected" and
                    status not in {"sent", "sending", "uncertain"} and not paused)
        send = (f'''<form method="post" action="/rule/send"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="rule_id" value="{rule['id']}">
            <input type="hidden" name="message_id" value="{record['message_row_id']}">
            <button>Send this ad</button></form>''' if can_send else "")
        can_resend = (status == "sent" and rule["enabled"] and target["enabled"]
                      and target["connection_status"] != "disconnected" and not paused)
        resend = (f'''<form method="post" action="/rule/resend"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="rule_id" value="{rule['id']}">
            <input type="hidden" name="message_id" value="{record['message_row_id']}">
            <button data-confirm="This ad was already delivered to this channel. Send another copy?">Send again</button>
            </form>''' if can_resend else "")
        uncertain = (f'''<form method="post" action="/retry"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="message_id" value="{record['message_row_id']}">
            <input type="hidden" name="target_id" value="{target['id']}">
            <button data-confirm="Telegram may already have posted this ad. Check the channel before retrying.">Review and retry</button>
            </form>''' if status == "uncertain" else "")
        entries.append(f'''<div class="ad-row"><strong>Ad #{record['message_row_id']}</strong>
            · {_escape(record.get('channel_username') or '')} · {_escape(status)}
            <p>{_escape(snippet)}</p><details><summary>Full ad preview</summary>
            <pre>{_escape(preview[:4000])}</pre></details>{send}{resend}{uncertain}</div>''')
    nav = ""
    for label, p in (("Previous", page - 1), ("Next", page + 1)):
        if p >= 0 and p * 20 < counts["total"] and (p != page):
            nav += (f'<a href="/?channel={target["id"]}&rule={rule["id"]}&page={p}'
                    f'#rule-{rule["id"]}">{label}</a> ')
    expanded = str(getattr(request, "query", {}).get("rule", "")) == str(rule["id"])
    return f'''<div class="rule-panel" id="rule-{rule['id']}"><div class="rule-title">
        <h4>{_escape(rule['name'])}</h4><span class="hint">{counts['total']} matching ads ·
        {counts['sent']} delivered to this channel · {mode}</span>
        <form method="post" action="/rule/delete"><input type="hidden" name="csrf" value="{csrf}">
        <input type="hidden" name="rule_id" value="{rule['id']}">
        <input type="hidden" name="target_id" value="{target['id']}">
        <button class="danger" data-confirm="Delete this rule? Existing delivery history will remain.">Delete rule</button>
        </form></div><details><summary>Edit rule</summary>{_rule_form(rule, target, csrf)}</details>
        <details {'open' if expanded else ''}><summary>Matching ads ({counts['total']})</summary>
        {''.join(entries) or '<p>No matching ads yet.</p>'}<p>{nav}</p></details></div>'''


async def save_target(request):
    data = await request.post()
    try:
        routing_rules.save_target(data.get("label", ""), data.get("chat_id", ""),
                                  "enabled" in data, data.get("id") or None,
                                  data.get("description", ""))
    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    saved = next((target for target in routing_rules.list_targets()
                  if target["chat_id"] == data.get("chat_id", "").strip()), None)
    if saved:
        await _check_target_connection(saved)
        raise web.HTTPSeeOther(f"/?channel={saved['id']}#channel-{saved['id']}")
    raise web.HTTPSeeOther("/")


async def save_rule(request):
    data = await request.post()
    try:
        routing_rules.save_rule(
            data.get("name", ""), data.get("category", ""), "", "either", data.get("target_id"),
            priority=data.get("priority", 100), enabled="enabled" in data,
            stop_on_match="continue" not in data, rule_id=data.get("id") or None,
            source_channel=data.get("source_channel", ""),
            delivery_mode=data.get("delivery_mode", "auto"),
            conditions=data.get("conditions_json", "[]"))
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    target_id = int(data.get("target_id"))
    raise web.HTTPSeeOther(f"/?channel={target_id}#channel-{target_id}")


async def delete_rule(request):
    data = await request.post()
    try:
        target_id = int(data.get("target_id", ""))
        routing_rules.delete_rule(data.get("rule_id"), target_id)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    raise web.HTTPSeeOther(f"/?channel={target_id}#channel-{target_id}")


async def send_selected(request):
    data = await request.post()
    try:
        record, rule = routing_rules.selected_pair(data.get("rule_id"), data.get("message_id"))
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    from routed_publisher import RoutedPublisher
    result = await RoutedPublisher().publish_pending(pairs=[(record, rule)])
    notice = "&notice=rate_limited" if result.get("rate_limited") else ""
    raise web.HTTPSeeOther(f"/?channel={rule['target_id']}&rule={rule['id']}{notice}#rule-{rule['id']}")


async def resend_selected(request):
    data = await request.post()
    try:
        record, rule = routing_rules.selected_resend_pair(data.get("rule_id"), data.get("message_id"))
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    from routed_publisher import RoutedPublisher
    result = await RoutedPublisher().publish_pending(
        pairs=[(record, rule)], resend=True, requested_by=request["session"]["admin_id"])
    notice = "&notice=rate_limited" if result.get("rate_limited") else ""
    raise web.HTTPSeeOther(
        f"/?channel={rule['target_id']}&rule={rule['id']}{notice}#rule-{rule['id']}")


async def filter_values(request):
    try:
        values = routing_rules.distinct_filter_values(
            request.query.get("category", ""), request.query.get("field", ""))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response(values)


_DATA_CSS = """
* {box-sizing:border-box}
body {font:15px/1.5 system-ui,sans-serif;background:#f3f6fb;color:#192333;
      max-width:1500px;margin:0 auto;padding:1.5rem}
h1 {margin:.2rem 0}.muted {color:#536479}
.tabs,.table-tabs,.pagination {display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin:1rem 0}
.tabs a,.table-tabs a,.pagination a {padding:.5rem .8rem;background:white;color:#1957b8;
      text-decoration:none;border-radius:8px;border:1px solid #d9e2ee}
.tabs .active,.table-tabs .active {background:#1957b8;color:white}
section {padding:1rem;background:white;border:1px solid #dce4ef;border-radius:14px}
.scroll {overflow:auto;max-height:75vh}
table {border-collapse:separate;border-spacing:0;width:max-content;min-width:100%}
th,td {padding:.55rem .7rem;border-bottom:1px solid #e7edf4;border-right:1px solid #edf1f6;
       vertical-align:top;min-width:120px;max-width:340px;overflow-wrap:anywhere}
th {position:sticky;top:0;background:#eaf0f9;text-align:left;z-index:2}
td:first-child,th:first-child {position:sticky;left:0;z-index:1;background:#f7f9fd;min-width:64px}
th:first-child {z-index:3;background:#eaf0f9}
.edit-cell {display:block;border:0;background:transparent;color:#164e91;text-align:left;
            cursor:pointer;font:inherit;width:100%;padding:0;overflow-wrap:anywhere}
.edit-cell:hover {text-decoration:underline}
.readonly {color:#4e5e70}.null {color:#79869a;font-style:italic}
.cell-editor {display:grid;gap:.45rem;min-width:230px}
.cell-editor textarea {font:inherit;min-height:80px;width:100%;resize:vertical}
.cell-editor button {border:0;border-radius:6px;padding:.35rem .6rem;background:#1957b8;color:white;cursor:pointer}
.cell-editor .cancel {background:#e8eef7;color:#254360}
.cell-editor label {font-size:.9rem}.error {color:#ad302b}
.filter-form {margin:0}.filter-actions {display:flex;gap:.55rem;align-items:center;flex-wrap:wrap;
              margin:.35rem 0 .8rem}
.filter-actions button,.filter-actions a {font:inherit;padding:.45rem .75rem;border-radius:7px;
              text-decoration:none;cursor:pointer}
.filter-actions button {border:0;background:#1957b8;color:white}
.filter-actions a {border:1px solid #c8d3e2;background:white;color:#1957b8}
.column-name {display:block;font-weight:700;margin-bottom:.4rem}
.filter-control {display:grid;grid-template-columns:minmax(92px,auto) minmax(95px,1fr);gap:.35rem}
.filter-control select,.filter-control input {font:12px/1.25 system-ui;padding:.35rem;border:1px solid #bcc9d9;
              border-radius:6px;min-width:0;width:100%;background:white}
th.filtered {background:#dce9fb}
.filter-summary {font-size:.9rem;color:#31597f}
"""


_DATA_SCRIPT = r"""
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const table = document.querySelector('[data-table]').dataset.table;
function preview(value) {
  if (value === null) return 'NULL';
  const text = String(value);
  if (!text) return '(empty)';
  return text.replace(/\s+/g, ' ').slice(0, 110) + (text.length > 110 ? '…' : '');
}
document.querySelectorAll('button.edit-cell').forEach(button => button.addEventListener('click', async () => {
  const td = button.closest('td');
  if (td.querySelector('.cell-editor')) return;
  button.disabled = true;
  const url = '/data/cell?table=' + encodeURIComponent(table) + '&id=' + encodeURIComponent(td.dataset.id)
              + '&column=' + encodeURIComponent(td.dataset.column);
  try {
    const response = await fetch(url, {credentials:'same-origin'});
    if (!response.ok) throw Error(await response.text());
    const original = (await response.json()).value;
    const form = document.createElement('form'); form.className = 'cell-editor';
    const input = document.createElement('textarea'); input.value = original === null ? '' : String(original);
    input.setAttribute('aria-label', 'Edit ' + td.dataset.column + ' for row ' + td.dataset.id);
    const nullLabel = document.createElement('label');
    const clear = document.createElement('input'); clear.type = 'checkbox';
    nullLabel.append(clear, document.createTextNode(' Save as NULL'));
    const controls = document.createElement('div');
    const save = document.createElement('button'); save.type='submit';save.textContent='Save';
    const cancel = document.createElement('button');cancel.type='button';cancel.className='cancel';cancel.textContent='Cancel';
    controls.append(save,cancel);
    const error = document.createElement('span');error.className='error';error.setAttribute('role','alert');
    form.append(input,nullLabel,controls,error);td.append(form);
    cancel.addEventListener('click', () => {form.remove();button.disabled=false;});
    clear.addEventListener('change', () => {input.disabled=clear.checked;});
    form.addEventListener('submit', async event => {
      event.preventDefault();save.disabled=true;error.textContent='';
      const payload = new URLSearchParams({csrf, table, id:td.dataset.id, column:td.dataset.column,
        expected:JSON.stringify(original), value:input.value, make_null:clear.checked?'1':'0'});
      try {
        const result = await fetch('/data/cell',{method:'POST',body:payload,credentials:'same-origin'});
        if (!result.ok) throw Error(await result.text());
        const updated = (await result.json()).value;
        button.textContent=preview(updated);button.classList.toggle('null',updated===null);
        form.remove();button.disabled=false;
      } catch (exc) {error.textContent=exc.message;save.disabled=false;}
    });
    input.focus();
  } catch (exc) {button.disabled=false;window.alert('Unable to open cell: '+exc.message);}
}));
document.querySelectorAll('.filter-op').forEach(select => {
  const input = select.closest('.filter-control').querySelector('.filter-value');
  const noValue = new Set(['null','not_null','empty','not_empty']);
  const sync = () => {
    input.disabled = noValue.has(select.value);
    input.placeholder = input.disabled ? 'No value needed' : 'Value…';
  };
  select.addEventListener('change', sync);
  sync();
});
"""


def _requested_data_filters(query):
    filters = {}
    for key, value in query.items():
        if key.startswith("op_"):
            filters.setdefault(key[3:], {})["op"] = value
        elif key.startswith("f_"):
            filters.setdefault(key[2:], {})["value"] = value
    return filters


def _data_url(table, page_number=None, filters=None):
    params = [("table", table)]
    if page_number is not None:
        params.append(("page", str(page_number)))
    for column, spec in (filters or {}).items():
        params.append((f"op_{column}", spec["op"]))
        if spec.get("value", "") != "":
            params.append((f"f_{column}", spec["value"]))
    return "/data?" + urlencode(params)


def _filter_header(name, data_type, active):
    numeric = backoffice_data.numeric_filter_type(data_type)
    choices = ([("eq", "="), ("ne", "≠"), ("gt", ">"), ("gte", "≥"), ("lt", "<"), ("lte", "≤"),
                ("null", "NULL"), ("not_null", "Not NULL")]
               if numeric else
               [("contains", "Contains"), ("not_contains", "Not contains"), ("eq", "Exact"),
                ("ne", "Not equal"), ("starts", "Starts with"), ("empty", "Empty"),
                ("not_empty", "Not empty"), ("null", "NULL"), ("not_null", "Not NULL")])
    current_op = active.get("op") if active else ("eq" if numeric else "contains")
    current_value = active.get("value", "") if active else ""
    options = "".join(f'<option value="{value}" {"selected" if value == current_op else ""}>{label}</option>'
                      for value, label in choices)
    filtered = "filtered" if active else ""
    escaped_name = _escape(name)
    return f'''<th class="{filtered}"><span class="column-name">{escaped_name}</span>
        <div class="filter-control">
        <select class="filter-op" name="op_{escaped_name}" aria-label="Filter operator for {escaped_name}">{options}</select>
        <input class="filter-value" name="f_{escaped_name}" value="{_escape(current_value)}"
        placeholder="Value…" aria-label="Filter value for {escaped_name}"></div></th>'''


async def data_page(request):
    table = request.query.get("table", "messages")
    requested_filters = _requested_data_filters(request.query)
    try:
        result = backoffice_data.page(table, request.query.get("page", "1"), requested_filters)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    names = list(result["columns"])
    editable = set(result["editable"])
    cells = []
    for row in result["rows"]:
        columns = []
        for name in names:
            value = row[name]
            label = "NULL" if value is None else str(value).replace("\n", " ")[:110]
            if value is not None and len(str(value)) > 110:
                label += "…"
            if value == "":
                label = "(empty)"
            label = _escape(label)
            if name in editable:
                columns.append(f'''<td data-id="{row['id']}" data-column="{_escape(name)}">
                    <button type="button" class="edit-cell {'null' if value is None else ''}"
                    title="Edit this cell">{label}</button></td>''')
            else:
                columns.append(f'<td class="readonly">{label}</td>')
        cells.append('<tr>' + ''.join(columns) + '</tr>')
    nav = []
    for name in backoffice_data.TABLES:
        nav.append(f'''<a href="/data?table={name}" {'class="active"' if name == table else ''}>
            {name}</a>''')
    pagination = []
    for title, number in (("First", 1), ("Previous", result["page"]-1),
                          ("Next", result["page"]+1), ("Last", result["pages"])):
        if 1 <= number <= result["pages"] and number != result["page"]:
            pagination.append(f'<a href="{_escape(_data_url(table, number, result["filters"]))}">{title}</a>')
    headers = "".join(_filter_header(name, result["columns"][name], result["filters"].get(name))
                      for name in names)
    filter_count = len(result["filters"])
    filter_summary = (f'<span class="filter-summary">{filter_count} active filter'
                      f'{"s" if filter_count != 1 else ""}</span>' if filter_count else "")
    csrf = _escape(request["session"]["csrf"])
    content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="csrf-token" content="{csrf}"><title>Telclaw · Database</title>
    <style>{_DATA_CSS}</style></head><body><h1>Telclaw Back Office</h1>
    <nav class="tabs" aria-label="Back office sections"><a href="/">Publishing</a>
    <a class="active" href="/data">Database</a><a href="/health">System Health</a><a href="/settings">Settings</a></nav>
    <p class="muted">Browse and edit ad data. IDs, links between tables and pipeline controls are read only.</p>
    <nav class="table-tabs" aria-label="Data tables">{''.join(nav)}</nav>
    <section data-table="{table}"><p>{result['total']} rows · Page {result['page']} of {result['pages']}
    · {backoffice_data.PAGE_SIZE} per page. Click a blue cell to edit it.</p>
    <form method="get" action="/data" class="filter-form"><input type="hidden" name="table" value="{_escape(table)}">
    <div class="filter-actions"><button type="submit">Apply filters</button>
    <a href="/data?table={_escape(table)}">Clear filters</a>{filter_summary}</div>
    <div class="scroll"><table><thead><tr>{headers}</tr></thead>
    <tbody>{''.join(cells) or f'<tr><td colspan="{len(names)}">No rows match these filters.</td></tr>'}</tbody>
    </table></div></form><nav class="pagination">{''.join(pagination)}</nav></section>
    <script nonce="{request['csp_nonce']}">{_DATA_SCRIPT}</script></body></html>'''
    return web.Response(text=content, content_type="text/html")


def _fmt_bytes(value):
    if value is None:
        return "Unavailable"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _health_badge(state):
    value = str(state or "unknown").lower()
    css = "ok" if value in {"healthy", "running", "connected", "ok", "enabled", "polling"} else (
        "bad" if value in {"failed", "error", "disconnected", "corrupt", "stopped"} else "warn")
    return f'<span class="health-badge {css}">{_escape(state)}</span>'


def _activity_details(value):
    if value in (None, "", {}):
        return "—"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    return _escape(str(value)[:5000])


async def health_page(request):
    report = backoffice_health.snapshot()
    monitor = get_telegram_monitor().runtime_status()
    db = report["database"]
    pipeline = report["pipeline"]
    bot = report["bot"]
    publishing = report["publishing"]

    failures = (pipeline["processing_failed"] + pipeline["classification_failed"]
                + pipeline["ai_failed"] + pipeline["advertio_failed"])
    backlog = (pipeline["processing_pending"] + pipeline["classification_pending"]
               + pipeline["ai_pending"] + pipeline["advertio_pending"])
    overall = "HEALTHY" if db["healthy"] and failures == 0 else "WARNING"

    pipeline_cards = "".join(
        f'''<div class="metric"><span>{_escape(label)}</span><strong>{int(value):,}</strong></div>'''
        for label, value in (
            ("Total messages", pipeline["total"]),
            ("Crawled channels", pipeline["channels"]),
            ("Pipeline backlog", backlog),
            ("Failed items", failures),
            ("Processing pending", pipeline["processing_pending"]),
            ("Classification pending", pipeline["classification_pending"]),
            ("AI pending", pipeline["ai_pending"]),
            ("Advertio pending", pipeline["advertio_pending"]),
        )
    )
    stage_rows = "".join(
        f"<tr><th>{_escape(label)}</th><td>{int(pending):,}</td><td>{int(failed):,}</td><td>{_escape(last or '—')}</td></tr>"
        for label, pending, failed, last in (
            ("Processing", pipeline["processing_pending"], pipeline["processing_failed"], pipeline["last_processing"]),
            ("Classification", pipeline["classification_pending"], pipeline["classification_failed"], pipeline["last_processing"]),
            ("AI extraction", pipeline["ai_pending"], pipeline["ai_failed"], pipeline["last_ai"]),
            ("Advertio", pipeline["advertio_pending"], pipeline["advertio_failed"], pipeline["last_advertio"]),
        )
    )
    database_rows = "".join(
        f"<tr><td>{_escape(name)}</td><td>{int(count):,}</td></tr>"
        for name, count in db["row_counts"].items()
    )
    daily_rows = "".join(
        f"""<tr><td>{_escape(row.get('day') or '—')}</td><td>{int(row.get('crawled') or 0):,}</td>
        <td>{int(row.get('processed') or 0):,}</td><td>{int(row.get('classified') or 0):,}</td>
        <td>{int(row.get('ai_processed') or 0):,}</td><td>{int(row.get('advertio_sent') or 0):,}</td>
        <td>{int(row.get('failed') or 0):,}</td></tr>"""
        for row in report["daily"]
    )
    channel_rows = "".join(
        f"""<tr><td>@{_escape(row.get('channel_username') or '')}</td>
        <td>{_escape(row.get('configured_name') or row.get('channel_name') or '—')}</td>
        <td>{_escape(row.get('configured_category') or '—')}</td>
        <td>{'yes' if row.get('configured') else 'no'}</td>
        <td>{int(row.get('messages') or 0):,}</td>
        <td>{_escape(row.get('first_message') or '—')}</td>
        <td>{_escape(row.get('last_message') or '—')}</td>
        <td>{int(row.get('failed') or 0):,}</td>
        <td>{_health_badge(row.get('crawl_state') or 'unknown')}</td></tr>"""
        for row in report["channels"]
    )
    activity_rows = "".join(
        f"""<tr><td>{_escape(row.get('created_at') or '—')}</td><td>{_escape(row.get('kind') or '—')}</td>
        <td>{_health_badge(row.get('level') or 'INFO')}</td><td>{_escape(row.get('source') or '—')}</td>
        <td>{_escape(row.get('message') or '—')}</td><td><pre>{_activity_details(row.get('details_data'))}</pre></td></tr>"""
        for row in report["activity"]
    )
    edit_rows = "".join(
        f"""<tr><td>{_escape(row.get('edited_at') or '—')}</td><td>{_escape(row.get('admin_id'))}</td>
        <td>{_escape(row.get('table_name'))} #{_escape(row.get('row_id'))}</td><td>{_escape(row.get('column_name'))}</td>
        <td>{_escape(str(row.get('old_value'))[:180] if row.get('old_value') is not None else 'NULL')}</td>
        <td>{_escape(str(row.get('new_value'))[:180] if row.get('new_value') is not None else 'NULL')}</td></tr>"""
        for row in report["edits"]
    )
    publish_rows = "".join(
        f"<tr><td>{_escape(kind)}</td><td>{_escape(status)}</td><td>{int(count):,}</td></tr>"
        for kind, statuses in (("delivery", publishing["deliveries"]), ("resend", publishing["resends"]))
        for status, count in statuses.items()
    )
    runtime_rows = "".join(
        f"<tr><th>{_escape(label)}</th><td>{_health_badge(value)}</td></tr>"
        for label, value in (
            ("Overall", overall),
            ("Database integrity", "OK" if db["healthy"] else db["quick_check"]),
            ("Telegram monitor", "POLLING" if monitor["polling"] else ("ENABLED" if monitor["enabled"] else "STOPPED")),
            ("Telegram token", "configured" if bot["token_configured"] else "missing"),
            ("Back office", "enabled" if bot["backoffice_enabled"] else "disabled"),
            ("Classification", "enabled" if bot["classification_enabled"] else "disabled"),
            ("AI extraction", "enabled" if bot["extraction_enabled"] else "disabled"),
            ("Advertio", "enabled" if bot["advertio_enabled"] else "disabled"),
        )
    )

    content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Telclaw · System Health</title>
    <style>{_DATA_CSS}
    body{{max-width:1500px}}.health-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.8rem}}
    .metric{{padding:1rem;border:1px solid #dde5ef;border-radius:12px;background:#f8faff}}
    .metric span{{display:block;color:#536479;font-size:.85rem}}.metric strong{{display:block;font-size:1.5rem;margin-top:.25rem}}
    .health-badge{{display:inline-block;padding:.2rem .55rem;border-radius:999px;background:#e8edf4;font-size:.82rem}}
    .health-badge.ok{{background:#d9f4e5;color:#17633e}}.health-badge.warn{{background:#fff1c8;color:#755300}}
    .health-badge.bad{{background:#ffe0da;color:#9b2c1f}}.health-sections{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:1rem}}
    .health-sections section{{margin:0}}section{{margin:1rem 0}}pre{{white-space:pre-wrap;max-width:560px;margin:0;font:12px/1.45 ui-monospace,monospace}}
    .refresh{{float:right}}@media(max-width:700px){{.health-sections{{grid-template-columns:1fr}}}}
    </style></head><body><h1>Telclaw · System Health</h1>
    <nav class="tabs" aria-label="Back office sections"><a href="/">Publishing</a>
    <a href="/data">Database</a><a class="active" href="/health">System Health</a><a href="/settings">Settings</a></nav>
    <p class="muted"><a class="refresh" href="/health">Refresh</a>Operational reports from SQLite and the running Telegram monitor.</p>
    <div class="health-grid">{pipeline_cards}</div>

    <div class="health-sections">
    <section><h2>Runtime & bot</h2><table><tbody>{runtime_rows}
    <tr><th>Monitor subscribers</th><td>{int(bot['active_subscribers']):,}</td></tr>
    <tr><th>Monitor update offset</th><td>{int(monitor['offset']):,}</td></tr></tbody></table></section>
    <section><h2>Database</h2><p>SQLite quick check: {_health_badge(db['quick_check'])}</p>
    <p>Database file: <strong>{_fmt_bytes(db['file_size'])}</strong> · allocated: <strong>{_fmt_bytes(db['allocated_bytes'])}</strong></p>
    <div class="scroll"><table><thead><tr><th>Table</th><th>Rows</th></tr></thead><tbody>
    {database_rows or '<tr><td colspan="2">No tracked tables.</td></tr>'}</tbody></table></div></section>
    </div>

    <section><h2>Pipeline queues & failures</h2><p>Last crawl: <strong>{_escape(pipeline['last_crawl'] or '—')}</strong></p>
    <div class="scroll"><table><thead><tr><th>Stage</th><th>Pending</th><th>Failed</th><th>Last activity</th></tr></thead>
    <tbody>{stage_rows}</tbody></table></div></section>

    <section><h2>Crawl reports · recent days</h2>
    <div class="scroll"><table><thead><tr><th>Date</th><th>Crawled</th><th>Processed</th><th>Classified</th>
    <th>AI processed</th><th>Advertio sent</th><th>Failed</th></tr></thead>
    <tbody>{daily_rows or '<tr><td colspan="7">No crawl data yet.</td></tr>'}</tbody></table></div></section>

    <section><h2>Crawled channels</h2><p class="muted">Configured sources are included even when no message has been stored yet.</p>
    <div class="scroll"><table><thead><tr><th>Channel</th><th>Name</th><th>Source group</th><th>Configured</th>
    <th>Messages</th><th>First</th><th>Last</th><th>Failed</th><th>State</th></tr></thead>
    <tbody>{channel_rows or '<tr><td colspan="9">No configured or crawled channels.</td></tr>'}</tbody></table></div></section>

    <section><h2>Robot &amp; system activity</h2><p class="muted">Crawler, processing, AI and error reports are persisted here from now on.</p>
    <div class="scroll"><table><thead><tr><th>Time (UTC)</th><th>Kind</th><th>Level</th><th>Source</th><th>Message</th><th>Details</th></tr></thead>
    <tbody>{activity_rows or '<tr><td colspan="6">No persisted activity yet.</td></tr>'}</tbody></table></div></section>

    <div class="health-sections">
    <section><h2>Publishing activity</h2><p>{publishing['targets']} channels / groups · {publishing['rules']} rules</p>
    <table><thead><tr><th>Type</th><th>Status</th><th>Count</th></tr></thead>
    <tbody>{publish_rows or '<tr><td colspan="3">No publishing attempts.</td></tr>'}</tbody></table></section>
    <section><h2>Recent database edits</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Admin</th><th>Row</th><th>Column</th><th>Before</th><th>After</th></tr></thead>
    <tbody>{edit_rows or '<tr><td colspan="6">No back-office edits yet.</td></tr>'}</tbody></table></div></section>
    </div>
    </body></html>'''
    return web.Response(text=content, content_type="text/html")



def _setting_display(value, secret=False):
    if not secret:
        return _escape(value if value not in (None, "") else "—")
    return "•••••••• (configured)" if str(value or "").strip() else "—"


def _setting_input(row, csrf):
    spec = row["spec"]
    if spec.readonly:
        return f'''<div class="setting-control"><input value="{_escape(row["effective"])}" disabled>
        <span class="muted">Bootstrap setting · edit in .env</span></div>'''

    value = row["override"] if row["override"] is not None else row["effective"]
    if spec.kind == "bool":
        selected = str(value).strip().lower() in {"1", "true", "yes", "on"}
        control = f'''<select name="value">
            <option value="true" {"selected" if selected else ""}>true</option>
            <option value="false" {"selected" if not selected else ""}>false</option>
        </select>'''
    elif spec.choices:
        control = '<select name="value">' + ''.join(
            f'<option value="{_escape(choice)}" {"selected" if str(value)==choice else ""}>{_escape(choice or "(disabled)")}</option>'
            for choice in spec.choices
        ) + '</select>'
    else:
        input_type = "password" if spec.secret else ("number" if spec.kind in {"int", "float"} else "text")
        step = ' step="any"' if spec.kind == "float" else ""
        minimum = f' min="{spec.minimum}"' if spec.minimum is not None else ""
        maximum = f' max="{spec.maximum}"' if spec.maximum is not None else ""
        shown = "" if spec.secret else str(value or "")
        placeholder = "Enter new secret; blank keeps current" if spec.secret else ""
        control = (f'<input type="{input_type}" name="value" value="{_escape(shown)}"'
                   f'{step}{minimum}{maximum} placeholder="{_escape(placeholder)}">')
        if spec.secret:
            control += '<label class="empty-secret"><input type="checkbox" name="set_empty" value="1"> override with empty value</label>'

    restart = '<span class="restart">restart required</span>' if spec.restart else '<span class="live">runtime setting</span>'
    return f'''<div class="setting-control"><form method="post" action="/settings/save" class="setting-form">
        <input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="key" value="{_escape(spec.key)}">
        {control}<button>Save override</button>{restart}</form>
        <form method="post" action="/settings/reset" class="setting-reset">
        <input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="key" value="{_escape(spec.key)}">
        <button class="secondary" {"disabled" if row["override"] is None else ""}>Use .env default</button></form></div>'''


async def settings_page(request):
    csrf = _escape(request["session"]["csrf"])
    grouped = {}
    for row in backoffice_settings.rows():
        grouped.setdefault(row["spec"].group, []).append(row)

    sections = []
    for group, rows in grouped.items():
        items = []
        for row in rows:
            spec = row["spec"]
            source_class = "override" if row["source"] == "Back Office" else "default"
            items.append(f'''<div class="setting-row">
                <div class="setting-meta"><code>{_escape(spec.key)}</code>
                <span class="source {source_class}">{_escape(row["source"])}</span>
                <div class="setting-values"><span>Effective: <strong>{_setting_display(row["effective"], spec.secret)}</strong></span>
                <span>.env default: {_setting_display(row["default"], spec.secret)}</span></div></div>
                {_setting_input(row, csrf)}
            </div>''')
        sections.append(f'<section><h2>{_escape(group)}</h2>{"".join(items)}</section>')

    notice = request.query.get("notice", "")
    notice_html = {
        "saved": '<p class="notice ok">Setting override saved. Restart Telclaw for restart-required services to rebuild.</p>',
        "reset": '<p class="notice ok">Override removed; the .env value is now the default again.</p>',
    }.get(notice, "")

    content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Telclaw · Settings</title>
    <style>{_DATA_CSS}
    body{{max-width:1350px}}.setting-row{{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(420px,1.5fr);
    gap:1rem;padding:1rem 0;border-bottom:1px solid #e3e8ef;align-items:center}}
    .setting-row:last-child{{border-bottom:0}}.setting-meta code{{font-weight:700}}
    .setting-values{{display:flex;gap:1rem;flex-wrap:wrap;margin-top:.35rem;color:#536479;font-size:.86rem}}
    .source{{display:inline-block;margin-left:.5rem;padding:.12rem .45rem;border-radius:999px;font-size:.75rem}}
    .source.override{{background:#d9f4e5;color:#17633e}}.source.default{{background:#e8edf4;color:#46576b}}
    .setting-control{{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}}.setting-form,.setting-reset{{display:flex;gap:.45rem;
    align-items:center;border:0;padding:0;margin:0}}.setting-form input[type=text],.setting-form input[type=password],
    .setting-form input[type=number],.setting-form select{{min-width:250px}}
    .secondary{{background:#66758a}}.restart{{color:#8a5700;font-size:.8rem}}.live{{color:#17633e;font-size:.8rem}}
    .empty-secret{{font-size:.8rem;color:#536479;display:flex;align-items:center;gap:.2rem}}
    .notice{{padding:.8rem 1rem;border-radius:10px}}.notice.ok{{background:#d9f4e5;color:#17633e}}
    @media(max-width:850px){{.setting-row{{grid-template-columns:1fr}}.setting-form input[type=text],
    .setting-form input[type=password],.setting-form input[type=number],.setting-form select{{min-width:180px;flex:1}}}}
    </style></head><body><h1>Telclaw · Settings</h1>
    <nav class="tabs" aria-label="Back office sections"><a href="/">Publishing</a>
    <a href="/data">Database</a><a href="/health">System Health</a><a class="active" href="/settings">Settings</a></nav>
    <p class="muted">Values from <code>.env</code> are defaults. A saved Back Office value overrides that default and is
    persisted in SQLite. Secret values are never rendered in the page. Settings marked restart required are saved immediately
    but need a Telclaw restart for already-created clients, listeners or workers to rebuild safely.</p>
    {notice_html}
    {"".join(sections)}
    </body></html>'''
    return web.Response(text=content, content_type="text/html")


async def save_setting(request):
    data = await request.post()
    key = str(data.get("key") or "")
    value = data.get("value", "")
    spec = next((item for item in backoffice_settings.SPECS if item.key == key), None)
    if spec is None:
        raise web.HTTPBadRequest(text="Unknown setting")
    if spec.secret and value == "" and data.get("set_empty") != "1":
        raise web.HTTPSeeOther("/settings")
    if spec.secret and data.get("set_empty") == "1":
        value = ""
    try:
        backoffice_settings.save_override(key, value, request["session"]["admin_id"])
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    raise web.HTTPSeeOther("/settings?notice=saved")


async def reset_setting(request):
    data = await request.post()
    try:
        backoffice_settings.reset_override(data.get("key", ""))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    raise web.HTTPSeeOther("/settings?notice=reset")


async def data_cell(request):
    try:
        result = backoffice_data.cell(request.query.get("table", ""), request.query.get("id", ""),
                                      request.query.get("column", ""))
    except backoffice_data.ConflictError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response(result)


async def save_data_cell(request):
    data = await request.post()
    try:
        expected = json.loads(data.get("expected", ""))
        if expected is not None and not isinstance(expected, (str, int, float)):
            raise ValueError("Invalid previous cell value")
        value = backoffice_data.update_cell(
            data.get("table", ""), data.get("id", ""), data.get("column", ""),
            data.get("value", ""), expected, request["session"]["admin_id"],
            make_null=data.get("make_null") == "1")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, backoffice_data.ConflictError):
            raise web.HTTPConflict(text=str(exc)) from exc
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response({"value": value})


async def logout(request):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM backoffice_sessions WHERE token_hash=?",
                     (_digest(request.cookies.get("telclaw_admin", "")),))
        conn.commit()
    finally:
        conn.close()
    response = web.HTTPSeeOther("/")
    response.del_cookie("telclaw_admin")
    raise response


async def retry_delivery(request):
    data = await request.post()
    try:
        routing_rules.retry_delivery(data.get("message_id"), data.get("target_id"))
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    target_id = int(data.get("target_id"))
    raise web.HTTPSeeOther(f"/?channel={target_id}#channel-{target_id}")


async def check_target(request):
    data = await request.post()
    try:
        target_id = int(data.get("target_id", ""))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="Invalid destination") from exc
    target = next((t for t in routing_rules.list_targets() if t["id"] == target_id), None)
    if target is None:
        raise web.HTTPNotFound(text="Destination not found")
    await _check_target_connection(target)
    raise web.HTTPSeeOther(f"/?channel={target_id}#channel-{target_id}")


async def _check_target_connection(target):
    target_id = target["id"]
    async def telegram(method, payload=None):
        async with session.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}",
                                json=payload or {}) as response:
            body = await response.json(content_type=None)
            if not response.ok or not body.get("ok"):
                raise RuntimeError(str(body.get("description") or f"Telegram HTTP {response.status}"))
            return body["result"]

    status, detail = "unknown", "Unable to check Telegram"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            bot = await telegram("getMe")
            chat = await telegram("getChat", {"chat_id": target["chat_id"]})
            membership = await telegram("getChatMember", {"chat_id": target["chat_id"], "user_id": bot["id"]})
            role = membership.get("status")
            can_send = (role in {"creator", "administrator", "member"} and
                        (chat.get("type") != "channel" or role in {"creator", "administrator"}) and
                        membership.get("can_post_messages", True) is not False and
                        membership.get("can_send_messages", True) is not False and
                        membership.get("is_member", True) is not False)
            status = "connected" if can_send else "disconnected"
            detail = (f"{chat.get('title') or target['chat_id']} · {role or 'unknown role'}"
                      if can_send else "Bot is not a member or lacks permission to post")
    except (aiohttp.ClientError, TimeoutError) as exc:
        detail = f"Telegram connection failed: {exc.__class__.__name__}"
    except (RuntimeError, ValueError) as exc:
        status, detail = "disconnected", str(exc)
    routing_rules.update_target_connection(target_id, status, detail)


def create_app():
    routing_rules.initialize()
    initialize_auth()
    backoffice_data.initialize()
    backoffice_settings.initialize()
    app = web.Application(middlewares=[_security])
    app.add_routes([web.get("/login", login), web.get("/", index),
                    web.get("/data", data_page), web.get("/health", health_page),
                    web.get("/settings", settings_page),
                    web.post("/settings/save", save_setting),
                    web.post("/settings/reset", reset_setting),
                    web.get("/data/cell", data_cell),
                    web.post("/data/cell", save_data_cell),
                    web.get("/filter-values", filter_values),
                    web.post("/target", save_target), web.post("/rule", save_rule),
                    web.post("/rule/delete", delete_rule), web.post("/rule/send", send_selected),
                    web.post("/rule/resend", resend_selected),
                    web.post("/retry", retry_delivery), web.post("/target/check", check_target),
                    web.post("/logout", logout)])
    return app
