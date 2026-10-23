# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Self-healing: read why the API rejected a request, fix the request, try again.

Ground rules (these are what make healing safe to use on a test suite):
  * Only rows that are meant to succeed are healed. A negative test is never "fixed" into passing.
  * Server errors (5xx) are never healed: they are bugs in the API and are reported as such.
  * Secrets are never invented. A token can only come from an earlier response; passwords and API keys
    must be typed by the tester.
  * Every change is recorded with its reason, the row is marked HEALED (not PASSED), and the fix is
    written to the corrected sheet for a human to accept.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from . import jsonpath
from .errors import Issue
from .generate import is_secret_field, kind_for
from .names import is_id_name, norm, similarity, words
from .repair import RepairFailed, infer_method, repair_json

DEFAULT_STATUSES = (400, 401, 403, 405, 406, 409, 415, 422, 428, 429)
_UNIQUE_KINDS = {"email", "username", "phone", "pan", "account_number", "upi", "gstin"}


@dataclass
class Change:
    where: str                  # body | header | query | method | wait
    action: str                 # set | remove
    name: str = ""              # body: JSON path text;  header / query: its name
    value: Any = None
    note: str = ""              # 'added phone = 9876543210'
    ref: str = ""               # what to write in the corrected sheet instead of the literal ({{Login.access_token}})


@dataclass
class Patch:
    changes: list
    reason: str
    source: str = "rule"        # rule | ai | plugin
    confidence: float = 0.9

    def summary(self) -> str:
        return "; ".join(c.note for c in self.changes if c.note)


@dataclass
class HealContext:
    memory: Any
    generator: Any
    row: Any
    sample: Any = None          # the row's sample response (parsed), used as a source of likely values
    ai: Any = None
    plugins: Any = None
    tried: set = field(default_factory=set)


# ----------------------------------------------------------------------------- helpers on a prepared request

def body_json(prepared):
    if prepared.body is None:
        return None
    try:
        return json.loads(prepared.body)
    except ValueError:
        return None


def is_form(prepared) -> bool:
    ct = next((v for k, v in prepared.headers.items() if k.lower() == "content-type"), "")
    return "x-www-form-urlencoded" in ct.lower()


def header_get(headers: dict, name: str):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return k, v
    return None, None


def apply(prepared, patch: Patch):
    """A new prepared request with the patch applied (the original is untouched)."""
    p = copy.deepcopy(prepared)
    data = body_json(p)
    form = None if data is not None or not is_form(p) else parse_qsl(p.body or "", keep_blank_values=True)
    for ch in patch.changes:
        if ch.where == "method":
            p.method = str(ch.value).upper()
        elif ch.where == "header":
            k, _ = header_get(p.headers, ch.name)
            if ch.action == "remove":
                if k:
                    del p.headers[k]
            else:
                p.headers[k or ch.name] = str(ch.value)
        elif ch.where == "query":
            p.url = set_query(p.url, ch.name, None if ch.action == "remove" else ch.value)
        elif ch.where == "body":
            if form is not None:
                form = [(k, v) for k, v in form if k.lower() != ch.name.lower()]
                if ch.action != "remove":
                    form.append((ch.name, "" if ch.value is None else str(ch.value)))
                continue
            if ch.name == "$":
                data = ch.value
                continue
            if data is None:
                data = {}
            parts = jsonpath.parse(ch.name)
            if ch.action == "remove":
                jsonpath.delete(data, parts)
            else:
                jsonpath.put(data, parts, ch.value)
        elif ch.where == "url":
            p.url = str(ch.value)
    if form is not None:
        p.body = urlencode(form)
    elif data is not None:
        p.body = json.dumps(data, ensure_ascii=False)
    return p


def set_query(url: str, name: str, value) -> str:
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != name.lower()]
    if value is not None:
        pairs.append((name, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, quote_via=quote), parts.fragment))


