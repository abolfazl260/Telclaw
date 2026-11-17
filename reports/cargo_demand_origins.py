"""Top seven origin cities by actual shipper demand in Advertio Cargo.

Read-only Telegram HTML report, without sendWidget or inline keyboards.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import html
import math

from reports.cargo_weekly import TEHRAN, _clean_city, _country
from reports.cargo_countries import _posted_utc
from storage import database

ALLOWED_PERIODS = (7, 15, 30)
DEFAULT_PERIOD = 30
TOP_ORIGINS = 7
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _fa(value):
    return str(value).translate(_PERSIAN_DIGITS)


def _flag(code):
    return "".join(chr(127397 + ord(char)) for char in code)


def build_demand_report(now: datetime | None = None, *, days: int = DEFAULT_PERIOD):
    """Rank distinct shipper requests by (canonical origin city, ISO country).

    The date window covers elapsed 24-hour periods. Known reposts of one
    sender's same route and departure date count once. If a route is missing,
    retain source messages independently rather than over-deduplicating.
    """
    if days not in ALLOWED_PERIODS:
        raise ValueError("Unsupported cargo demand report period")
    current = now or datetime.now(TEHRAN)
    if current.tzinfo is None:
        raise ValueError("Report time must include a timezone")
    utc_now = current.astimezone(timezone.utc)
    cutoff = utc_now - timedelta(days=days)

    with closing(database.get_connection()) as conn:
        has_location_cache = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_locations'"
        ).fetchone() is not None
        cache_columns = (
            """l.origin_city_canonical AS canonical_origin,
               l.destination_city_canonical AS canonical_destination,
               l.origin_country_iso2 AS canonical_origin_country,
               l.destination_country_iso2 AS canonical_destination_country"""
            if has_location_cache else
            """NULL AS canonical_origin, NULL AS canonical_destination,
               NULL AS canonical_origin_country, NULL AS canonical_destination_country"""
        )
        join_cache = (
            "LEFT JOIN transfer_locations AS l ON l.processed_message_id=m.id"
            if has_location_cache else ""
        )
        rows = conn.execute(
            f"""SELECT m.id, m.channel_username, m.message_id, m.sender_id,
                       m.sender_username, m.date,
                       t.origin_city, t.origin_country,
                       t.destination_city, t.destination_country,
                       t.departure_date, {cache_columns}
                FROM messages AS m
                JOIN transferlist AS t ON t.processed_message_id=m.id
                {join_cache}
                WHERE m.processing_status='processed'
                  AND m.ai_status='processed' AND m.ai_category='transferlist'
                  AND LOWER(TRIM(COALESCE(t.transfer_role,'')))='shipper'
                  AND julianday(m.date) >= julianday(?)
                  AND julianday(m.date) <= julianday(?)
                ORDER BY julianday(m.date) DESC, m.id DESC""",
            (cutoff.isoformat(), utc_now.isoformat()),
        ).fetchall()

    counts = Counter()
    display = {}
    seen = set()
    for row in rows:
        posted = _posted_utc(row["date"])
        if posted is None or not cutoff <= posted <= utc_now:
            continue
        raw_country = _country(row["origin_country"])
        cache_country = _country(row["canonical_origin_country"])
        country = cache_country or raw_country
        if not country:
            continue
        origin = _clean_city(
            row["canonical_origin"] if cache_country == country and row["canonical_origin"] else row["origin_city"]
        )
        if not origin:
            continue

        dest_country = _country(row["canonical_destination_country"]) or _country(row["destination_country"])
        destination = _clean_city(row["canonical_destination"] or row["destination_city"])
        if destination and dest_country:
            if row["sender_id"] is not None:
                sender = ("id", row["sender_id"])
            elif str(row["sender_username"] or "").strip():
                sender = ("username", str(row["sender_username"]).strip().casefold())
            else:
                sender = ("message", row["channel_username"], row["message_id"])
            dedup_key = (
                sender, country, origin.casefold(), dest_country,
                destination.casefold(), "shipper",
                str(row["departure_date"] or "")[:10],
            )
        else:
            dedup_key = ("message", row["id"])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        key = (country, origin.casefold())
        display.setdefault(key, origin)
        counts[key] += 1

    ranked = sorted(counts, key=lambda key: (-counts[key], key[0], key[1]))
    return {
        "as_of": current.astimezone(TEHRAN).strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "total": sum(counts.values()),
        "origin_count": len(counts),
        "top": [
            {"city": display[key], "country": key[0], "count": counts[key]}
            for key in ranked[:TOP_ORIGINS]
        ],
    }


def format_demand_report(report):
    """Telegram HTML markup: exactly seven ranked cities at most, no widgets."""
    days = _fa(report["days"])
    total = report["total"]
    rows = report["top"]
    lines = [
        "<b>📦 ۷ مبدأ پرتقاضای ارسال بار | Advertio</b>",
        f"📅 {days} روز اخیر · تا {_fa(report['as_of'])} تهران",
        f"📊 <b>{_fa(total)}</b> درخواست یکتا در <b>{_fa(report['origin_count'])}</b> مبدأ",
        "",
    ]
    if not rows:
        lines.append("در این بازه درخواست ارسال بار با مبدأ معتبر ثبت نشده است.")
    else:
        maximum = rows[0]["count"]
        for index, row in enumerate(rows, 1):
            country = row["country"]
            safe_city = html.escape(row["city"])
            percent = round(row["count"] * 100 / total)
            filled = max(1, math.ceil(row["count"] * 10 / maximum))
            bar = "▰" * filled + "▱" * (10 - filled)
            lines.append(
                f"{_fa(index)}. {_flag(country)} <b>{safe_city} ({country})</b>"
                f" — <b>{_fa(row['count'])}</b> درخواست"
            )
            lines.append(f"{bar}  {_fa(percent)}٪ از کل")
            if index != len(rows):
                lines.append("")
    lines.extend([
        "",
        "<i>مبنای رتبه‌بندی: درخواست‌های ارسال بار (shipper)؛"
        " نه آگهی مسافر. تکرارهای قابل‌شناسایی حذف شده‌اند.</i>",
        "🔎 بازه‌های دیگر: <code>/cargoorigins 7</code> | "
        "<code>/cargoorigins 15</code> | <code>/cargoorigins 30</code>",
    ])
    return "\n".join(lines)


async def send_demand_report(monitor, chat_id, *, now=None, days=DEFAULT_PERIOD):
    report = await asyncio.to_thread(build_demand_report, now, days=days)
    await monitor._api("sendMessage", {
        "chat_id": chat_id,
        "text": format_demand_report(report),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
