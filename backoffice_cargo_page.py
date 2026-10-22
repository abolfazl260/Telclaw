"""Private Back Office page for date-active cargo and selected Advertio sending."""
import html
from urllib.parse import urlencode, urlparse
from aiohttp import web

import config
from backoffice_active_cargo import active_cargo_report


def _esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _telegram_link(value, label):
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname in {"t.me", "telegram.me"} and not parsed.username:
        return '<a href="' + _esc(url) + '" target="_blank" rel="noopener noreferrer">' + _esc(label) + '</a>'
    return _esc(label)


def _redirect(*, notice=None, error=None):
    params = {}
    if notice:
        params["notice"] = str(notice)[:400]
    if error:
        params["error"] = str(error)[:400]
    raise web.HTTPSeeOther("/cargo" + ("?" + urlencode(params) if params else ""))


async def cargo_page(request):
    q = request.query
    try:
        data = active_cargo_report(
            page=q.get("page", "1"),
            status=q.get("status", "all"),
            trip=q.get("trip", "all"),
            search=q.get("search", ""),
            view=q.get("view", "advertio"),
        )
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    filters = data["filters"]
    report = data["summary"]
    state = request.app["operations"].state("advertio_cargo")
    busy = state["status"] in {"queued", "running", "stopping"}
    enabled = bool(config.ADVERTIO_INGEST_ENABLED and config.ADVERTIO_CARGO_ENABLED
                   and config.ADVERTIO_INGEST_KEY)
    csrf = _esc(request["session"]["csrf"])
    notices = []
    for key, css in (("notice", "notice"), ("error", "error")):
        if q.get(key):
            notices.append('<p class="' + css + '">' + _esc(q[key]) + '</p>')
    if not enabled:
        notices.append('<p class="error">Website ingest is disabled or its API key is missing. Check Settings → Advertio and Advertio Cargo.</p>')

    option_status = [
        ("all", "All statuses"), ("ready", "Ready to send"),
        ("waiting", "Waiting"), ("retry", "Retry"), ("sent", "Sent to API"),
        ("already_existed", "Already existed"), ("rejected", "Rejected"),
        ("sending", "Sending"),
    ]
    options = lambda items, selected: "".join(
        '<option value="' + _esc(value) + '"' + (" selected" if selected == value else "") +
        ">" + _esc(label) + "</option>" for value, label in items
    )
    rows_html = []
    selected_count = 0
    for row in data["rows"]:
        route = (
            (row["origin_city"] or row["origin_country"] or "?") + " → " +
            (row["destination_city"] or row["destination_country"] or "?")
        )
        is_ready = row["sendable"]
        if is_ready:
            selected_count += 1
        checkbox = (
            '<input type="checkbox" name="record_id" value="' + str(int(row["row_id"])) +
            '" aria-label="Select cargo ' + _esc(row["message_id"]) + '">'
            if is_ready and enabled and not busy else '—'
        )
        details = row.get("blocked_reason") or row.get("error") or row.get("next_attempt_at") or "—"
        source = _telegram_link(
            row.get("message_link"),
            "@" + str(row.get("channel_username") or "?") + " #" + str(row["message_id"])
        )
        title = row.get("title") or "Cargo transfer"
        excerpt = str(row.get("description") or "").strip().replace("\\n", " ")
        detail = '<strong>' + _esc(title) + '</strong>'
        if excerpt:
            detail += '<div class="muted">' + _esc(excerpt[:120]) + '</div>'
        rows_html.append(
            '<tr><td>' + checkbox + '</td><td>' + source + '</td>' +
            '<td>' + detail + '</td>' +
            '<td>' + _esc(route) + '</td>' +
            '<td>' + _esc(row.get("departure_date") or "Undated") + '</td>' +
            '<td>' + _esc(row.get("posted_at") or "") + '</td>' +
            '<td><strong>' + _esc(row["status"]) + '</strong></td>' +
            '<td>' + _esc(details) + '</td></tr>'
        )
    tbody = "".join(rows_html) or '<tr><td colspan="8">No cargo ads match the selected filters.</td></tr>'
    params = {key: value for key, value in filters.items() if value not in ("all", "")}
    links = []
    if data["page"] > 1:
        links.append('<a href="/cargo?' + _esc(urlencode({**params, "page": data["page"] - 1})) + '">Previous</a>')
    if data["page"] < data["pages"]:
        links.append('<a href="/cargo?' + _esc(urlencode({**params, "page": data["page"] + 1})) + '">Next</a>')
    links_html = " · ".join(links)
    result = state.get("result")
    result_text = ""
    if result is not None:
        sent = result.get("sent", 0) if isinstance(result, dict) else 0
        rejected = result.get("rejected", 0) if isinstance(result, dict) else 0
        retry = result.get("retry", 0) if isinstance(result, dict) else 0
        found = result.get("found", 0) if isinstance(result, dict) else 0
        result_text = (
            f"<p>Last batch: found <strong>{int(found)}</strong> · sent <strong>{int(sent)}</strong> · "
            f"rejected <strong>{int(rejected)}</strong> · retry <strong>{int(retry)}</strong>.</p>"
        )
    if state.get("error"):
        result_text += '<p class="error">' + _esc(state["error"]) + '</p>'
    submit_disabled = " disabled" if busy or not enabled or selected_count == 0 else ""
    running_script = (
        '<script nonce="' + _esc(request["csp_nonce"]) +
        '">setTimeout(()=>location.reload(),4000)</script>'
        if busy else ""
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Telclaw · Active Cargo</title>
    <style>
      body{{font:15px/1.55 system-ui,sans-serif;color:#192638;background:#f4f7fc;max-width:1460px;margin:0 auto;padding:18px}}
      h1{{margin:.2rem 0}}h2{{margin:.3rem 0 .8rem}}
      .muted{{color:#56667e}}.hint{{background:#eef4ff;padding:12px;border-radius:9px}}
      .notice{{background:#d9f4e5;padding:10px;border-radius:9px}}
      .error{{background:#ffe0da;color:#822c24;padding:10px;border-radius:9px}}
      .tabs,.filters,.stats,.actions{{display:flex;gap:9px;flex-wrap:wrap;align-items:center}}
      .tabs{{margin:14px 0}}.tabs a{{padding:7px 11px;background:#fff;border:1px solid #d6e0ee;border-radius:7px;text-decoration:none}}
      .tabs .active{{font-weight:700;background:#e1ecff}}.card{{border:1px solid #dce4ef;background:#fff;border-radius:12px;padding:18px;margin:15px 0}}
      .stats > div{{background:#f5f8fc;border-radius:9px;padding:12px 18px;min-width:130px}}
      .stats strong{{display:block;font-size:1.5rem}}label{{display:grid;gap:5px}}
      input,select,button{{padding:9px;border:1px solid #bacbe2;border-radius:7px;background:#fff}}
      button{{cursor:pointer;background:#e5efff;font-weight:600}}button:disabled{{opacity:.5;cursor:not-allowed}}
      .table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:930px}}
      th,td{{padding:9px 11px;text-align:left;border-bottom:1px solid #e3e9f2;vertical-align:top}}
      th{{background:#ebf1f9}}td{{max-width:290px;overflow-wrap:anywhere}}.pagination{{padding:14px 0}}
    </style></head><body>
    <h1>Active Cargo · Advertio</h1>
    <nav class="tabs" aria-label="Back Office sections"><a href="/">Publishing</a>
      <a href="/operations">Operations</a><a class="active" href="/cargo">Active Cargo</a>
      <a href="/data">Database</a><a href="/normalization">Normalization</a>
      <a href="/health">System Health</a><a href="/settings">Settings</a></nav>
    <p class="hint"><strong>Two different reports:</strong> the Advertio candidate pool includes future-dated trips and undated posts from the last {int(config.ADVERTIO_CARGO_BACKFILL_DAYS)} days, with no de-duplication.
      Telegram <strong>/transferlive</strong> includes only dated trips departing today or later, requiring both country codes, and removes duplicates by sender, route and date.
      Switch report views below to compare the exact same entries. Neither report verifies Telegram deletion.</p>
    {''.join(notices)}
    <section class="card"><h2>Active cargo overview</h2>
      <div class="stats">
        <div>Rows in selected view<strong>{report.get('total', 0):,}</strong></div>
        <div>Telegram /transferlive (unique)<strong>{report.get('transferlive', 0):,}</strong></div>
        <div>Ready for API<strong>{report.get('ready', 0):,}</strong></div>
        <div>Sent to API<strong>{report.get('sent', 0):,}</strong></div>
        <div>Already existed<strong>{report.get('already_existed', 0):,}</strong></div>
        <div>Rejected<strong>{report.get('rejected', 0):,}</strong></div>
      </div>
      <p class="muted">Sent to API means accepted, not necessarily publicly Active; older PendingReview listings still need separate resolution.</p>
    </section>
    <section class="card"><h2>Search and filter active cargo</h2>
      <form method="get" action="/cargo" class="filters">
        <label>Report definition<select name="view">{options([("advertio","Advertio candidates (dated + recent undated)"),("telegram","Match Telegram /transferlive (deduplicated)")],filters['view'])}</select></label>
        <label>API status<select name="status">{options(option_status,filters['status'])}</select></label>
        <label>Trip<select name="trip">{options([("all","All active"),("upcoming","Dated upcoming"),("undated","Recent undated")],filters['trip'])}</select></label>
        <label>Channel, city, title, Telegram ID<input type="search" name="search" value="{_esc(filters['search'])}" maxlength="100" placeholder="e.g. Tehran"></label>
        <button type="submit">Apply filters</button><a href="/cargo">Clear</a>
      </form>
    </section>
    <section class="card"><h2>Active cargo records ({data['total']:,})</h2>
      <p class="muted">Select individual ready records and send them through the same idempotent cargo queue. Sent, rejected, unsupported and expired records cannot be selected.</p>
      <p>Manual cargo job: <strong>{_esc(state.get('status') or 'idle')}</strong></p>
      {result_text}
      <form method="post" action="/cargo/send">
        <input type="hidden" name="csrf" value="{csrf}">
        <div class="table-wrap"><table><thead><tr>
          <th>Select</th><th>Telegram source</th><th>Ad / description</th><th>Route</th><th>Trip date</th>
          <th>Post date</th><th>Advertio status</th><th>Reason / next retry</th>
        </tr></thead><tbody>{tbody}</tbody></table></div>
        <p class="pagination">Page {data['page']:,} of {data['pages']:,} · {links_html}</p>
        <div class="actions"><button type="submit"{submit_disabled}>Send selected active cargo to Advertio</button>
        <a href="/operations#advertio-delivery">Open full cargo operations</a></div>
      </form>
      <hr>
      <h3>Send next batch automatically</h3>
      <p class="muted">Send up to 25 ready ads in trip-date priority order (no manual selection).
      The cargo worker applies the configured per-batch and daily limits and never resends an already-delivered record.</p>
      <form method="post" action="/cargo/send" class="actions">
        <input type="hidden" name="csrf" value="{csrf}">
        <input type="hidden" name="mode" value="next">
        <button type="submit"{" disabled" if busy or not enabled or report.get('ready',0)==0 else ""}>Send next 25 ready ads</button>
      </form>
    </section>
    {running_script}
    </body></html>"""
    return web.Response(text=page, content_type="text/html")


async def cargo_send(request):
    form = await request.post()
    values = form.getall("record_id", [])
    try:
        if form.get("mode") == "next":
            request.app["operations"].start_advertio_cargo(
                limit=25, requested_by=request["session"]["admin_id"]
            )
            _redirect(notice="Queued up to 25 ready cargo ads. Check results below.")
        request.app["operations"].start_selected_advertio_cargo(
            values, requested_by=request["session"]["admin_id"]
        )
        _redirect(notice=f"Queued {len(values)} selected cargo ad(s). Refresh to see delivery results.")
    except web.HTTPException:
        raise
    except (ValueError, RuntimeError) as exc:
        _redirect(error=str(exc))