def _key_style(data, name: str) -> str:
    """Use the body's own naming style for a field the API named differently ('Email' -> 'email')."""
    if not isinstance(data, dict) or not data:
        return name
    keys = [k for k in data if isinstance(k, str)]
    if name[:1].isupper() and keys and all(k[:1].islower() for k in keys):
        return name[:1].lower() + name[1:]
    return name


def find_field(data, name: str):
    """Path of the body field the API is talking about (exact, then case/format-insensitive, then nested)."""
    if not isinstance(data, (dict, list)):
        return None
    parts = jsonpath.parse(name)
    if parts and jsonpath.exists(data, parts):
        return parts
    target = norm(name.split(".")[-1])
    found = jsonpath.find_key(data, "", lambda k: norm(k) == target)
    return found


# ----------------------------------------------------------------------------- where values come from

def value_for(field_name: str, ctx: HealContext, data=None, hint: dict | None = None, fresh: bool = False):
    """(value, how, ref) for a field the API wants. None when it must be typed by a person."""
    hint = dict(hint or {})
    leaf = field_name.split(".")[-1]
    # 1. the row's own sample response often shows the intended value (currency 'USD' in a rates call)
    if ctx.sample is not None and not fresh:
        p = jsonpath.find_key(ctx.sample, "", lambda k: norm(k) == norm(leaf))
        if p:
            v = jsonpath.get(ctx.sample, p)
            if v not in (None, "*", "") and not isinstance(v, (dict, list)):
                return v, "taken from this row's sample response", ""
    # 2. references to things that already exist come from earlier responses
    reference_like = is_id_name(leaf) or kind_for(leaf) == "account_number" or norm(leaf).endswith("account")
    if reference_like and ctx.memory is not None and not fresh:
        m = ctx.memory.lookup(leaf, exclude_row=ctx.row.number, allow_request=False)
        if m is not None and m.score >= 0.7:
            return m.value, m.reason, m.ref
    # 3. fresh, realistic test data
    if is_secret_field(leaf):
        return None, "it is a secret, so it must be typed in the sheet", ""
    if fresh:
        hint["fresh"] = True
    siblings = data if isinstance(data, dict) else None
    v = ctx.generator.value(leaf, hint=hint, siblings=siblings)
    if v is None:
        return None, "it must be typed in the sheet", ""
    kind = kind_for(leaf) or hint.get("format")
    ref = "{{$" + kind + "}}" if kind in _UNIQUE_KINDS or kind == "uuid" else ""
    return v, "generated test data", ref


def _coerce(value, type_name: str):
    if type_name in ("number",):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        s = str(value).replace(",", "").strip()
        try:
            f = float(s)
            return int(f) if f.is_integer() and "." not in s else f
        except ValueError:
            return None
    if type_name == "integer":
        try:
            return int(float(str(value).replace(",", "").strip()))
        except ValueError:
            return None
    if type_name == "string":
        return value if isinstance(value, str) else (json.dumps(value) if isinstance(value, (dict, list)) else str(value))
    if type_name == "boolean":
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off"):
            return False
        return None
    if type_name == "array":
        return value if isinstance(value, list) else [value]
    if type_name == "date":
        return _to_date(value, "YYYY-MM-DD")
    return None


def _to_date(value, fmt: str):
    s = str(value).strip()
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d", "%d %b %Y", "%d %B %Y"):
        try:
            d = _dt.datetime.strptime(s[:len(_dt.date.today().strftime(pattern)) + 2].strip(), pattern).date()
            break
        except ValueError:
            continue
    else:
        return None
    f = fmt.upper().replace("YYYY", "%Y").replace("YY", "%y").replace("MM", "%m").replace("DD", "%d")
    return d.strftime(f)


def _show(v) -> str:
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 44 else s[:40] + '..."'[: 4 if isinstance(v, str) else 3]


# ----------------------------------------------------------------------------- the rules

