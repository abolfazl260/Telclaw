"""Interactive origin-country distribution for processed Advertio Cargo listings.

One database snapshot covers the rolling 7, 15 and 30-day windows. The private
Telegram widget switches periods locally, without network requests or .env flags.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
import zlib

from reports.cargo_weekly import TEHRAN, _clean_city, _country
from storage import database

log = logging.getLogger(__name__)

PERIODS = (7, 15, 30)
TOP_COUNTRIES = 8

COUNTRY_WIDGET_HTML = """<html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;padding:14px 13px;
color:var(--tg-theme-text-color);background:var(--tg-theme-bg-color)}
h3{margin:0 0 4px;font-size:1.03em}
.hint{font-size:.74em;color:var(--tg-theme-hint-color);line-height:1.45}
.tabs{display:flex;gap:6px;margin:14px 0 11px}
.tabs button{flex:1;min-width:0;background:var(--tg-theme-secondary-bg-color);
color:var(--tg-theme-text-color);border:0;border-radius:10px;
padding:9px 0;font-weight:650;font-size:.88em}
.tabs button.active{background:var(--tg-theme-button-color);
color:var(--tg-theme-button-text-color)}
.metrics{display:flex;gap:8px;margin:8px 0 10px}
.metric{background:var(--tg-theme-secondary-bg-color);border-radius:10px;
padding:9px 12px;flex:1;min-width:0}
.metric strong{display:block;font-size:1.19em}
.metric small{display:block;font-size:.72em;color:var(--tg-theme-hint-color);margin-top:2px}
.item{margin:0 0 10px}
.line{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:.82em}
.country{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.count{font-weight:700;white-space:nowrap;direction:ltr;unicode-bidi:isolate}
.track{height:9px;border-radius:99px;margin-top:5px;background:var(--tg-theme-secondary-bg-color);overflow:hidden}
.fill{height:100%;width:0;border-radius:99px;background:var(--tg-theme-button-color)}
.note{margin-top:10px;line-height:1.5;font-size:.69em;color:var(--tg-theme-hint-color)}
</style></head><body>
<h3>📦 آگهی‌های کارگو به تفکیک کشور مبدأ</h3>
<div class="hint" id="asof">Advertio Cargo</div>
<div class="tabs"><button type="button" id="p7" class="active">۷ روز</button>
<button type="button" id="p15">۱۵ روز</button><button type="button" id="p30">۳۰ روز</button></div>
<div class="metrics"><div class="metric"><strong id="total">—</strong>
<small>آگهی با مبدأ معتبر</small></div>
<div class="metric"><strong id="countries">—</strong><small>کشور مبدأ</small></div></div>
<div id="chart" role="img" aria-label="نمودار تعداد آگهی به تفکیک کشور مبدأ"></div>
<div class="note">مقایسه بر اساس زمان ثبت آگهی‌های پردازش‌شده در بازه‌های متحرک تا لحظه گزارش است؛ تکرارهای قابل‌شناسایی حذف شده‌اند. «سایر» مجموع کشورهای دیگر است.</div>
<script>
(function(){
 var W=window.TelegramWidget, state={}, active=0, ids=['p7','p15','p30'];
 var names={IR:'ایران',RU:'روسیه',AE:'امارات',TR:'ترکیه',CA:'کانادا',
 DE:'آلمان',US:'آمریکا',CN:'چین',GB:'بریتانیا',FR:'فرانسه',
 IN:'هند',IT:'ایتالیا',ES:'اسپانیا',PL:'لهستان',NL:'هلند',
 AT:'اتریش',CH:'سوئیس',SE:'سوئد',NO:'نروژ',FI:'فنلاند',
 AU:'استرالیا',JP:'ژاپن',KR:'کره جنوبی',GE:'گرجستان',
 AM:'ارمنستان',AZ:'آذربایجان',IQ:'عراق',QA:'قطر',
 SA:'عربستان',OM:'عمان',TH:'تایلند',MY:'مالزی',
 BG:'بلغارستان',RS:'صربستان',KZ:'قزاقستان',UZ:'ازبکستان'};
 function num(x){return Number(x||0).toLocaleString('fa-IR');}
 function flag(code){
  if(!/^[A-Z]{2}$/.test(code))return '';
  return String.fromCodePoint(code.charCodeAt(0)+127397,code.charCodeAt(1)+127397)+' ';
 }
 function make(tag,cls,txt){
  var el=document.createElement(tag);if(cls)el.className=cls;
  if(txt!==undefined)el.textContent=txt;return el;
 }
 function render(){
  document.getElementById('asof').textContent='تا '+(state.a||'')+' (تهران)';
  document.getElementById('total').textContent=num((state.t||[])[active]);
  document.getElementById('countries').textContent=num((state.n||[])[active]);
  ids.forEach(function(id,i){
   document.getElementById(id).className=i===active?'active':'';
   document.getElementById(id).setAttribute('aria-pressed',i===active?'true':'false');
  });
  var list=document.getElementById('chart');while(list.firstChild)list.removeChild(list.firstChild);
  var rows=(state.c||[]).filter(function(r){return (r[active+1]||0)>0;});
  rows.sort(function(a,b){return b[active+1]-a[active+1]||(a[0]<b[0]?-1:1);});
  rows=rows.slice(0,8);
  var included=0;rows.forEach(function(r){included+=r[active+1];});
  var other=Math.max(0,((state.t||[])[active]||0)-included);
  var max=rows.length?rows[0][active+1]:other;
  if(other>max)max=other;
  if(!max){list.appendChild(make('p','hint','در این بازه آگهی با کشور مبدأ معتبر ثبت نشده است.'));return;}
  rows.forEach(function(r){add(r[0],r[active+1],max);});
  if(other)add('OTHER',other,max);
  function add(code,count,maxValue){
   var item=make('div','item'),line=make('div','line');
   var label=code==='OTHER'?'سایر کشورها':flag(code)+(names[code]||code)+' ('+code+')';
   line.appendChild(make('span','country',label));
   line.appendChild(make('span','count',num(count)+' آگهی'));
   item.appendChild(line);
   var track=make('div','track'),fill=make('div','fill');
   fill.style.width=Math.max(2,100*count/maxValue)+'%';
   track.appendChild(fill);item.appendChild(track);list.appendChild(item);
  }
 }
 ids.forEach(function(id,index){document.getElementById(id).addEventListener('click',
 function(){active=index;render();});});
 function reload(){try{state=JSON.parse(W&&W.publicData||'{}');}catch(e){state={};}render();}
 if(W&&W.onEvent)W.onEvent('publicDataChanged',reload);
 reload();if(W&&W.ready)W.ready();
})();
</script></body></html>"""


def check_country_widget_limits():
    encoded = COUNTRY_WIDGET_HTML.encode("utf-8")
    if (b"\x00" in encoded or len(encoded) > 32768
            or len(zlib.compress(encoded, 9)) > 4096):
        raise ValueError("Telegram country widget HTML exceeds limits")


def _posted_utc(value):
    try:
        posted = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return posted.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def build_country_snapshot(now: datetime | None = None):
    """Aggregate 30 rolling days in one read; counts are monotonic across ranges.

    Only AI-processed transferlist messages with valid ISO-like origin countries
    are included. Known reposts of the same trip by one sender are deduplicated.
    If a complete route is unavailable, retain the distinct Telegram message.
    """
    current = now or datetime.now(TEHRAN)
    if current.tzinfo is None:
        raise ValueError("The snapshot time must include a timezone")
    utc_now = current.astimezone(timezone.utc)
    cutoffs = [utc_now - timedelta(days=days) for days in PERIODS]
    with closing(database.get_connection()) as conn:
        rows = conn.execute(
            """SELECT m.id, m.channel_username, m.message_id, m.sender_id,
                      m.sender_username, m.date, t.origin_country,
                      t.origin_city, t.destination_country, t.destination_city,
                      t.transfer_role, t.departure_date
               FROM messages AS m
               INNER JOIN transferlist AS t ON t.processed_message_id=m.id
               WHERE m.processing_status='processed'
                 AND m.ai_status='processed' AND m.ai_category='transferlist'
                 AND julianday(m.date) >= julianday(?)
                 AND julianday(m.date) <= julianday(?)
               ORDER BY julianday(m.date) DESC, m.id DESC""",
            (cutoffs[-1].isoformat(), utc_now.isoformat()),
        ).fetchall()

    counts = [Counter() for _ in PERIODS]
    unique = set()
    for row in rows:
        country = _country(row["origin_country"])
        posted = _posted_utc(row["date"])
        if not country or posted is None or posted < cutoffs[-1] or posted > utc_now:
            continue
        origin = _clean_city(row["origin_city"])
        destination = _clean_city(row["destination_city"])
        dest_country = _country(row["destination_country"])
        if origin and destination and dest_country:
            if row["sender_id"] is not None:
                sender = ("id", row["sender_id"])
            elif str(row["sender_username"] or "").strip():
                sender = ("username", str(row["sender_username"]).strip().casefold())
            else:
                sender = ("message", row["channel_username"], row["message_id"])
            role = str(row["transfer_role"] or "").strip().lower()
            if role not in {"passenger", "shipper"}:
                role = "unknown"
            dedup_key = (
                sender, country, origin.casefold(), dest_country,
                destination.casefold(), role, str(row["departure_date"] or "")[:10],
            )
        else:
            dedup_key = ("message", row["id"])
        if dedup_key in unique:
            continue
        unique.add(dedup_key)
        for index, cutoff in enumerate(cutoffs):
            if posted >= cutoff:
                counts[index][country] += 1

    totals = [sum(part.values()) for part in counts]
    country_counts = [len(part) for part in counts]
    selected = set()
    for part in counts:
        selected.update(code for code, _ in part.most_common(TOP_COUNTRIES))
    ranked = sorted(selected, key=lambda code: (-counts[-1][code], code))
    return {
        "as_of": current.astimezone(TEHRAN).strftime("%Y-%m-%d %H:%M"),
        "totals": totals,
        "country_counts": country_counts,
        "countries": [
            [code, *[part[code] for part in counts]]
            for code in ranked
        ],
    }


def country_widget_payload(chat_id, snapshot):
    """The complete 7/15/30-day snapshot fits in a single 1024-byte state."""
    check_country_widget_limits()
    state = {
        "a": snapshot["as_of"],
        "t": snapshot["totals"],
        "n": snapshot["country_counts"],
        "c": snapshot["countries"],
    }
    packed = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(packed.encode("utf-8")) > 1024:
        raise ValueError("Country widget public_data exceeds 1024-byte limit")
    return {
        "chat_id": chat_id,
        "html": COUNTRY_WIDGET_HTML,
        "public_data": packed,
        "caption": "📦 آگهی‌های کارگو بر اساس کشور مبدأ | ۷، ۱۵ و ۳۰ روز اخیر",
        "allow_queries": False,
    }


async def send_country_report(monitor, chat_id, *, now=None):
    snapshot = await asyncio.to_thread(build_country_snapshot, now)
    await monitor._api("sendWidget", country_widget_payload(chat_id, snapshot))
