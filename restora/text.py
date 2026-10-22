"""Small text helpers shared by the loader, the client and the masker."""
from __future__ import annotations

import json
import re
from typing import Any

FORM_RE = re.compile(r"^[^=&\s<>{}\[\]\"]+=[^&\s]*(&[^=&\s<>{}\[\]\"]+=[^&\s]*)*$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.$\- ]+?)\s*\}\}")

_SMART = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


def looks_like_json(text: str) -> bool:
    return text.lstrip()[:1] in ("{", "[")


def loads_lenient(text: str) -> tuple[Any, str, bool]:
    """Parse JSON. If it only fails because of 'smart quotes' (pasted from Word/Outlook/Teams),
    fix those and say so. Returns (value, text_used, was_fixed). Raises ValueError otherwise."""
    try:
        return json.loads(text), text, False
    except ValueError as first:
        fixed = text.translate(_SMART)
        if fixed != text:
            try:
                return json.loads(fixed), fixed, True
            except ValueError:
                pass
        raise first


def clip(text: str, limit: int = 50_000) -> str:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text) - limit} more characters)"