def diagnose(prepared, outcome, issues: list, ctx: HealContext) -> Patch | None:
    """A patch that should make the request acceptable, or None. Tries rules in a fixed order."""
    msg = _api_said(outcome, issues)
    for rule in (_rule_auth, _rule_content_type, _rule_accept, _rule_method, _rule_json, _rule_fields,
                 _rule_duplicate, _rule_rate_limit):
        patch = rule(prepared, outcome, issues, ctx, msg)
        if patch is not None and patch.changes:
            sig = _signature(patch)
            if sig in ctx.tried:
                continue
            ctx.tried.add(sig)
            return patch
    return None


def diagnose_setup(prepared, outcome, issues: list, ctx: HealContext, auth: bool = False) -> Patch | None:
    """Like diagnose(), but for setup problems only: a missing header, the login token, the content type.

    Used on negative tests (see guard.py), whose body must never change: a patch that would touch anything
    other than headers is refused. `issues` must already be limited to the setup issues."""
    msg = _api_said(outcome, issues)
    rules = [_rule_auth] if auth else []
    if any(i.kind == "content_type" for i in issues):
        rules.append(_rule_content_type)
    if any(i.kind == "accept" for i in issues):
        rules.append(_rule_accept)
    if any(i.location == "header" for i in issues):
        rules.append(_rule_fields)
    for rule in rules:
        patch = rule(prepared, outcome, issues, ctx, msg)
        if patch is None or not patch.changes or any(c.where != "header" for c in patch.changes):
            continue
        sig = _signature(patch)
        if sig in ctx.tried:
            continue
        ctx.tried.add(sig)
        return patch
    return None


def _signature(patch: Patch) -> str:
    return json.dumps([(c.where, c.action, c.name.lower(), str(c.value)) for c in patch.changes], sort_keys=True)


def _api_said(outcome, issues=None) -> str:
    from .errors import first_message
    m = first_message(outcome.json_value if outcome.is_json else None, outcome.text)
    m = " ".join(m.split())
    if (not m or m[:1] in "{[" or m.lower() in ("validation failed", "bad request", "one or more validation errors occurred."))\
            and issues:
        parts = [f"{i.field}: {i.message}" if i.field and i.message and i.field.lower() not in i.message.lower()
                 else (i.message or i.kind) for i in issues if i.message or i.field][:3]
        if parts:
            m = "; ".join(dict.fromkeys(parts))
    return m if len(m) <= 140 else m[:137] + "..."


def _said(msg: str, status) -> str:
    return f"The API answered {status}" + (f" ('{msg}')" if msg else "")


def _rule_auth(prepared, outcome, issues, ctx, msg):
    if outcome.status not in (401, 403):
        return None
    www = (outcome.headers.get("WWW-Authenticate") or outcome.headers.get("www-authenticate") or "")
    if www.lower().startswith("basic"):
        return None
    k, current = header_get(prepared.headers, "Authorization")
    if outcome.status == 403 and current:
        return None                                    # logged in but not allowed: a real permission result
    m = ctx.memory.lookup("token", exclude_row=ctx.row.number) if ctx.memory else None
    if m is None:
        return None
    scheme = "Bearer"
    tt = ctx.memory.lookup("token type", exclude_row=ctx.row.number) if ctx.memory else None
    if tt is not None and isinstance(tt.value, str) and tt.value.lower() in ("bearer", "token", "jwt"):
        scheme = "Bearer" if tt.value.lower() in ("bearer", "jwt") else tt.value
    new = f"{scheme} {m.value}"
    if current == new:
        return None
    ref = f"{scheme} {m.ref}" if m.ref else ""
    what = "replaced the Authorization header with a fresh token" if current else "added an Authorization header"
    src = f"'{m.entry.row_name}'" if m.entry else "An earlier row"
    return Patch([Change("header", "set", "Authorization", new, f"{what} (the token {m.reason})", ref)],
                 f"{_said(msg, outcome.status)}: it needs a login token. {src} returned one, "
                 f"so Restora sent it as '{scheme} ...'.")


