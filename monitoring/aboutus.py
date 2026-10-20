"""Public Advertio directory for Telegram's /aboutus command.

Source: abolfazl2600/Advertio, 15-communication-assets/registry.md
(snapshot 2026-10-20). These are communication links, not crawler sources.
"""

from __future__ import annotations

import html

# Name, public Telegram username. Maintain from the Advertio asset registry.
CHANNELS = (
    ("خانه و آگهی‌های کانادا", "canada_advertio"),
    ("اجاره خانه در کانادا", "canadahouse24"),
    ("ریچموند هیل", "Richmondhill_advertio"),
    ("مشاغل انتاریو", "Ontario_Job"),
    ("مشاغل تورنتو", "Toronto_Advertio"),
    ("ارسال بار و مدارک", "advertio_cargo"),
    ("فروشگاه ادورتیو", "advertio_shop"),
)

GROUPS = (
    ("خانه و اجاره کانادا", "iranian_homes"),
    ("خانه‌های ونکوور", "Vancouver_house"),
    ("خانه‌های تورنتو", "canada_house_toronto"),
    ("گروه انتاریو", "ontario_advertio"),
    ("ارسال بار آلمان", "German_Advertio_transfer"),
)

BOTS = (
    ("ربات آگهی‌های Advertio", "advertio_bot"),
    ("ربات پرواز و ویزا", "Flightiranbot"),
)

SUPPORT = ("پشتیبانی ادورتیو", "advertio_support")


def _link(name: str, username: str) -> str:
    """Telegram HTML text link; usernames come from our static registry."""
    return f'<a href="https://t.me/{html.escape(username, quote=True)}">{html.escape(name)}</a>'


def build_aboutus_message() -> str:
    """Persian directory with separate links and approximate member counts."""
    lines = [
        "✨ <b>Advertio | ادورتیو</b>",
        "<b>ادورتیو</b> با دو سال فعالیت در حوزه آگهی و خدمات آنلاین، "
        "تلاش می‌کند ارتباط میان افراد، فرصت‌ها و خدمات موردنیازشان را "
        "ساده‌تر و مستقیم‌تر کند.",
        "از <b>پیدا کردن خانه و فرصت‌های شغلی در کانادا</b> تا "
        "<b>ارسال بار و مدارک بین‌المللی، خدمات پرواز و ویزا و ثبت آگهی</b>، "
        "ادورتیو مجموعه‌ای از خدمات و جوامع تخصصی را در کنار هم گرد آورده است.",
        "",
        "🏠 <b>کانال‌های مسکن | 5K عضو</b>",
    ]
    lines.extend("• " + _link(*item) for item in CHANNELS[:3])
    lines.extend(["", "📦✈️ <b>ارسال بار، پرواز و ویزا</b>"])
    lines.append("• " + _link(*CHANNELS[5]) + " | 9K عضو")
    lines.append("• " + _link(*BOTS[1]))
    lines.extend(["", "💼 <b>کانال‌های فرصت شغلی</b>"])
    lines.extend("• " + _link(*item) for item in CHANNELS[3:5])
    lines.extend(["", "🛍 <b>فروشگاه</b>"])
    lines.append("• " + _link(*CHANNELS[6]))
    lines.extend(["", "👥 <b>گروه‌های گفت‌وگو</b>"])
    lines.extend("• " + _link(*item) for item in GROUPS)
    lines.extend(["", "🤖 <b>ربات ثبت آگهی</b>"])
    lines.append("• " + _link(*BOTS[0]))
    lines.extend([
        "",
        "💬 " + _link(*SUPPORT),
        "",
        "<i>👇 برای شروع سریع، یکی از دکمه‌های زیر را انتخاب کن.</i>",
    ])
    return "\n".join(lines)


def aboutus_keyboard() -> dict:
    """Direct URL buttons, with no callback permissions or extra state."""
    return {
        "inline_keyboard": [
            [
                {"text": "🏠 خانه و اجاره", "url": "https://t.me/canadahouse24"},
                {"text": "📦 ارسال بار", "url": "https://t.me/advertio_cargo"},
            ],
            [
                {"text": "✈️ پرواز و ویزا", "url": "https://t.me/Flightiranbot"},
                {"text": "🤖 ثبت آگهی", "url": "https://t.me/advertio_bot"},
            ],
            [
                {"text": "💼 فرصت‌های شغلی", "url": "https://t.me/Ontario_Job"},
                {"text": "🛍 فروشگاه", "url": "https://t.me/advertio_shop"},
            ],
            [
                {"text": "👥 گروه مسکن کانادا", "url": "https://t.me/iranian_homes"},
                {"text": "💬 پشتیبانی", "url": "https://t.me/advertio_support"},
            ],
        ]
    }
