"""Private rule back office. Run behind an HTTPS reverse proxy."""
import hashlib
import html
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from aiohttp import web

import config
import routing_rules
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


def issue_link(admin_id):
    """Issue a five-minute, single-use link only to approved Telegram admins."""
    if int(admin_id) not in ADMIN_USER_IDS:
        raise PermissionError("Not an admin")
    base = config.BACKOFFICE_PUBLIC_URL.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise RuntimeError("TELCLAW_BACKOFFICE_PUBLIC_URL must be a public HTTPS URL")
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
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
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
            AND used_at IS NULL AND expires_at>? AND admin_id IN (1485409432,266809220,7469291969)""",
            (_now().isoformat(), _digest(token), _now().isoformat()))
        if cursor.rowcount != 1:
            conn.rollback()
            raise web.HTTPForbidden(text="Invalid or expired link")
        row = conn.execute("SELECT admin_id FROM backoffice_links WHERE token_hash=?",
                           (_digest(token),)).fetchone()
        session_token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        conn.execute("INSERT INTO backoffice_sessions VALUES(?,?,?,?)",
                     (_digest(session_token), row["admin_id"], csrf,
                      (_now() + timedelta(hours=12)).isoformat()))
        conn.commit()
    finally:
        conn.close()
    response = web.HTTPSeeOther("/")
    response.set_cookie("telclaw_admin", session_token, httponly=True,
                        secure=True, samesite="Strict", max_age=43200, path="/")
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
        rows.append(f'''<form method="post" action="/target"><input type="hidden" name="csrf" value="{csrf}">
            <input type="hidden" name="id" value="{target['id']}">
            <input name="label" value="{_escape(target['label'])}" required>
            <input name="chat_id" value="{_escape(target['chat_id'])}" required>
            <label><input type="checkbox" name="enabled" {"checked" if target['enabled'] else ""}> Enabled</label>
            <button>Save destination</button></form>''')
    rule_rows = []
    for rule in rules:
        rule_rows.append(_rule_form(rule, targets, csrf))
    body = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Telclaw back office</title>
    <style>body{{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#f6f7fb;color:#192333}}
    section{{background:white;padding:1.3rem;margin:1.3rem 0;border-radius:12px}}
    form{{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;border-bottom:1px solid #ddd;padding:.8rem 0}}
    input,select,button{{font:inherit;padding:.55rem;max-width:100%}}button{{cursor:pointer;background:#1957b8;color:white;border:0;border-radius:6px}}
    input[name=country]{{width:4rem}}input[name=priority]{{width:5rem}}
    .hint{{color:#526174}} </style></head><body><h1>Telclaw · Publishing rules</h1>
    <p class="hint">Rules run by priority (smallest number first). First match wins unless Continue is checked.
    An ad without a matching rule is not published. Countries use two-letter codes, e.g. TR.</p>
    <section><h2>Destinations</h2>{''.join(rows)}
    <h3>Add destination</h3><form method="post" action="/target"><input type="hidden" name="csrf" value="{csrf}">
    <input name="label" placeholder="Name" required><input name="chat_id" placeholder="@channel or -100..." required>
    <label><input type="checkbox" name="enabled" checked> Enabled</label><button>Add</button></form></section>
    <section><h2>Rules</h2>{''.join(rule_rows) if rule_rows else '<p>No rules yet.</p>'}
    <h3>Add rule</h3>{_rule_form(None, targets, csrf)}</section>
    <form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}"><button>Log out</button></form>
    </body></html>'''
    return web.Response(text=body, content_type="text/html")


def _rule_form(rule, targets, csrf):
    if not targets:
        return "<p>Add a destination first.</p>"
    rule = rule or {}
    options = "".join(f'<option value="{t["id"]}" {"selected" if t["id"]==rule.get("target_id") else ""}>{_escape(t["label"])}</option>' for t in targets)
    return f'''<form method="post" action="/rule"><input type="hidden" name="csrf" value="{csrf}">
        <input type="hidden" name="id" value="{rule.get('id','')}">
        <input name="name" placeholder="Rule name" value="{_escape(rule.get('name'))}" required>
        {_select('category', routing_rules.CATEGORIES, rule.get('category','transferlist'))}
        <input name="country" maxlength="2" placeholder="TR" value="{_escape(rule.get('country'))}">
        {_select('scope', routing_rules.SCOPES, rule.get('country_scope','either'))}
        <select name="target_id">{options}</select>
        <input name="priority" type="number" value="{rule.get('priority',100)}" required>
        <label><input type="checkbox" name="enabled" {"checked" if rule.get('enabled',1) else ""}> Enabled</label>
        <label><input type="checkbox" name="continue" {"checked" if not rule.get('stop_on_match',1) else ""}> Continue</label>
        <button>Save rule</button></form>'''


async def save_target(request):
    data = await request.post()
    try:
        routing_rules.save_target(data.get("label", ""), data.get("chat_id", ""),
                                  "enabled" in data, data.get("id") or None)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    raise web.HTTPSeeOther("/")


async def save_rule(request):
    data = await request.post()
    try:
        routing_rules.save_rule(data.get("name", ""), data.get("category", ""),
                                data.get("country", ""), data.get("scope", ""),
                                data.get("target_id"), data.get("priority", 100),
                                "enabled" in data, "continue" not in data, data.get("id") or None)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    raise web.HTTPSeeOther("/")


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


def create_app():
    routing_rules.initialize()
    initialize_auth()
    app = web.Application(middlewares=[_security])
    app.add_routes([web.get("/login", login), web.get("/", index),
                    web.post("/target", save_target), web.post("/rule", save_rule),
                    web.post("/logout", logout)])
    return app