def _rule_content_type(prepared, outcome, issues, ctx, msg):
    ct_issue = next((i for i in issues if i.kind == "content_type"), None)
    if outcome.status != 415 and ct_issue is None:
        return None
    _, current = header_get(prepared.headers, "Content-Type")
    expected = (ct_issue.detail.get("expected") if ct_issue else None) or ""
    expected = expected.split(",")[0].strip()
    body = (prepared.body or "").lstrip()
    if not expected:
        if body[:1] in "{[":
            expected = "application/json"
        elif body.startswith("<"):
            expected = "application/xml"
        elif "=" in body:
            expected = "application/x-www-form-urlencoded"
    if not expected or (current or "").split(";")[0].strip().lower() == expected.lower():
        return None
    return Patch([Change("header", "set", "Content-Type", expected, f"set Content-Type to {expected}")],
                 f"{_said(msg, outcome.status)}: the body was sent with the wrong content type, so Restora sent it as {expected}.")


def _rule_accept(prepared, outcome, issues, ctx, msg):
    if outcome.status != 406:
        return None
    _, current = header_get(prepared.headers, "Accept")
    if current == "*/*":
        return None
    return Patch([Change("header", "set", "Accept", "*/*", "set Accept to */*")],
                 f"{_said(msg, outcome.status)}: it could not answer in the format asked for, so Restora accepted any format.")


def _rule_method(prepared, outcome, issues, ctx, msg):
    if outcome.status != 405:
        return None
    allow = next((i.detail.get("allow") for i in issues if i.kind == "method"), None) or ""
    allowed = [a.strip().upper() for a in re.split(r"[,\s]+", allow) if a.strip()]
    allowed = [a for a in allowed if a not in ("OPTIONS", "HEAD", prepared.method)]
    if not allowed:
        return None
    wanted, why = infer_method(ctx.row.name, prepared.body is not None)
    if wanted in allowed and wanted != prepared.method:
        choice, because = wanted, f"the row name says it is {why.replace('the name says ', '')}"
    else:
        order = ["PUT", "PATCH", "POST", "DELETE", "GET"] if prepared.body is not None else ["GET", "DELETE", "PUT", "PATCH", "POST"]
        choice = next((m for m in order if m in allowed), allowed[0])
        because = "it is the closest allowed method"
    return Patch([Change("method", "set", "", choice, f"changed the method from {prepared.method} to {choice}")],
                 f"The API does not accept {prepared.method} here (405); it allows {', '.join(allowed)}. Restora used {choice} because {because}.",
                 confidence=0.75)


def _rule_json(prepared, outcome, issues, ctx, msg):
    if not any(i.kind == "json" for i in issues) or prepared.body is None:
        return None
    try:
        json.loads(prepared.body)
        return None
    except ValueError:
        pass
    try:
        value, _, fixes = repair_json(prepared.body)
    except RepairFailed:
        return None
    return Patch([Change("body", "set", "$", value, "repaired the JSON body (" + ", ".join(fixes) + ")")],
                 f"{_said(msg, outcome.status)}: the body was not valid JSON. Restora repaired it: {', '.join(fixes)}.")


def _rule_fields(prepared, outcome, issues, ctx, msg):
    if outcome.status not in (400, 422, 428) and not (outcome.status == 409 and any(i.field for i in issues)):
        return None
    data = body_json(prepared)
    changes = []
    for issue in issues:
        ch = None
        if issue.location in ("query",):
            ch = _fix_query(prepared, issue, ctx)
        elif issue.location == "header":
            ch = _fix_header(prepared, issue, ctx)
        elif issue.kind in ("missing",) and issue.field and _looks_like_header(issue.field) and not find_field(data, issue.field):
            ch = _fix_header(prepared, issue, ctx)
        elif issue.field:
            ch = _fix_body_field(prepared, data, issue, ctx)
        if ch is not None and not any(c.where == ch.where and c.name.lower() == ch.name.lower() for c in changes):
            changes.append(ch)
    if not changes:
        return None
    what = "; ".join(c.note for c in changes)
    return Patch(changes, f"{_said(msg, outcome.status)}. Restora {what}.")


