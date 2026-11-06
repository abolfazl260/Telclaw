"""Admin-only back-office page for collecting exactly one Telegram message."""
import asyncio
import html
import logging
import re
from urllib.parse import urlsplit

from aiohttp import web

from backoffice_layout import SHARED_STYLE, page_header
from processing.normalizer import normalize_channel_username

logger = logging.getLogger("telclaw.backoffice.manual_crawl")
MAX_MESSAGE_ID = 2_147_483_647
PUBLIC_USERNAME = re.compile(r"^[A-Za-z0-9_]{4,64}$")


def parse_message_reference(value, channel_username):
    """Accept a message ID or a t.me permalink for the selected source."""
    value = str(value or "").strip()
    if not value or len(value) > 250:
        raise ValueError("Enter a Telegram message ID or public t.me message link.")
    if value.isascii() and value.isdecimal():
        number = int(value)
    else:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in
                {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
                or parsed.username or parsed.password or parsed.port
                or parsed.fragment or parsed.query not in ("", "single")):
            raise ValueError("Use a valid HTTPS Telegram post link or a numeric message ID.")
        segments = parsed.path.strip("/").split("/")
        if (len(segments) != 2
                or normalize_channel_username(segments[0]) != normalize_channel_username(channel_username)
                or not segments[1].isascii() or not segments[1].isdecimal()):
            raise ValueError("The link must point to a post in the selected source channel.")
        number = int(segments[1])
    if not 1 <= number <= MAX_MESSAGE_ID:
        raise ValueError("The Telegram message ID is out of range.")
    return number


def source_channels(channel_data):
    """Build a deduplicated selection list using existing crawler sources."""
    seen = set()
    sources = []
    for category, entries in channel_data.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            username = normalize_channel_username(entry.get("username"))
            if not PUBLIC_USERNAME.fullmatch(username) or username in seen:
                continue
            seen.add(username)
            sources.append((str(category), username, str(entry.get("name") or username)))
    return sources


def _result_details(result):
    if result.get("status") == "not_found":
        return "The post could not be found. Check the ID and Telegram account access."
    if result.get("status") == "failed":
        return "Telegram collection failed. Check the account's channel access and service logs."
    if result.get("saved"):
        return "One message saved. Normal Processing and AI queues handle it next; nothing was published."
    if result.get("duplicates_skipped"):
        return "Skipped: the sender/content was already collected."
    reasons = (
        ("weak_text", "Skipped: message has fewer than 10 words."),
        ("no_username", "Skipped: the sender has no public Telegram username."),
        ("bot_skipped", "Skipped: the sender is a bot."),
        ("forwarded_channel_skipped", "Skipped: the post was forwarded from a channel."),
        ("forwarded_bot_skipped", "Skipped: the post was forwarded from a bot."),
        ("forwarded_unknown_skipped", "Skipped: the forwarded sender could not be verified."),
        ("filtered", "Skipped: the selected crawl mode excludes this message."),
    )
    for key, text in reasons:
        if result.get(key):
            return text
    return "No message saved. Check the crawler's eligibility rules and logs."


async def _render(request, *, selected_channel="", selected_account="", reference="", error="", result=None, status=200):
    operations = request.app["operations"]
    try:
        sources = source_channels(operations.channel_data())
        accounts = await operations.list_accounts()
        load_error = ""
    except Exception:
        logger.exception("Manual crawl options unavailable")
        sources, accounts = [], []
        load_error = "Cannot load configured source channels or Telegram sessions."

    esc = lambda v: html.escape(str(v), quote=True)
    selected_account = selected_account or operations.connected_account() or ""
    groups = {}
    for category, username, name in sources:
        groups.setdefault(category, []).append((username, name))
    source_options = '<option value="">Select source channel</option>'
    for category, items in groups.items():
        source_options += f'<optgroup label="{esc(category)}">'
        for username, name in items:
            selected = " selected" if username == selected_channel else ""
            source_options += (f'<option value="{esc(username)}"{selected}>'
                               f'{esc(name)} (@{esc(username)})</option>')
        source_options += "</optgroup>"

    account_names = [str(item.get("session")) for item in accounts if item.get("session")]
    account_options = '<option value="">Select Telegram session</option>'
    for name in account_names:
        selected = " selected" if name == selected_account else ""
        account_options += f'<option value="{esc(name)}"{selected}>{esc(name)}</option>'
    disabled = " disabled" if not sources or not account_names else ""
    notice = ""
    if load_error or error:
        notice += f'<p class="backoffice-error" role="alert">{esc(error or load_error)}</p>'
    if result is not None:
        details = _result_details(result)
        kind = "backoffice-error" if result.get("status") in ("failed", "not_found") else "backoffice-notice"
        notice += (f'<div class="{kind}" role="status"><strong>{esc(details)}</strong>'
                   f'<p>Stored: {int(result.get("saved") or 0)} · '
                   f'Duplicates: {int(result.get("duplicates_skipped") or 0)} · '
                   f'Status: {esc(result.get("status") or "unknown")}</p></div>')

    content = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telclaw · Manual Crawl</title>
