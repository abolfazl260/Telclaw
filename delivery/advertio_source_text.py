"""Source evidence shared by the Advertio journeys."""

import re

CARGO_CURRENCIES = {
    "CAD": r"\bCAD\b|\bC\$|Canadian dollars?|دلار\s*کانادا",
    "USD": r"\bUSD\b|\bUS\$|US dollars?|دلار\s*(?:آمریکا|امریکا)",
    "EUR": r"\bEUR\b|€|\beuros?\b|یورو",
    "GBP": r"\bGBP\b|£|\bpounds?\s*sterling\b|پوند\s*(?:انگلیس|بریتانیا)",
    "AED": r"\bAED\b|UAE dirhams?|درهم\s*امارات",
    "CNY": r"\b(?:CNY|RMB|yuan)\b|یوان|юан\w*",
    "TRY": r"\bTRY\b|\bTL\b|₺|Turkish lira|لیر\s*ترکیه|турецк\w*\s+лир\w*",
    "RUB": r"\bRUB\b|₽|\brubles?\b|\broubles?\b|روبل|руб(?:\.|л\w*|\b)",
}

NEGOTIABLE = re.compile(
    r"\bnegotiable\b|\bprice\s+on\s+request\b|توافقی|قابل\s*مذاکره", re.I
)
_NEGATION = re.compile(
    r"\b(?:non[- ]|not\s+)negotiable\b|غیر\s*توافقی|قابل\s*مذاکره\s*نیست", re.I
)
_HOUSING_PRICE = re.compile(
    r"\b(?:rent|price)\b.{0,24}\bnegotiable\b|\bnegotiable\s+(?:rent|price)\b|"
    r"\bprice\s+on\s+request\b|(?:اجاره(?:[‌ ]بها)?|قیمت).{0,14}(?:توافقی|قابل\s*مذاکره)|(?m:^\s*توافقی\s*$)",
    re.I,
)
_PER_KG = re.compile(
    r"\bper\s*(?:kg|kilo(?:gram)?)\b|/\s*kg\b|\bkg\s*rate\b|کیلویی|کیلوئی|"
    r"هر\s*کیلو|за\s*кг|/\s*кг\b",
    re.I,
)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬", "01234567890123456789.,")
_AMOUNT = re.compile(r"(?<!\d)\d+(?:,\d{3})*(?:\.\d+)?(?!\d)")


def original_text(record):
    for key in ("raw_text", "text", "cleaned_text"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def description_limit(text):
    # Advertio uses a .NET database field limit, measured in UTF-16 units.
    return text.encode("utf-16-le")[:4000].decode("utf-16-le", errors="ignore")


def explicitly_negotiable(text, *, housing=False):
    return bool(not _NEGATION.search(text) and (_HOUSING_PRICE if housing else NEGOTIABLE).search(text))


def has_cargo_price(text, price, currency):
    if currency not in CARGO_CURRENCIES:
        return False
    for clause in re.split(r"[\n؛;!؟]+", text.translate(_DIGITS).replace("ي", "ی").replace("ك", "ک")):
        if len(clause) > 1000:
            continue
        for amount in _AMOUNT.finditer(clause):
            if float(amount[0].replace(",", "")) != price:
                continue
            # Associate evidence within a short phrase around this amount.
            # A second amount bounds the phrase, preventing a flat fee from
            # borrowing the per-kg unit or currency of a different quote.
            before, after = clause[:amount.start()], clause[amount.end():]
            left = list(_AMOUNT.finditer(before))
            right = _AMOUNT.search(after)
            phrase = (before[left[-1].end():] if left else before)[-26:] + amount[0]
            phrase += (after[:right.start()] if right else after)[:26]
            if (re.search(CARGO_CURRENCIES[currency], phrase, re.I)
                    and _PER_KG.search(phrase)
                    and not re.search(r"\b(?:AUD|AZN|AMD|IRR)\b|تومان|ریال|\btotal\b|"
                                      r"\bflat\s+fee\b|هزینه[‌ ]کل", phrase, re.I)
                    and not any(code != currency and re.search(pattern, phrase, re.I)
                                for code, pattern in CARGO_CURRENCIES.items())):
                return True
    return False


def job_pay_amount(text, extracted, unit_pattern):
    number = r"\d+(?:,\d{3})*(?:\.\d+)?"
    quotes = re.compile(
        rf"(?<!\d)(?P<low>{number})(?:\s*(?:[-–—]|to|تا)\s*(?P<high>{number}))?(?!\d)",
        re.I,
    )
    for clause in re.split(r"[\n؛;!؟]+", text.translate(_DIGITS).replace("ي", "ی").replace("ك", "ک")):
        matches = list(quotes.finditer(clause))
        for i, match in enumerate(matches):
            values = [float(v.replace(",", "")) for v in match.groups() if v is not None]
            if extracted not in values:
                continue
            start = matches[i - 1].end() if i else 0
            end = matches[i + 1].start() if i + 1 < len(matches) else len(clause)
            phrase = clause[max(start, match.start() - 20):min(end, match.end() + 24)]
            if (not re.search(unit_pattern, phrase, re.I)
                    or not re.search(r"\$|dollars?|دلار|" + CARGO_CURRENCIES["CAD"], phrase, re.I)
                    or re.search(r"\b(?:USD|AUD|AZN|AMD|IRR)\b|تومان|ریال", phrase, re.I)
                    or any(code != "CAD" and re.search(pattern, phrase, re.I)
                           for code, pattern in CARGO_CURRENCIES.items())):
                continue
            return min(values)
    return None