def _looks_like_header(name: str) -> bool:
    return bool(re.match(r"^x-[\w\-]+$|^(idempotency-key|authorization|api-key|x_[\w]+)$", name, re.I))


def _fix_query(prepared, issue: Issue, ctx):
    if issue.kind not in ("missing", "format", "type", "enum"):
        return None
    current = dict(parse_qsl(urlsplit(prepared.url).query, keep_blank_values=True))
    if issue.kind == "enum" and issue.detail.get("allowed"):
        value = _pick_allowed(current.get(issue.field), issue.detail["allowed"])
        return Change("query", "set", issue.field, value, f"set the {issue.field} query parameter to {_show(value)} (one of the allowed values)")
    value, how, ref = value_for(issue.field, ctx)
    if value is None:
        return None
    return Change("query", "set", issue.field, value, f"added the query parameter {issue.field}={_show(value)} ({how})", ref)


def _fix_header(prepared, issue: Issue, ctx):
    if issue.kind != "missing":
        return None
    name = issue.field
    if norm(name) in ("authorization",):
        return None
    k, current = header_get(prepared.headers, name)
    if current:
        return None
    kind = kind_for(name)
    if kind == "uuid" or re.search(r"request|idempot|correlation|trace|nonce", name, re.I):
        value, how, ref = ctx.generator.value(name, kind="uuid"), "a new unique id", "{{$uuid}}"
    else:
        value, how, ref = value_for(name, ctx)
    if value is None:
        return None
    return Change("header", "set", name, value, f"added the header {name}: {_show(value)} ({how})", ref)


