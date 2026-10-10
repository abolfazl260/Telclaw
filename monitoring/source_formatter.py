"""Safe, paginated Telegram HTML for the /source monitoring command.

The formatter never edits channels.json or alters crawler selection order.
Every message contains complete HTML entities/tags and stays below Telegram's
4096-character sendMessage limit (including markup).
"""

import html
import re

MAX_SOURCE_MESSAGE_CHARS = 3750
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")

CATEGORY_ICONS = (
    ("job", "💼"),
    ("rent", "🏠"),
    ("home", "🏠"),
    ("flight", "✈️"),
    ("immigrat", "🛂"),
    ("language", "🗣️"),
    ("market", "🛍️"),
    ("shop", "🛍️"),
    ("education", "🎓"),
    ("community", "👥"),
)


def _short(value, limit):
    value = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _category_heading(category, count, continued=False):
    label = _short(category, 110)
    icon = next(
        (emoji for keyword, emoji in CATEGORY_ICONS if keyword in label.casefold()),
        "📂",
    )
    suffix = f" <i>· {count} sources</i>"
    if continued:
        suffix += " <i>· continued</i>"
    return f"{icon} <b>{html.escape(label)}</b>{suffix}"


def _channel_entry(item, number):
    raw = _short(item.get("username"), 80).lstrip("@")
    valid = bool(USERNAME_PATTERN.fullmatch(raw))
    name = html.escape(_short(item.get("name") or raw or "Unnamed source", 140))
    description = _short(item.get("description"), 180)

    if valid:
        label = f'<a href="https://t.me/{raw}">{name}</a>  <code>@{raw}</code>'
    else:
        # A malformed username must not create an unsafe link or HTML injection.
        label = f"<b>{name}</b>"
        if raw:
            label += f"  <code>@{html.escape(raw)}</code>"

    lines = [f"{number}. {label}"]
    if description:
        lines.append(f"    <i>{html.escape(description)}</i>")
    return "\n".join(lines)


def format_source_messages(data):
    """Build ordered, HTML-safe source pages; do not split inside tags or links."""
    if not isinstance(data, dict):
        raise ValueError("Channel configuration must be a category mapping")

    groups = []
    for name, sources in data.items():
        if not isinstance(sources, list):
            raise ValueError(f"Invalid source category: {name!s}")
        entries = [source for source in sources if isinstance(source, dict)]
        groups.append((name, entries))

    total = sum(len(entries) for _, entries in groups)
    header = (
        "📡 <b>TELCLAW · CONFIGURED SOURCES</b>\n"
        f"🗂 <b>Categories:</b> {len(groups)}    "
        f"📢 <b>Sources:</b> {total}"
    )
    continued_header = "📡 <b>TELCLAW · SOURCES (continued)</b>"
    pages = []
    current = header

    for category, entries in groups:
        count = len(entries)
        source_lines = (
            [_channel_entry(item, i) for i, item in enumerate(entries, 1)]
            if entries else ["<i>No channels configured</i>"]
        )
        heading = _category_heading(category, count)
        for index, entry in enumerate(source_lines):
            addition = ("\n\n" + heading + "\n" if index == 0 else "\n") + entry
            if len(current) + len(addition) > MAX_SOURCE_MESSAGE_CHARS:
                pages.append(current)
                current = continued_header
                addition = (
                    "\n\n" + _category_heading(category, count, continued=index > 0)
                    + "\n" + entry
                )
            current += addition

    pages.append(current)
    return [
        f"{page}\n\n<i>Page {index}/{len(pages)}</i>"
        for index, page in enumerate(pages, 1)
    ]
