"""A deliberately small Telegram HTML subset; the same parser builds the preview."""
import json
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs


class ValidationError(ValueError):
    pass


def link_url(value):
    value = str(value or "").strip()
    if len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValidationError("Invalid link URL")
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
        return value
    # Telegram supports mention-by-ID and deep links. No javascript/data/file URLs.
    if parsed.scheme == "tg" and parsed.netloc in {"user", "resolve", "join", "privatepost"}:
        params = parse_qs(parsed.query)
        if parsed.netloc == "user" and re.fullmatch(r"[1-9][0-9]*", params.get("id", [""])[0]):
            return value
        if parsed.netloc != "user" and params:
            return value
    raise ValidationError("Use an HTTP(S) or supported tg:// link")


class TelegramHTML(HTMLParser):
    TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.output, self.plain = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS:
            raise ValidationError(f"Unsupported Telegram HTML tag: {tag}")
        if tag in {"code", "pre"} and any(t not in {"pre"} for t in self.stack):
            raise ValidationError("Code/pre cannot be nested inside formatting")
        if any(t in {"code", "pre"} for t in self.stack) and not (tag == "code" and self.stack == ["pre"]):
            raise ValidationError("Formatting cannot be nested in code/pre")
        if tag == "a" and "a" in self.stack or tag == "blockquote" and "blockquote" in self.stack:
            raise ValidationError("Nested links/quotes are not supported")
        allowed = {"a": {"href"}, "code": {"class"}, "blockquote": {"expandable"}}.get(tag, set())
        if any(k not in allowed for k, _ in attrs) or len(dict(attrs)) != len(attrs):
            raise ValidationError("Unsupported HTML attributes")
        attr = dict(attrs)
        suffix = ""
        if tag == "a":
            suffix = ' href="' + escape(link_url(attr.get("href")), quote=True) + '"'
        elif tag == "code" and attr:
            language = attr.get("class", "")
            if self.stack != ["pre"] or not re.fullmatch(r"language-[a-zA-Z0-9_+-]{1,40}", language):
                raise ValidationError("Code language is only allowed inside pre")
            suffix = f' class="{language}"'
        elif tag == "blockquote" and attr:
            suffix = " expandable"
        self.stack.append(tag)
        self.output.append(f"<{tag}{suffix}>")

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValidationError("Unbalanced Telegram HTML tags")
        self.output.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        raise ValidationError("Self-closing tags are not supported")

    def handle_data(self, data):
        if any(ord(c) < 32 and c not in "\n\r\t" for c in data):
            raise ValidationError("Invalid control character")
        self.plain.append(data)
        self.output.append(escape(data, quote=False))

    def handle_comment(self, data):
        raise ValidationError("HTML comments are not supported")

    def handle_decl(self, decl):
        raise ValidationError("HTML declarations are not supported")

    def unknown_decl(self, data):
        raise ValidationError("HTML declarations are not supported")

    def handle_pi(self, data):
        raise ValidationError("HTML processing instructions are not supported")


def content(text, parse_mode="HTML", has_media=False):
    text = str(text or "")
    if len(text) > 32768:
        raise ValidationError("Message markup is too large")
    if parse_mode not in {"HTML", "plain"}:
        raise ValidationError("Formatting must be HTML or plain")
    if parse_mode == "HTML":
        parser = TelegramHTML()
        parser.feed(text)
        parser.close()
        if parser.stack:
            raise ValidationError("Unclosed Telegram HTML tag")
        plain, safe = "".join(parser.plain), "".join(parser.output)
    else:
        plain, safe = text, escape(text)
    limit = 1024 if has_media else 4096
    if not plain.strip() or len(plain.encode("utf-16-le")) // 2 > limit:
        raise ValidationError(f"Message must contain 1–{limit} characters (UTF-16 units)")
    return (safe if parse_mode == "HTML" else text), safe


def buttons(value):
    try:
        rows = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError) as exc:
        raise ValidationError("Buttons must be a JSON array of rows") from exc
    if rows is None:
        rows = []
    if not isinstance(rows, list) or len(rows) > 20:
        raise ValidationError("At most 20 button rows are allowed")
    result = []
    total = 0
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 8:
            raise ValidationError("Each button row needs 1–8 buttons")
        target = []
        for item in row:
            if not isinstance(item, dict) or set(item) != {"text", "url"}:
                raise ValidationError("Each button needs only text and url")
            text = str(item["text"]).strip()
            if not 1 <= len(text) <= 64:
                raise ValidationError("Button text must contain 1–64 characters")
            target.append({"text": text, "url": link_url(item["url"])})
        total += len(target)
        result.append(target)
    if total > 100:
        raise ValidationError("At most 100 buttons are allowed")
    return result