def _fix_body_field(prepared, data, issue: Issue, ctx):
    if data is None and prepared.body and not is_form(prepared):
        return None                                        # not JSON and not a form: nothing safe to edit
    target = data if data is not None else {}
    path = find_field(target, issue.field)
    current = jsonpath.get(target, path) if path else None
    name = jsonpath.fmt(path)[2:] if path else _key_style(target, issue.field)
    label = name.split(".")[-1]
    if issue.kind == "missing":
        if path and current not in (None, "", [], {}):
            return None                                    # the field is there and filled: renaming is a guess
        value, how, ref = value_for(name, ctx, target)
        if value is None:
            return None
        return Change("body", "set", name, value, f"added {label} = {_show(value)} ({how})", ref)
    if issue.kind == "unknown":
        if not path:
            return None
        return Change("body", "remove", name, None, f"removed {label}, which the API does not accept")
    if issue.kind == "enum":
        allowed = issue.detail.get("allowed") or []
        if not allowed:
            return None
        value = _pick_allowed(current, allowed)
        if value == current:
            return None
        return Change("body", "set", name, value, f"changed {label} from {_show(current)} to {_show(value)} (allowed: {', '.join(map(str, allowed[:6]))})")
    if issue.kind == "type":
        t = issue.detail.get("type")
        if current is None:
            value, how, ref = value_for(name, ctx, target, hint={"type": t})
            if value is None:
                return None
            return Change("body", "set", name, value, f"added {label} = {_show(value)} ({how})", ref)
        value = _coerce(current, t)
        if value is None or value == current and type(value) is type(current):
            value, how, ref = value_for(name, ctx, target, hint={"type": t}, fresh=True)
            if value is None:
                return None
        return Change("body", "set", name, value, f"sent {label} as {t} {_show(value)} instead of {_show(current)}")
    if issue.kind in ("format", "date_format"):
        hint = {}
        if issue.detail.get("format"):
            hint["format"] = issue.detail["format"]
        if issue.detail.get("length"):
            hint["length"] = issue.detail["length"]
        if issue.kind == "date_format":
            fmt = issue.detail.get("date_format") or "YYYY-MM-DD"
            value = _to_date(current, fmt) if current else None
            if value is None:
                value, how, ref = value_for(name, ctx, target, hint={"date_format": fmt, "format": "date"})
            if value is None or value == current:
                return None
            return Change("body", "set", name, value, f"wrote {label} as {fmt}: {_show(value)}")
        value, how, ref = value_for(name, ctx, target, hint=hint, fresh=True)
        if value is None or value == current:
            return None
        return Change("body", "set", name, value, f"replaced {label} {_show(current)} with a valid one: {_show(value)}", ref)
    if issue.kind in ("min_length", "max_length", "length"):
        n = int(issue.detail.get("n") or 0)
        s = "" if current is None else str(current)
        if issue.kind == "min_length" and len(s) < n:
            value = (s or "Restora") + "x" * max(0, n - len(s or "Restora"))
        elif issue.kind == "max_length" and len(s) > n:
            value = s[:n]
        elif issue.kind == "length" and len(s) != n:
            value = (s * (n // max(1, len(s)) + 1))[:n] if s else "1" * n
        else:
            return None
        return Change("body", "set", name, value, f"adjusted the length of {label} to {len(value)}")
    if issue.kind in ("min", "max"):
        n = float(issue.detail.get("n"))
        if issue.kind == "min":
            value = n + 1 if issue.detail.get("strict") else n
            value = max(value, n * 2 if n > 0 else value)
        else:
            value = n - 1 if issue.detail.get("strict") else n
        value = int(value) if float(value).is_integer() else value
        if current is not None and isinstance(current, (int, float)) and (issue.kind == "min" and current >= value or issue.kind == "max" and current <= value):
            return None
        return Change("body", "set", name, value, f"set {label} to {_show(value)} ({'minimum' if issue.kind == 'min' else 'maximum'} {n:g})")
    if issue.kind == "duplicate":
        value, how, ref = value_for(name, ctx, target, fresh=True)
        if value is None or value == current:
            return None
        return Change("body", "set", name, value, f"used a new unique {label} {_show(value)} (the old one already exists)", ref)
    return None


def _pick_allowed(current, allowed: list):
    if current is not None:
        c = str(current).strip().lower()
        for a in allowed:
            if str(a).lower() == c:
                return a
        for a in allowed:
            if str(a).lower().startswith(c[:3]) or c in str(a).lower():
                return a
        best = max(allowed, key=lambda a: similarity(str(a), str(current)))
        if similarity(str(best), str(current)) > 0.5:
            return best
    return allowed[0]


def _rule_duplicate(prepared, outcome, issues, ctx, msg):
    if outcome.status != 409 and not any(i.kind == "duplicate" for i in issues):
        return None
    data = body_json(prepared)
    if not isinstance(data, dict):
        return None
    named = [i.field for i in issues if i.kind == "duplicate" and i.field]
    changes = []
    for parts, key, value in jsonpath.leaves(data):
        if not isinstance(key, str) or not isinstance(value, (str, int)):
            continue
        kind = kind_for(key)
        if (named and any(norm(n.split(".")[-1]) == norm(key) for n in named)) or (not named and kind in _UNIQUE_KINDS):
            new, how, ref = value_for(key, ctx, data, fresh=True)
            if new is not None and new != value:
                changes.append(Change("body", "set", jsonpath.fmt(parts)[2:], new, f"used a new unique {key} {_show(new)}", ref))
    if not changes:
        return None
    return Patch(changes, f"{_said(msg, outcome.status)}: this test data already exists (probably from an earlier run). Restora used fresh values.")


def _rule_rate_limit(prepared, outcome, issues, ctx, msg):
    if outcome.status != 429:
        return None
    ra = outcome.headers.get("Retry-After") or outcome.headers.get("retry-after") or "2"
    try:
        wait = min(10.0, max(0.5, float(ra)))
    except ValueError:
        wait = 2.0
    return Patch([Change("wait", "set", "", wait, f"waited {wait:g} s and tried again")],
                 f"The API asked Restora to slow down (429), so it waited {wait:g} s and tried again.", confidence=1.0)
