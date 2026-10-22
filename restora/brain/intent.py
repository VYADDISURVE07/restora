"""Is this row meant to succeed or meant to fail?

A negative test ("login with a wrong password gives 401") must never be 'healed' into passing:
that would hide exactly the behaviour the tester wanted to check. The brain only heals rows that
are meant to succeed.
"""
from __future__ import annotations

import json
import re

_NEGATIVE = re.compile(
    r"\b(invalid|wrong|incorrect|without|missing|no\s+(?:token|auth|header|body|name|email|password|key)|"
    r"not\s+found|unauthori[sz]ed|forbidden|expired|bad|reject(?:ed|s)?|is\s+rejected|fails?|failed|failure|error|negative|"
    r"denied|duplicate|empty|blank|malformed|unknown|too\s+(?:long|short|large|small|many|big)|exceed(?:s|ed|ing)?|"
    r"over\s+(?:the\s+)?limit|below\s+(?:the\s+)?minimum|above\s+(?:the\s+)?maximum|locked|blocked|insufficient|"
    r"unsupported|illegal|tamper(?:ed)?|(?:gives|returns|expects?|expected|->|=>|\u2192)\s*[45]\d\d)\b", re.I)
_ERROR_KEYS = {"error", "errors", "fault", "faultstring", "exception"}


def negative_reason(row) -> str | None:
    """Why this row is a negative test, or None when it is meant to succeed."""
    status = getattr(row, "expected_status", None)
    if status is not None:
        if 200 <= status < 300:
            return None
        return f"it expects HTTP {status}"
    m = _NEGATIVE.search(getattr(row, "name", "") or "")
    if m:
        return f"its name says '{m.group(1)}'"
    exp = getattr(row, "expected_response", None)
    if exp and exp.lstrip().startswith("{"):
        try:
            data = json.loads(exp)
        except ValueError:
            data = None
        if isinstance(data, dict) and data and set(k.lower() for k in data) <= _ERROR_KEYS | {"message", "code", "status", "detail", "details", "title", "type"} \
                and set(k.lower() for k in data) & _ERROR_KEYS:
            return "its sample response is an error message"
    return None


def is_negative(row) -> bool:
    return negative_reason(row) is not None
