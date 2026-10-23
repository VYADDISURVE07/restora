# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Negative tests that pass for the wrong reason.

A negative test ("a transfer with a negative amount is rejected") can pass by accident: the API may refuse
the request because a required header or the login token is missing, without ever looking at the amount.
Most tools report that as a pass.

After a negative test is refused, Restora reads why. When the reason is a setup problem that the test is not
about (a required header, the login token, the content type), it fixes only that and asks again, so the API
judges what the test is really about:

  * refused again -> HEALED: the row is now refused for the right reason
  * accepted      -> FAILED: the API accepts what the test says it must refuse (a real bug)

Only headers are changed, and only headers the test's name does not mention. The body - the thing a
negative test is about - is never touched. Rules only: the AI model is not involved.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .names import norm, words

_AUTH_WORDS = re.compile(
    r"\b(token|tokens|auth\w*|log\s?in|logged|sign\s?in|signin|unauthori[sz]ed|credentials?|password|passcode|pin|"
    r"otp|session|permission|forbidden|access|api\s?key|secret|expired|jwt|bearer|signature|signed|anonymous|guest)\b",
    re.I)
_AUTH_PATH = re.compile(r"/(auth|login|logout|token|oauth2?|signin|sign-in|session)s?(/|$)", re.I)
_CT_WORDS = re.compile(r"\b(content[\s-]?type|media\s?type|xml|json|form|format|encoding|mime|accept)\b", re.I)
_AUTH_HEADERS = {"authorization", "cookie", "proxyauthorization"}
_PLAIN = {"x", "id", "api", "the", "header", "value"}


def _mentions(text: str, field: str) -> bool:
    """Does the test's name talk about this header? ('without a request id' mentions X-Request-Id)."""
    said = set(words(text))
    own = [w for w in words(field) if len(w) > 2 and w not in _PLAIN]
    return any(w in said or w.rstrip("s") in said for w in own)


def setup_issues(row, prepared, outcome, issues: list) -> tuple:
    """(issues that are about setup rather than about what this test checks, whether the login is one of them)."""
    name = getattr(row, "name", "") or ""
    path = urlsplit(prepared.url or "").path
    about_login = bool(_AUTH_WORDS.search(name)) or bool(_AUTH_PATH.search(path))
    login = outcome.status == 401 and not about_login
    keep = []
    for i in issues:
        if i.location == "header" and i.field and i.kind == "missing":
            spoken = " ".join(words(i.field))
            if norm(i.field) in _AUTH_HEADERS or _AUTH_WORDS.search(spoken):
                continue                       # keys and signatures are the tester's business, never guessed
            if not _mentions(name, i.field):
                keep.append(i)
        elif i.kind in ("content_type", "accept") and not _CT_WORDS.search(name):
            keep.append(i)
    return keep, login


def describe(issues: list, login: bool) -> str:
    parts = [f"the {i.field} header was missing" for i in issues if i.location == "header"]
    parts += ["the body was sent with the wrong content type" for i in issues if i.kind == "content_type"]
    parts += ["the API could not answer in the format asked for" for i in issues if i.kind == "accept"]
    if login:
        parts.insert(0, "the request was not logged in")
    return " and ".join(dict.fromkeys(parts)) or "of a setup problem"
