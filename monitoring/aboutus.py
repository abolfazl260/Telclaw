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
    """Readable, clickable Persian directory within Telegram's message limit."""
    lines = [
        "✨ <b>Advertio | ادورتیو</b>",
        "<i>از پیدا کردن فرصت تا ارتباط با آدم‌های مناسب.</i>",
        "",
        "در شبکه ادورتیو می‌توانی آگهی‌های مسکن و شغل در کانادا، "
        "ارسال بار مسافری، خدمات سفر و فروشگاه را دنبال کنی.",
        "",
        "🏠 <b>کانال‌های مسکن</b>",
        " · ".join(_link(*item) for item in CHANNELS[:3]),
        "",
        "💼 <b>کانال‌های فرصت شغلی</b>",
        " · ".join(_link(*item) for item in CHANNELS[3:5]),
        "",
        "📦 <b>بار و خرید</b>",
        " · ".join(_link(*item) for item in CHANNELS[5:]),
        "",
        "👥 <b>گروه‌های گفت‌وگو</b>",
        " · ".join(_link(*item) for item in GROUPS[:3]),
        " · ".join(_link(*item) for item in GROUPS[3:]),
        "",
        "🤖 <b>ربات‌های ادورتیو</b>",
        " · ".join(_link(*item) for item in BOTS),
        "",
        "💬 " + _link(*SUPPORT),
        "",
        "<i>👇 برای شروع سریع، یکی از دکمه‌های زیر را انتخاب کن.</i>",
    ]
    return "\n".join(lines)


def aboutus_keyboard() -> dict:
    """Direct URL buttons, with no callback permissions or extra state."""
    return {
        "inline_keyboard": [
            [
                {"text": "🏠 خانه و اجاره", "url": "https://t.me/canadahouse24"},
                {"text": "💼 فرصت‌های شغلی", "url": "https://t.me/Ontario_Job"},
            ],
            [
                {"text": "📦 ارسال بار", "url": "https://t.me/advertio_cargo"},
                {"text": "✈️ پرواز و ویزا", "url": "https://t.me/Flightiranbot"},
            ],
            [
                {"text": "🤖 ثبت و مشاهده آگهی", "url": "https://t.me/advertio_bot"},
                {"text": "🛍 فروشگاه", "url": "https://t.me/advertio_shop"},
            ],
            [
                {"text": "👥 گروه مسکن کانادا", "url": "https://t.me/iranian_homes"},
                {"text": "💬 پشتیبانی", "url": "https://t.me/advertio_support"},
            ],
        ]
    }
