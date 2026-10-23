# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""One or two plain-English sentences for every failure: what happened and what to do about it."""
from __future__ import annotations

import re


def explain(record, row=None) -> str:
    """A short explanation for a FAILED record (or '' when nothing useful can be said)."""
    if record.result != "FAILED":
        return ""
    f = " ".join(record.failures or [])
    status, expected = record.http_status, record.expected_status
    suggestion = getattr(record, "suggestion", "") or ""
    if suggestion:
        fix = suggestion.split(": ", 1)[-1]
        return (f"Restora found a fix that works ({fix}) but did not apply it, because healing is set to 'suggest'. "
                f"The corrected sheet already contains it - review and use it.")
    unresolved = getattr(record, "unresolved", None) or []
    if unresolved:
        names = ", ".join(u for u, _ in unresolved[:3])
        why = unresolved[0][1]
        return f"Restora could not fill {names}: {why}. Nothing was sent for this row."
    if status is None:
        if "SSL" in f:
            return "The connection failed at the security (SSL) check. For a test server with its own certificate, run with --insecure."
        if "within" in f and "seconds" in f:
            return "The server did not answer in time. It may be slow or down; try --timeout 60."
        if "connect" in f.lower():
            return "Restora could not reach the server. Check that it is running and reachable from this machine (VPN, proxy, firewall)."
        if record.failures:
            return record.failures[0]
        return ""
    negative = bool(row is not None and row.brain.get("negative"))
    from .errors import looks_like_crash
    crash = looks_like_crash(getattr(record, "response_body", None))
    if crash and status < 500:
        return (f"The API crashed while handling this request ('{crash}' in its answer), even though it answered "
                f"HTTP {status}. That is a bug in the API, not in your sheet - Restora never heals it, it reports it.")
    if status >= 500:
        return (f"The server failed (HTTP {status}) while handling this request. That points to a bug in the API, "
                f"not in your sheet - Restora never heals server errors, it reports them.")
    if expected is not None and not 200 <= expected < 300 and 200 <= status < 300:
        setup = [c for a in (getattr(record, "heal_attempts", None) or []) if a.get("source") == "guard" for c in a["changes"]]
        if setup:
            return (f"At first the API refused this request, but only because of a setup problem. Once Restora fixed that "
                    f"({'; '.join(setup)}), the API accepted it (HTTP {status}): it does not refuse what this test checks. "
                    f"Without the fix the test would have passed for the wrong reason. If the request really should be "
                    f"refused, this is a bug - possibly a security one.")
        return (f"You expected the API to reject this request (HTTP {expected}), but it accepted it (HTTP {status}). "
                f"If the request really should be refused, this is a bug - possibly a security one.")
    if status == 401:
        if negative:
            return f"The API refused the login (401) as your test expected, but the status or message differs from the sample."
        return ("The API says the request is not logged in (401) and no earlier row returned a token to use. "
                "Add a Login row before this one, or put 'Authorization: Bearer <token>' in Headers.")
    if status == 403:
        return "The API knows who you are but refused access (403): this user may not have permission for this action."
    if status == 404 and not negative:
        return "The API says this address does not exist (404). Check the URL path and any id in it."
    if status == 405:
        return "The API does not accept this method on this address (405). Check the Method column."
    if status == 429:
        return "The API is limiting how fast it can be called (429). Run fewer rows at once or wait a little."
    healed_attempts = getattr(record, "heal_attempts", None) or []
    prefix = ""
    if healed_attempts and 200 <= status < 300:
        prefix = "Restora fixed the request, but the response still differs from your sample. "
    elif healed_attempts:
        prefix = "Restora tried to fix the request but the API still refused it. "
    diffs = [x for x in record.failures if x.startswith("$")]
    if diffs:
        missing = [d.split(":")[0] for d in diffs if "missing from the response" in d]
        extra = [d.split(":")[0] for d in diffs if "not in the sample response" in d]
        typed = [d.split(":")[0] for d in diffs if "expected string, got number" in d or "expected number, got string" in d]
        changed = [d for d in diffs if ": expected " in d and " got " in d and d not in typed]
        parts = []
        if missing:
            parts.append(f"the API no longer returns {_list(missing)}")
        if extra:
            parts.append(f"it now also returns {_list(extra)} (add them to the sample, with \"*\" if they change)")
        if typed:
            parts.append(f"{_list(typed)} came back as a different type (number vs text)")
        case_only = [d for d in changed if _case_only(d)]
        if case_only:
            parts.append(f"{_list([d.split(':')[0] for d in case_only])} differ only in upper/lower case")
        other = [d for d in changed if d not in case_only]
        if other:
            parts.append(f"{len(other)} value(s) differ, e.g. {other[0]}")
        if parts:
            return prefix + "Compared with your sample: " + "; ".join(parts) + "."
    if "HTTP status was" in f:
        m = re.search(r"HTTP status was (\d+), expected (\d+)", f)
        if m:
            return prefix + f"The API answered {m.group(1)} instead of the {m.group(2)} you expected."
        return prefix + f"The API answered {status}, which is not a success code."
    return prefix.strip()


def _list(items: list) -> str:
    items = [i.replace("$.", "") for i in items]
    if len(items) <= 3:
        return ", ".join(items)
    return ", ".join(items[:3]) + f" and {len(items) - 3} more"


def _case_only(diff: str) -> bool:
    m = re.search(r"expected '([^']*)', got '([^']*)'", diff)
    return bool(m) and m.group(1).lower() == m.group(2).lower() and m.group(1) != m.group(2)
