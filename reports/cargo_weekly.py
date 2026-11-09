"""Weekly Advertio Cargo route intelligence and Telegram Bot API 10.4 widget.

Production widgets are private-chat-only. A normal channel post links to the
bot's /start cargo_weekly deep link; the actual HTML widget is sent in private.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import html
import json
import logging
import re
import zlib
from zoneinfo import ZoneInfo

from storage import database

log = logging.getLogger(__name__)
TEHRAN = ZoneInfo("Asia/Tehran")
# Advertio Cargo is a product destination, not a per-installation setting.
REPORT_CHANNEL = "@advertio_cargo"
UNSPECIFIED = {"", "-", "—", "unknown", "unspecified", "none", "null", "n/a", "na", "نامشخص"}

# Telegram widget profile 1: inline CSS/JS, no network, no external assets.
# The HTML remains immutable; the compact snapshot is carried in public_data.
WIDGET_HTML = """<html lang="fa" dir="rtl"><head><style>
body{font-family:system-ui,sans-serif;margin:0;padding:14px;color:var(--tg-theme-text-color)}
h3{margin:0 0 5px;font-size:1.1em}.meta,.note{color:var(--tg-theme-hint-color);font-size:.77em}
.meta{margin-bottom:10px}.summary{display:flex;gap:7px;margin:10px 0}
.stat{flex:1;border-radius:10px;background:var(--tg-theme-secondary-bg-color);padding:9px 5px;text-align:center}
.stat strong{display:block;font-size:1.25em}.stat small{display:block;font-size:.68em;color:var(--tg-theme-hint-color)}
.tabs{display:flex;gap:5px;margin:12px 0}
.tabs button{flex:1;min-width:0;font-size:.78em;padding:7px 2px;border-radius:8px;
background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);border:0}
.tabs button.active{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
.route{padding:8px 0;border-bottom:1px solid var(--tg-theme-section-separator-color)}
.row{display:flex;justify-content:space-between;gap:6px;font-size:.82em}
.name{font-weight:600;overflow-wrap:anywhere}.count{white-space:nowrap;font-weight:700}
.track{height:7px;border-radius:8px;background:var(--tg-theme-secondary-bg-color);overflow:hidden;margin:7px 0 4px}
.fill{height:100%;border-radius:8px;background:var(--tg-theme-link-color)}
.sub{font-size:.73em;color:var(--tg-theme-hint-color)}
.note{margin-top:11px;line-height:1.6}
</style></head><body><h3>📦 گزارش هفتگی مسیرهای کارگو</h3>
<div id="period" class="meta">Advertio Cargo</div>
<div class="summary"><div class="stat"><strong id="total">—</strong><small>آگهی یکتا</small></div>
<div class="stat"><strong id="routes">—</strong><small>مسیر</small></div>
<div class="stat"><strong id="unknown">—</strong><small>نقش نامشخص</small></div></div>
<div class="tabs"><button id="all" class="active">همه</button>
<button id="shipper">درخواست حمل</button><button id="passenger">پیشنهاد مسافر</button></div>
<div id="list">در حال نمایش گزارش…</div>
<div class="note">آمار بر اساس آگهی‌های پردازش‌شده و حذف تکرارهای شناخته‌شده است؛ نه تعداد حمل‌های انجام‌شده.</div>
<script>
(function(){
 var W=window.TelegramWidget, mode='all', data={}, ids=['all','shipper','passenger'];
 function num(x){return Number(x||0).toLocaleString('fa-IR');}
 function el(tag,cls,txt){var x=document.createElement(tag);if(cls)x.className=cls;if(txt!==undefined)x.textContent=txt;return x;}
 function render(){
  try{data=JSON.parse(W&&W.publicData||'{}');}catch(e){data={};}
  document.getElementById('period').textContent=(data.s||'')+' تا '+(data.e||'')+' · Advertio Cargo';
  document.getElementById('total').textContent=num(data.t);
  document.getElementById('routes').textContent=num(data.k);
  document.getElementById('unknown').textContent=num(data.u);
  var root=document.getElementById('list');while(root.firstChild)root.removeChild(root.firstChild);
  var rows=(data.r||[]), idx=mode==='all'?1:(mode==='shipper'?2:3), max=1;
  rows.forEach(function(r){max=Math.max(max,r[idx]||0);});
  var visible=0;
  rows.forEach(function(r){
   var n=r[idx]||0;if(mode!=='all'&&!n)return;visible++;
   var item=el('div','route'),head=el('div','row');
   head.appendChild(el('span','name',r[0]));head.appendChild(el('span','count',num(n)));
   item.appendChild(head);
   var track=el('div','track'),fill=el('div','fill');fill.style.width=(n/max*100)+'%';
   track.appendChild(fill);item.appendChild(track);
   item.appendChild(el('div','sub','درخواست: '+num(r[2])+' · مسافر: '+num(r[3])+' · نامشخص: '+num(r[4])));
   root.appendChild(item);
  });
  if(!visible)root.appendChild(el('p','meta','برای این فیلتر آگهی ثبت نشده است.'));
  ids.forEach(function(id){document.getElementById(id).className=(id===mode?'active':'');});
 }
 ids.forEach(function(id){document.getElementById(id).addEventListener('click',function(){mode=id;render();});});
 if(W&&W.onEvent)W.onEvent('publicDataChanged',render);
 render();if(W&&W.ready)W.ready();
})();
</script></body></html>"""


def check_widget_limits() -> None:
    raw = WIDGET_HTML.encode("utf-8")
    if b"\x00" in raw or len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise ValueError("Telegram widget HTML exceeds the Bot API 10.4 limits")


def previous_complete_week(now: datetime | None = None):
    """Inclusive local Monday / exclusive next Monday for the last finished week."""
    current = (now or datetime.now(TEHRAN)).astimezone(TEHRAN)
    this_monday = (current - timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return this_monday - timedelta(days=7), this_monday


def _clean_city(value):
    result = re.sub(r"\s+", " ", str(value or "").strip())
    return result if result.casefold() not in UNSPECIFIED else ""


def _country(value):
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z]{2}", text) and text not in {"XX", "ZZ"} else ""


def build_report(now: datetime | None = None, *, max_routes: int = 5):
    """Only processed transferlist records; no AI, HTTP or live Telegram calls.

    Deduplicate one sender's repeated listing for the same role, route and
    departure date. Unknown senders use the source channel and message ID,
    never the Telegram message ID on its own.
    """
    start, end = previous_complete_week(now)
    conn = database.get_connection()
    try:
        rows = conn.execute(
            """SELECT m.id, m.channel_username, m.message_id, m.sender_id,
                      m.sender_username, t.origin_city, t.origin_country,
                      t.destination_city, t.destination_country,
                      t.departure_date, t.transfer_role
               FROM messages m
               JOIN transferlist t ON t.processed_message_id=m.id
               WHERE m.processing_status='processed'
                 AND m.ai_status='processed' AND m.ai_category='transferlist'
                 AND datetime(m.date) >= datetime(?)
                 AND datetime(m.date) < datetime(?)
               ORDER BY m.id DESC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    grouped = defaultdict(lambda: {"total": 0, "shipper": 0, "passenger": 0, "unknown": 0})
    display = {}
    unique = set()
    total = unknown = 0
    for row in rows:
        oc, dc = _country(row["origin_country"]), _country(row["destination_country"])
        origin, destination = _clean_city(row["origin_city"]), _clean_city(row["destination_city"])
        if not (oc and dc and origin and destination):
            continue
        role = str(row["transfer_role"] or "").strip().lower()
        if role not in {"passenger", "shipper"}:
            role = "unknown"
        if row["sender_id"] is not None:
            sender = ("id", row["sender_id"])
        elif str(row["sender_username"] or "").strip():
            sender = ("username", str(row["sender_username"]).strip().casefold())
        else:
            sender = ("source", row["channel_username"], row["message_id"])
        route = (oc, origin.casefold(), dc, destination.casefold())
        dedup = (sender, route, role, str(row["departure_date"] or "")[:10])
        if dedup in unique:
            continue
        unique.add(dedup)
        display.setdefault(route, f"\u2066{origin} ({oc}) → {destination} ({dc})\u2069")
        grouped[route]["total"] += 1
        grouped[route][role] += 1
        total += 1
        unknown += role == "unknown"

    ranked = sorted(grouped, key=lambda key: (-grouped[key]["total"], display[key].casefold()))
    return {
        "start": start.date().isoformat(),
        "end": (end - timedelta(days=1)).date().isoformat(),
        "period_key": start.date().isoformat(),
        "total": total,
        "route_count": len(grouped),
        "unknown": unknown,
        "routes": [
            {"name": display[key], **grouped[key]}
            for key in ranked[:max(1, min(int(max_routes), 10))]
        ],
    }


def widget_payload(chat_id, report):
    """Create a static Telegram widget with a compact, non-secret snapshot."""
    check_widget_limits()
    routes = list(report["routes"])
    while True:
        state = {
            "s": report["start"], "e": report["end"],
            "t": report["total"], "k": report["route_count"],
            "u": report["unknown"],
            "r": [
                [r["name"], r["total"], r["shipper"], r["passenger"], r["unknown"]]
                for r in routes
            ],
        }
        packed = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        if len(packed.encode("utf-8")) <= 1024:
            break
        if not routes:
            raise ValueError("Widget data exceeds the Telegram state limit")
        routes.pop()
    return {
        "chat_id": chat_id,
        "html": WIDGET_HTML,
        "public_data": packed,
        "caption": (
            f"گزارش هفتگی مسیرهای کارگو | {report['start']} تا {report['end']} "
            f"| {report['total']} آگهی یکتا"
        ),
        "height": 0,
        "allow_queries": False,
    }


def channel_post(report, bot_username: str):
    """Channel-compatible message: Telegram production rejects channel widgets."""
    lines = [
        "<b>📦 گزارش هفتگی مسیرهای کارگو | Advertio</b>",
        f"📅 {report['start']} تا {report['end']}",
        f"📊 {report['total']} آگهی یکتا در {report['route_count']} مسیر",
        "",
        "<b>پرتکرارترین مسیرهای آگهی‌شده:</b>",
    ]
    for i, route in enumerate(report["routes"], 1):
        lines.append(
            f"{i}. {html.escape(route['name'])} — {route['total']} آگهی "
            f"(درخواست: {route['shipper']} | مسافر: {route['passenger']})"
        )
    lines += [
        "",
        f"نقش نامشخص: {report['unknown']} آگهی",
        "این اعداد تعداد آگهی‌های پردازش‌شده و یکتا هستند، نه تعداد حمل‌های قطعی.",
    ]
    return {
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [[{
            "text": "📊 مشاهده ویجت گزارش",
            "url": f"https://t.me/{bot_username}?start=cargo_weekly",
        }]]},
    }


