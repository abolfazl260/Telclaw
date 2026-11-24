"""Shared cargo type coercion for legacy strings and provider arrays."""
import json
import re

def _name(value):
    value = str(value or "").strip().casefold()
    return value.replace("ي", "ی").replace("ك", "ک").replace("\u200c", "_").replace("_", " ").replace("-", " ").replace("  ", " ")

def normalize_cargo_types(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else value
        except ValueError:
            pass
    parts = value if isinstance(value, list) else re.split(r"[,،;/+]|\band\b", str(value or ""), flags=re.I)
    aliases = {
        "document": "documents", "documents": "documents", "مدارک": "documents", "اسناد": "documents",
        "clothing": "clothes", "clothes": "clothes", "لباس": "clothes", "پوشاک": "clothes",
        "electronic": "electronics", "electronics": "electronics", "گوشی": "electronics",
        "موبایل": "electronics", "لپ تاپ": "electronics", "لپتاپ": "electronics",
        "food": "food", "خوراکی": "food", "مواد غذایی": "food",
        "other": "other", "سایر": "other",
    }
    result = []
    for part in parts:
        v = aliases.get(_name(part))
        if v and v not in result:
            result.append(v)
    return result