<style>
.manual-form{{display:grid;gap:1rem;max-width:720px}}
.manual-form label{{display:grid;gap:.4rem;font-weight:600}}
.manual-form select,.manual-form input{{width:100%}}
.manual-form .manual-actions{{display:flex;gap:.8rem;align-items:center;flex-wrap:wrap}}
.manual-form .manual-actions button{{width:auto}}
</style>{SHARED_STYLE}</head><body>
{page_header("manual_crawl", "Telclaw · Manual Crawl")}
<main class="backoffice-content">
<section class="backoffice-panel"><h2>Collect one Telegram message</h2>
<p class="backoffice-muted">Choose the source channel and Telegram session, then paste a message ID or its public Telegram link.
Only that post is fetched; the channel history is not crawled.</p>
{notice}
<form method="post" action="/crawl/manual" class="manual-form">
<input type="hidden" name="csrf" value="{esc(request['session']['csrf'])}">
<label for="source-channel">Source channel
<select id="source-channel" name="channel" required>{source_options}</select></label>
<label for="telegram-session">Telegram account
<select id="telegram-session" name="account" required>{account_options}</select></label>
<label for="message-reference">Message ID or link
<input id="message-reference" name="message" type="text" dir="ltr"
placeholder="https://t.me/channel_name/1234" maxlength="250"
value="{esc(reference)}" required></label>
<div class="manual-actions"><button type="submit"{disabled}>Crawl message</button>
<a href="/operations#crawler">Scheduled crawler</a></div>
</form>
<p class="backoffice-muted">Telegram session must have access to the source. Messages are subject to the same
minimum text, sender, forwarding and duplicate filters as the scheduled crawler.
Collection does not start AI extraction or publish any ads. To process immediately, use the Operations queues.</p>
<p class="backoffice-muted">Private channel links (t.me/c/…) are not supported as link input;
use the numeric message ID and the configured source instead.</p>
</section></main></body></html>"""
    return web.Response(text=content, content_type="text/html", status=status)


async def manual_crawl_page(request):
    return await _render(request)


async def manual_crawl_submit(request):
    form = await request.post()
    channel = normalize_channel_username(form.get("channel", ""))
    account = str(form.get("account", "")).strip()
    reference = str(form.get("message", "")).strip()
    try:
        message_id = parse_message_reference(reference, channel)
    except (ValueError, TypeError) as exc:
        return await _render(request, selected_channel=channel, selected_account=account,
                             reference=reference, error=str(exc), status=400)

    lock = request.app["manual_crawl_lock"]
    if lock.locked():
        return await _render(request, selected_channel=channel, selected_account=account,
                             reference=reference, error="A manual crawl is already running.", status=409)

    async with lock:
        try:
            result = await request.app["operations"].crawl_single_message(
                account_name=account,
                channel_username=channel,
                message_id=message_id,
                requested_by=request["session"]["admin_id"],
            )
        except ValueError as exc:
            return await _render(request, selected_channel=channel, selected_account=account,
                                 reference=reference, error=str(exc), status=400)
        except RuntimeError as exc:
            logger.warning("Manual crawl could not start: %s", type(exc).__name__)
            return await _render(request, selected_channel=channel, selected_account=account,
                                 reference=reference, error="Telegram account unavailable or crawler busy.", status=409)
        except Exception:
            logger.exception("Manual crawl failed: channel=%s message_id=%s", channel, message_id)
            return await _render(request, selected_channel=channel, selected_account=account,
                                 reference=reference, error="Manual crawl failed; check server logs.", status=503)
    result_status = 404 if result.get("status") == "not_found" else 502 if result.get("status") == "failed" else 200
    return await _render(request, selected_channel=channel, selected_account=account,
                         reference=reference, result=result, status=result_status)