def _claim(period_key, channel):
    """Claim before network I/O. Uncertain sends must never be auto-repeated."""
    with closing(database.get_connection()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cargo_weekly_report_publications (
                 period_key TEXT PRIMARY KEY, destination TEXT NOT NULL,
                 status TEXT NOT NULL, telegram_message_id INTEGER,
                 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 completed_at TEXT, error TEXT
               )"""
        )
        result = conn.execute(
            """INSERT OR IGNORE INTO cargo_weekly_report_publications
               (period_key, destination, status) VALUES (?, ?, 'sending')""",
            (period_key, channel),
        )
        return result.rowcount == 1


def _finish(period_key, status, message_id=None, error=None):
    with closing(database.get_connection()) as conn, conn:
        conn.execute(
            """UPDATE cargo_weekly_report_publications
               SET status=?, telegram_message_id=?, error=?, completed_at=CURRENT_TIMESTAMP
               WHERE period_key=?""",
            (status, message_id, str(error or "")[:1000], period_key),
        )


async def send_private_report(monitor, chat_id, *, now=None):
    report = await asyncio.to_thread(build_report, now)
    if not report["total"]:
        await monitor._api("sendMessage", {
            "chat_id": chat_id,
            "text": "در هفته کامل گذشته آگهی کارگوی قابل گزارش با مسیر معتبر ثبت نشده است.",
        })
        return
    await monitor._api("sendWidget", widget_payload(chat_id, report))


async def publish_weekly_once(monitor, *, now=None):
    """Called by the independent worker; safe across restarts/processes."""
    if not monitor.enabled:
        return False
    current = (now or datetime.now(TEHRAN)).astimezone(TEHRAN)
    monday = current - timedelta(days=current.weekday())
    if current < monday.replace(hour=10, minute=0, second=0, microsecond=0):
        return False
    report = await asyncio.to_thread(build_report, current)
    if not report["total"]:
        return False
    me = await monitor._api("getMe")
    username = str((me.get("result") or {}).get("username") or "")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("Cannot publish cargo widget deep link without bot username")
    target = REPORT_CHANNEL
    if not await asyncio.to_thread(_claim, report["period_key"], target):
        return False
    try:
        result = await monitor._api("sendMessage", {
            "chat_id": target, **channel_post(report, username)
        })
        message_id = (result.get("result") or {}).get("message_id")
    except Exception as exc:
        # A timeout/disconnect after Telegram accepted the post is ambiguous.
        # Do not auto-retry and risk duplicating the report in the channel.
        await asyncio.to_thread(_finish, report["period_key"], "uncertain", None, str(exc))
        log.exception("[CARGO WEEKLY] send outcome uncertain for %s", report["period_key"])
        return False
    await asyncio.to_thread(_finish, report["period_key"], "sent", message_id)
    log.info("[CARGO WEEKLY] published %s to %s (message %s)", report["period_key"], target, message_id)
    return True


async def run_weekly_publisher(monitor):
    while True:
        try:
            await publish_weekly_once(monitor)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[CARGO WEEKLY] report cycle failed")
        await asyncio.sleep(30 * 60)
