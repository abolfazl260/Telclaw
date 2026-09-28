"""Private rule back office, served directly or behind a proxy."""
import hashlib
import html
import ipaddress
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from aiohttp import web
import aiohttp

import config
import backoffice_data
import routing_rules
from delivery.telegram_transfer_publisher import TelegramTransferPublisher, TransferTelegramPublishError
from monitoring.telegram_monitor import ADMIN_USER_IDS
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


def _escape(value):
    return html.escape(str(value or ""), quote=True)


def _select(name, values, selected):
    return f'<select name="{name}">' + "".join(
        f'<option value="{_escape(v)}" {"selected" if str(v)==str(selected) else ""}>{_escape(v)}</option>'
        for v in values) + "</select>"


async def index(request):
    csrf = _escape(request["session"]["csrf"])
    targets, rules = routing_rules.list_targets(), routing_rules.list_rules()
    rows = []
    for target in targets:
        assigned = [rule for rule in rules if rule["target_id"] == target["id"]]
        rule_rows = "".join(_rule_panel(rule, target, csrf, request) for rule in assigned)
        history = routing_rules.recent_deliveries(30, target['id'])
        history_rows = ''.join(f'''<tr><td>#{item['message_id']}</td><td>{_escape(item['status'])}</td>
            <td>{_escape(item['telegram_message_id'] or '—')}</td><td>{_escape(item['updated_at'])}</td>
            <td>{_escape(item['error'])}</td><td>'''
            + (f'''<form method="post" action="/retry"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="message_id" value="{item['message_id']}">
            <input type="hidden" name="target_id" value="{target['id']}"><button
            {'data-confirm="Telegram may already have posted this ad. Check the channel before retrying."' if item['status']=='uncertain' else ''}
            >{'Review and retry' if item['status']=='uncertain' else 'Retry'}</button></form>'''
            if item['status'] in {'retry', 'rejected', 'uncertain'} else '') + '</td></tr>' for item in history)
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
            <button>Save destination</button></form></details>
            <div class="subsection"><h3>Publishing rules</h3>{rule_rows or '<p>No rules for this channel yet.</p>'}
            <details><summary>Add a rule for this channel</summary>{_rule_form(None, target, csrf)}</details></div>
            <details class="subsection"><summary>Delivery history ({len(history)})</summary><div class="scroll"><table>
            <thead><tr><th>Message</th><th>Status</th><th>Telegram ID</th><th>Updated (UTC)</th><th>Error</th><th></th></tr></thead>
            <tbody>{history_rows or '<tr><td colspan="6">No deliveries yet.</td></tr>'}</tbody></table></div></details>
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
    .danger{{background:#a53732}}.ad-row{{border-top:1px solid #e4e9ef;padding:.65rem 0}}
    .ad-row p{{margin:.3rem 0}}.ad-row form{{display:inline-flex;padding:0}}
    .ad-row pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-height:350px;overflow:auto;background:#f5f7fb;padding:.7rem}}
    .tabs{{display:flex;gap:.6rem;margin:1rem 0}}.tabs a{{padding:.5rem .9rem;border-radius:8px;background:white;color:#1957b8;text-decoration:none}}
    .tabs a.active{{background:#1957b8;color:white}}
    </style></head><body><h1>Telclaw · Publishing rules</h1>
    <nav class="tabs" aria-label="Back office sections"><a href="/" class="active">Publishing</a>
    <a href="/data">Database</a></nav>
    {('<p class="badge">Telegram paused this send; please retry after its rate limit clears.</p>'
      if getattr(request, 'query', {}).get('notice') == 'rate_limited' else '')}
    <p class="hint">Rules run by priority. Unmatched ads are not published. Check that the bot can post to each destination.</p>
    <section><h2>Channels & groups</h2><button type="button" data-open="target-new">+ Add destination</button>
    <div class="grid">{''.join(rows) or '<p>No destinations yet.</p>'}</div>
    <dialog id="target-new"><button type="button" data-close>Close</button><h2>New channel or group</h2>
    <form method="post" action="/target"><input type="hidden" name="csrf" value="{csrf}">
    <label>Name<input name="label" placeholder="Name" required></label>
    <label>Telegram ID or username<input name="chat_id" placeholder="@channel or -100..." required></label>
    <label>Purpose / notes<textarea name="description" maxlength="500" rows="3"></textarea></label>
    <label><input type="checkbox" name="enabled" checked> Enabled</label><button>Save</button></form></dialog></section>
    <form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}"><button>Log out</button></form>
    <script nonce="{request['csp_nonce']}">
    const fields={json.dumps({key: list(value) for key,value in routing_rules.FILTER_FIELDS.items()})};
    document.querySelectorAll('[data-open]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.open).showModal()));
    document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
    document.querySelectorAll('[data-confirm]').forEach(b=>b.closest('form').addEventListener('submit',e=>{{
      if(!confirm(b.dataset.confirm)) e.preventDefault();
    }}));
    document.querySelectorAll('select[name="category"]').forEach(category=>{{
      const form=category.closest('form');const field=form.querySelector('select[name="filter_field"]');
      const value=form.querySelector('select[name="filter_value"]');
      const savedField=field.dataset.selected;const savedValue=value.dataset.selected;
      async function updateValues(selected){{value.replaceChildren();
        const add=(v,label)=>{{const opt=document.createElement('option');opt.value=v;opt.textContent=label;value.append(opt);}};
        add('','Any value');if(!field.value) return;
        try{{const url='/filter-values?category='+encodeURIComponent(category.value)+'&field='+encodeURIComponent(field.value);
          const response=await fetch(url,{{credentials:'same-origin'}});if(!response.ok) throw Error('Fetch failed');
          const values=await response.json();for(const v of values) add(v,v);
          if(selected && !values.includes(selected)) add(selected,selected+' (saved)');value.value=selected||'';
        }}catch(error){{add('','Values unavailable');if(selected){{add(selected,selected+' (saved)');value.value=selected;}}}}
      }}
      function updateFields(selected){{field.replaceChildren();for(const v of ['',...(fields[category.value]||[])]){{
        const option=document.createElement('option');option.value=v;option.textContent=v?v.replaceAll('_',' '):'All values';field.append(option);
      }}field.value=selected||'';updateValues(selected?savedValue:'');}}
      category.addEventListener('change',()=>updateFields(''));field.addEventListener('change',()=>updateValues(''));
      updateFields(savedField);
    }});
    </script>
    </body></html>'''
    return web.Response(text=body, content_type="text/html")


def _rule_form(rule, target, csrf):
    rule = rule or {}
    return f'''<form method="post" action="/rule"><input type="hidden" name="csrf" value="{csrf}">
        <input type="hidden" name="id" value="{rule.get('id','')}">
        <input type="hidden" name="target_id" value="{target['id']}">
        <input name="name" placeholder="Rule name" value="{_escape(rule.get('name'))}" required>
        {_select('category', routing_rules.CATEGORIES, rule.get('category','transferlist'))}
        <label>Category field <select name="filter_field" data-selected="{_escape(rule.get('filter_field'))}"></select></label>
        <label>Stored value <select name="filter_value" data-selected="{_escape(rule.get('filter_value'))}"><option value="">Any value</option></select></label>
        <input name="source_channel" placeholder="Source @channel" value="{_escape(rule.get('source_channel'))}">
        <label>Publishing <select name="delivery_mode">
            <option value="manual" {"selected" if rule.get('delivery_mode','manual')=='manual' else ''}>Manual selection</option>
            <option value="auto" {"selected" if rule.get('delivery_mode')=='auto' else ''}>Automatic</option>
        </select></label>
        <input name="priority" type="number" value="{rule.get('priority',100)}" required>
        <label><input type="checkbox" name="enabled" {"checked" if rule.get('enabled',1) else ""}> Enabled</label>
        <label><input type="checkbox" name="continue" {"checked" if not rule.get('stop_on_match',1) else ""}> Continue</label>
        <details><summary>Existing country, city and price filters</summary>
        <input name="country" maxlength="2" placeholder="Country code (TR)" value="{_escape(rule.get('country'))}">
        {_select('scope', routing_rules.SCOPES, rule.get('country_scope','either'))}
        <input name="origin_city" placeholder="Origin city" value="{_escape(rule.get('origin_city'))}">
        <input name="destination_city" placeholder="Destination city" value="{_escape(rule.get('destination_city'))}">
        <input name="min_price" type="number" step="any" min="0" placeholder="Min price" value="{_escape(rule.get('min_price'))}">
        <input name="max_price" type="number" step="any" min="0" placeholder="Max price" value="{_escape(rule.get('max_price'))}"></details>
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
        uncertain = (f'''<form method="post" action="/retry"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="message_id" value="{record['message_row_id']}">
            <input type="hidden" name="target_id" value="{target['id']}">
            <button data-confirm="Telegram may already have posted this ad. Check the channel before retrying.">Review and retry</button>
            </form>''' if status == "uncertain" else "")
        entries.append(f'''<div class="ad-row"><strong>Ad #{record['message_row_id']}</strong>
            · {_escape(record.get('channel_username') or '')} · {_escape(status)}
            <p>{_escape(snippet)}</p><details><summary>Full ad preview</summary>
            <pre>{_escape(preview[:4000])}</pre></details>{send}{uncertain}</div>''')
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
        routing_rules.save_rule(data.get("name", ""), data.get("category", ""),
                                data.get("country", ""), data.get("scope", ""),
                                data.get("target_id"), data.get("priority", 100),
                                "enabled" in data, "continue" not in data, data.get("id") or None,
                                data.get("source_channel", ""), data.get("origin_city", ""),
                                data.get("destination_city", ""), data.get("min_price"),
                                data.get("max_price"), data.get("filter_field", ""),
                                data.get("filter_value", ""), data.get("delivery_mode", "auto"))
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
"""


async def data_page(request):
    table = request.query.get("table", "messages")
    try:
        result = backoffice_data.page(table, request.query.get("page", "1"))
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
            pagination.append(f'<a href="/data?table={table}&page={number}">{title}</a>')
    csrf = _escape(request["session"]["csrf"])
    content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="csrf-token" content="{csrf}"><title>Telclaw · Database</title>
    <style>{_DATA_CSS}</style></head><body><h1>Telclaw Back Office</h1>
    <nav class="tabs" aria-label="Back office sections"><a href="/">Publishing</a>
    <a class="active" href="/data">Database</a></nav>
    <p class="muted">Browse and edit ad data. IDs, links between tables and pipeline controls are read only.</p>
    <nav class="table-tabs" aria-label="Data tables">{''.join(nav)}</nav>
    <section data-table="{table}"><p>{result['total']} rows · Page {result['page']} of {result['pages']}
    · {backoffice_data.PAGE_SIZE} per page. Click a blue cell to edit it.</p>
    <div class="scroll"><table><thead><tr>{''.join(f'<th>{_escape(name)}</th>' for name in names)}</tr></thead>
    <tbody>{''.join(cells) or f'<tr><td colspan="{len(names)}">No rows in this table.</td></tr>'}</tbody>
    </table></div><nav class="pagination">{''.join(pagination)}</nav></section>
    <script nonce="{request['csp_nonce']}">{_DATA_SCRIPT}</script></body></html>'''
    return web.Response(text=content, content_type="text/html")


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
    app = web.Application(middlewares=[_security])
    app.add_routes([web.get("/login", login), web.get("/", index),
                    web.get("/data", data_page), web.get("/data/cell", data_cell),
                    web.post("/data/cell", save_data_cell),
                    web.get("/filter-values", filter_values),
                    web.post("/target", save_target), web.post("/rule", save_rule),
                    web.post("/rule/delete", delete_rule), web.post("/rule/send", send_selected),
                    web.post("/retry", retry_delivery), web.post("/target/check", check_target),
                    web.post("/logout", logout)])
    return app
