# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Hide secrets in anything that goes into a report (headers, JSON bodies, diff values).

Masking is for DISPLAY only. The real values are still sent to the API and still compared.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode

from .text import FORM_RE

MASK = "****"

SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "apikey", "x-auth-token", "x-access-token",
}

_EXACT_KEYS = {"password", "passwd", "pwd", "pin", "otp", "cvv", "secret", "token", "authorization", "apikey"}
_KEY_SUFFIXES = ("password", "secret", "token", "apikey")


def is_sensitive_key(key: str) -> bool:
    k = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return k in _EXACT_KEYS or k.endswith(_KEY_SUFFIXES)


def mask_headers(headers: dict) -> dict:
    return {k: (MASK if str(k).lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def mask_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (MASK if is_sensitive_key(k) and not isinstance(v, (dict, list)) else mask_json(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask_json(v) for v in value]
    return value


def mask_text(text: str | None) -> str | None:
    """Mask a request/response body that may be JSON, a urlencoded form, or plain text."""
    if not text:
        return text
    s = text.strip()
    if s[:1] in "{[":
        try:
            return json.dumps(mask_json(json.loads(s)), indent=2, ensure_ascii=False)
        except ValueError:
            return text
    if FORM_RE.match(s):
        pairs = parse_qsl(s, keep_blank_values=True)
        return urlencode([(k, MASK if is_sensitive_key(k) else v) for k, v in pairs], safe="*")
    return text


def mask_for_path(path: str, value: Any) -> Any:
    """Mask a diff value when the last key in its JSON path is sensitive ($.user.password)."""
    last = re.split(r"[.\[]", path)[-1].rstrip("]")
    return MASK if is_sensitive_key(last) else value
