"""Compare the sample (expected) response with what the API really returned.

Rules - deliberately few:
  * JSON is compared exactly: same keys, same values, same list lengths. Key order does not matter.
  * An expected value of "*" means "any value, but the key must be there" - use it for ids, timestamps, tokens.
  * Anything that is not JSON is compared as text, ignoring differences in whitespace.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .client import Outcome
from .mask import mask_for_path
from .status import StatusClass, status_matches
from .text import looks_like_json

WILDCARD = "*"


@dataclass
class Diff:
    path: str
    kind: str          # missing | unexpected | different | type | length | not_json | text
    expected: Any = None
    actual: Any = None

    def describe(self) -> str:
        e, a = mask_for_path(self.path, self.expected), mask_for_path(self.path, self.actual)
        if self.kind == "missing":
            return f"{self.path}: missing from the response (expected {_show(e)})"
        if self.kind == "unexpected":
            return f"{self.path}: not in the sample response, but the API returned {_show(a)}"
        if self.kind == "length":
            return f"{self.path}: expected {self.expected} item(s), got {self.actual}"
        if self.kind == "type":
            return f"{self.path}: expected {_kind(self.expected)}, got {_kind(self.actual)} ({_show(a)})"
        if self.kind == "not_json":
            return "$: the sample response is JSON but the API did not return JSON (got: %s)" % _show(a, 120)
        if self.kind == "text":
            return "response text differs from the sample response"
        if e == a:  # both hidden by masking
            return f"{self.path}: value differs (hidden because the field name looks like a secret)"
        return f"{self.path}: expected {_show(e)}, got {_show(a)}"


def _show(value: Any, limit: int = 80) -> str:
    s = repr(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "..."


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "list" if isinstance(value, list) else "object"


def _same_scalar(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def diff_json(expected: Any, actual: Any, path: str = "$", tolerate=None, accepted: list | None = None,
              key: str = "") -> list[Diff]:
    """Differences between the sample and the real response.

    tolerate(path, key, expected, actual) -> reason | None  lets the brain accept values that change on every
    call (ids, timestamps, tokens); each accepted difference is added to `accepted` as (path, reason)."""
    if isinstance(expected, str) and expected == WILDCARD:
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        out = []
        for k, exp_val in expected.items():
            child = f"{path}.{k}"
            if k not in actual:
                out.append(Diff(child, "missing", expected=exp_val))
            else:
                out += diff_json(exp_val, actual[k], child, tolerate, accepted, str(k))
        for k in actual:
            if k not in expected:
                out.append(Diff(f"{path}.{k}", "unexpected", actual=actual[k]))
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [Diff(path, "length", expected=len(expected), actual=len(actual))]
        out = []
        for i, (e, a) in enumerate(zip(expected, actual)):
            out += diff_json(e, a, f"{path}[{i}]", tolerate, accepted, key)
        return out
    if _kind(expected) != _kind(actual):
        return [Diff(path, "type", expected=expected, actual=actual)]
    if not _same_scalar(expected, actual):
        if tolerate is not None:
            reason = tolerate(path, key, expected, actual)
            if reason:
                if accepted is not None:
                    accepted.append((path, reason))
                return []
        return [Diff(path, "different", expected=expected, actual=actual)]
    return []


def _squash(text: str) -> str:
    return re.sub(r">\s+<", "><", " ".join(text.split()))


def check_status(expected: int | None, actual: int) -> str | None:
    if expected is None:
        if 200 <= actual < 300:
            return None
        return f"HTTP status was {actual}; with no ExpectedStatus given, any 2xx is accepted"
    if status_matches(expected, actual):
        return None
    return f"HTTP status was {actual}, expected {'any ' if isinstance(expected, StatusClass) else ''}{expected}"


def check_body(expected_text: str | None, outcome: Outcome, tolerate=None, accepted: list | None = None) -> list[Diff]:
    if not expected_text:
        return []
    if looks_like_json(expected_text):
        expected = json.loads(expected_text)
        if not outcome.is_json:
            return [Diff("$", "not_json", actual=outcome.text[:120])]
        return diff_json(expected, outcome.json_value, tolerate=tolerate, accepted=accepted)
    if _squash(expected_text) == _squash(outcome.text):
        return []
    return [Diff("$", "text", expected=expected_text, actual=outcome.text)]
