"""What the local AI model is asked to do - only when the rules have no answer.

1. suggest_value   a realistic value for a field the rules do not recognise ('purposeCode', 'branchName' ...)
2. heal_patch      a fix for an error message the rules could not read
3. explain         a plain-English explanation when the rules have nothing useful to say
4. write_tests     test rows from a plain-English description ("check that login fails with a wrong password")

Secrets are masked before anything is sent to the model, every answer must match a JSON schema, and every
suggestion is checked (no invented passwords, keys or tokens; only small, explainable changes).
"""
from __future__ import annotations

import json
import re

from ..mask import is_sensitive_key, mask_headers, mask_text
from .generate import is_secret_field, kind_for
from .heal import Change, Patch
from .llm import LLMClient, LLMError
from .names import norm

_VALUE_SCHEMA = {"type": "object", "properties": {
    "value": {"type": "string"},
    "type": {"type": "string", "enum": ["string", "number", "integer", "boolean", "null"]}},
    "required": ["value", "type"]}

_HEAL_SCHEMA = {"type": "object", "properties": {
    "fixable": {"type": "boolean"},
    "diagnosis": {"type": "string"},
    "changes": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
        "where": {"type": "string", "enum": ["body", "header", "query", "method"]},
        "action": {"type": "string", "enum": ["set", "remove"]},
        "name": {"type": "string"},
        "value": {"type": "string"},
        "type": {"type": "string", "enum": ["string", "number", "integer", "boolean", "null"]}},
        "required": ["where", "action", "name", "value", "type"]}}},
    "required": ["fixable", "diagnosis", "changes"]}

_EXPLAIN_SCHEMA = {"type": "object", "properties": {"explanation": {"type": "string"}}, "required": ["explanation"]}

_TESTS_SCHEMA = {"type": "object", "properties": {"tests": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
    "type": "object", "properties": {
        "name": {"type": "string"},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
        "url": {"type": "string"},
        "headers": {"type": "string"},
        "body": {"type": "string"},
        "expected_status": {"type": "string"},
        "expected_response": {"type": "string"}},
    "required": ["name", "method", "url", "headers", "body", "expected_status", "expected_response"]}}},
    "required": ["tests"]}

_PROTECTED_HEADERS = {"authorization", "cookie", "proxyauthorization", "xapikey", "apikey"}
_CREDENTIAL_KINDS = {"password", "otp", "pin", "cvv", "card_number"}


def _typed(value: str, type_name: str):
    if type_name == "null":
        return None
    if type_name in ("number", "integer"):
        try:
            f = float(str(value).replace(",", ""))
            return int(f) if type_name == "integer" or f.is_integer() and "." not in str(value) else f
        except ValueError:
            return value
    if type_name == "boolean":
        return str(value).strip().lower() in ("true", "1", "yes")
    return value


def _clip(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + " ..."


class LocalAI:
    def __init__(self, settings, client: LLMClient | None = None):
        self.settings = settings
        self.client = client or LLMClient(settings.provider, settings.url, settings.model, settings.timeout,
                                          settings.temperature, settings.ai_key if hasattr(settings, "ai_key") else None)
        self.ready, self.message, self.models = False, "", []
        self.calls_left = settings.max_calls
        self.log: list = []                    # (task, seconds) for the report

    def check(self) -> bool:
        self.ready, self.message, self.models = self.client.status()
        return self.ready

    def _ask(self, task: str, system: str, user: str, schema: dict, max_tokens: int = 300):
        if not self.ready or self.calls_left <= 0:
            return None
        self.calls_left -= 1
        before = self.client.seconds
        try:
            answer = self.client.chat_json(system, user, schema, max_tokens)
        except (LLMError, Exception) as e:  # noqa: BLE001 - the AI is optional; never fail a run because of it
            self.log.append((task, round(self.client.seconds - before, 1), f"error: {e}"))
            return None
        self.log.append((task, round(self.client.seconds - before, 1), "ok"))
        return answer if isinstance(answer, dict) else None

    # ------------------------------------------------------------------ 1. a value for an unknown field
    def suggest_value(self, field: str, row=None, siblings=None):
        if is_secret_field(field) or kind_for(field) in _CREDENTIAL_KINDS:
            return None                                    # credentials come from the tester, never from a model
        sib = {k: ("****" if is_sensitive_key(k) else v) for k, v in (siblings or {}).items()
               if not isinstance(v, (dict, list))}
        user = (f"Field name: {field}\n"
                f"API call: {getattr(row, 'method', '')} {_path(getattr(row, 'url', ''))}\n"
                f"Test name: {getattr(row, 'name', '')}\n"
                f"Other fields in the same JSON object: {json.dumps(sib, ensure_ascii=False)[:600]}\n\n"
                "Give one realistic value for this field that the API would accept (Indian context when it matters).\n"
                "Never invent passwords, OTPs, PINs, API keys, tokens or national ID numbers: for those set type to null.\n"
                'Answer as JSON: {"value": "<value as text>", "type": "string|number|integer|boolean|null"}')
        a = self._ask("value", "You create realistic test data for API requests. Reply with JSON only.", user, _VALUE_SCHEMA, 80)
        if not a or a.get("type") == "null":
            return None
        v = _typed(str(a.get("value", "")), str(a.get("type", "string")))
        if v in ("", None) or (isinstance(v, str) and len(v) > 200):
            return None
        return v

    # ------------------------------------------------------------------ 2. a fix the rules could not find
    def heal_patch(self, prepared, outcome, row, memory, ctx=None):
        known = []
        for e in (memory.entries[-40:] if memory else []):
            if e.source in ("response", "header") and not is_sensitive_key(e.key) and len(known) < 15:
                v = e.value if not isinstance(e.value, str) or len(e.value) <= 60 else e.value[:60]
                known.append(f"- {e.key} = {json.dumps(v, ensure_ascii=False)}  (from '{e.row_name}')")
        user = (f"Test name: {row.name}\n"
                f"Request: {prepared.method} {prepared.url}\n"
                f"Headers: {json.dumps(mask_headers(prepared.headers), ensure_ascii=False)}\n"
                f"Body: {_clip(mask_text(prepared.body) if prepared.body else '(none)', 1500)}\n\n"
                f"Response: HTTP {outcome.status}\n{_clip(mask_text(outcome.text), 1500)}\n\n"
                + ("Values returned by earlier requests (use these instead of inventing ids):\n" + "\n".join(known) + "\n\n" if known else "")
                + "This request should succeed. Find the smallest change that makes the API accept it.\n"
                  "Rules: change only body fields, headers, query parameters or the method. Never invent passwords, "
                  "API keys or tokens. If it looks like a server bug (for example an exception or a stack trace), "
                  "or you are not sure, set fixable to false.\n"
                  'Answer as JSON: {"fixable": true, "diagnosis": "<one plain sentence for a tester>", "changes": '
                  '[{"where": "body|header|query|method", "action": "set|remove", "name": "<field, header or parameter; '
                  'for method use \'method\'>", "value": "<new value as text>", "type": "string|number|integer|boolean|null"}]}')
        a = self._ask("heal", "You are a careful API testing assistant. Reply with JSON only.", user, _HEAL_SCHEMA, 350)
        if not a or not a.get("fixable"):
            return None
        changes = []
        for c in a.get("changes") or []:
            where, action, name = c.get("where"), c.get("action"), str(c.get("name") or "").strip()
            if where not in ("body", "header", "query", "method") or action not in ("set", "remove"):
                continue
            value = _typed(str(c.get("value", "")), str(c.get("type", "string")))
            if where == "method":
                m = str(c.get("value") or name).upper()
                if m in ("GET", "POST", "PUT", "PATCH", "DELETE") and m != prepared.method:
                    changes.append(Change("method", "set", "", m, f"changed the method to {m}"))
                continue
            if not name or len(name) > 80:
                continue
            if where == "header" and norm(name) in _PROTECTED_HEADERS:
                continue                                   # credentials come from responses or the tester, never the AI
            if action == "set" and (is_secret_field(name) or is_sensitive_key(name)):
                continue
            label = name.split(".")[-1]
            note = (f"removed {label}" if action == "remove" else
                    f"set {('header ' if where == 'header' else 'query parameter ' if where == 'query' else '')}{label} = "
                    f"{json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}")
            changes.append(Change(where, action, name, value, note))
        if not changes:
            return None
        diagnosis = " ".join(str(a.get("diagnosis") or "").split())[:300] or "The AI model suggested a fix."
        return Patch(changes, f"{diagnosis} (fix suggested by the local AI model)", source="ai", confidence=0.6)

    # ------------------------------------------------------------------ 3. explanation
    def explain(self, record, row, rule_text: str):
        if rule_text:                                      # the rules already said something useful
            return None
        user = (f"Test: {record.name}\n"
                f"Expected: HTTP {record.expected_status or 'any 2xx'}\n"
                f"Got: HTTP {record.http_status}\n"
                f"Problems found:\n" + "\n".join(f"- {f}" for f in record.failures[:8]) + "\n"
                f"Start of the response: {_clip(record.response_body, 600)}\n\n"
                "In at most two short sentences for a non-technical tester: what went wrong and the most likely fix. "
                "Use only the facts above. Do not guess what an error code means: quote it as the API gave it. "
                'Answer as JSON: {"explanation": "..."}')
        a = self._ask("explain", "You explain API test failures in plain English. Reply with JSON only.", user,
                      _EXPLAIN_SCHEMA, 140)
        text = " ".join(str((a or {}).get("explanation") or "").split())
        return (text[:400] + " (explained by the local AI model)") if text else None

    # ------------------------------------------------------------------ 4. tests from plain English
    def write_tests(self, request_text: str, known_rows: list) -> list:
        known = [r for r in known_rows if str(r.get("url") or "").strip()][:20]
        origin = next((m.group(0) for r in known for m in [re.match(r"^[a-z]+://[^/]+", str(r.get("url")), re.I)] if m), "")
        blocks = []
        for i, r in enumerate(known, 1):
            url = str(r.get("url")).strip()
            if url.startswith("/") and origin:
                url = origin + url
            part = [f"{i}. '{r.get('name') or ''}': {str(r.get('method') or 'GET').upper()} {url}"]
            headers = _prompt_headers(str(r.get("headers") or ""))
            if headers:
                part.append("   Headers: " + " | ".join(headers))
            body = str(r.get("body") or "").strip()
            if body:
                part.append("   Body: " + _clip(" ".join((mask_text(body) or "").split()), 400))
            if r.get("expected_status"):
                part.append(f"   Expects: HTTP {r['expected_status']}")
            ret = _keys(r.get("expected_response"))
            if ret:
                part.append(f"   Returns: {ret}")
            blocks.append("\n".join(part))
        user = ("Known API calls in the current sheet:\n" + ("\n".join(blocks) or "(none)") + "\n\n"
                f'Write test rows for: "{request_text}"\n\n'
                "How to write them:\n"
                "- Pick the known call the request is about. Copy its method, URL, headers and body exactly, including "
                "placeholders such as <token> or <accountNumber>.\n"
                "- Then change only what the request is about (for example the amount). Everything else stays as in "
                "the known call.\n"
                "- A request that should be refused: expected_status is the exact code only if a known call shows that "
                "code for the same kind of problem; otherwise write 4xx (any client error).\n"
                "- A request that should succeed: the same expected_status as the known call.\n"
                "- Use only the known endpoints. Leave expected_response empty. Name each test in plain English.\n"
                'Answer as JSON: {"tests": [{"name": "...", "method": "GET|POST|PUT|PATCH|DELETE", "url": "...", '
                '"headers": "Name: value per line, or empty", "body": "JSON text or empty", "expected_status": "e.g. 201, 4xx, or empty", '
                '"expected_response": ""}]}')
        a = self._ask("write_tests", "You write API test cases for a spreadsheet. Reply with JSON only.", user,
                      _TESTS_SCHEMA, 700)
        out = []
        for t in (a or {}).get("tests") or []:
            method = str(t.get("method") or "GET").upper()
            url = str(t.get("url") or "").strip()
            if not url or method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            status = str(t.get("expected_status") or "").strip()
            status = status.lower() if re.fullmatch(r"[1-5]\d\d|[1-5]xx", status, re.I) else ""
            match = _matching_call(method, url, known)
            headers = _with_setup_headers(str(t.get("headers") or "").strip(), match, request_text)
            body = _with_known_placeholders(str(t.get("body") or "").strip(), match, request_text)
            out.append({"name": str(t.get("name") or f"{method} {_path(url)}")[:80], "method": method, "url": url,
                        "headers": headers, "body": body,
                        "expected_status": status, "expected_response": str(t.get("expected_response") or "").strip()})
        return out


def _prompt_headers(text: str) -> list:
    """Header lines for a prompt: placeholders stay, real credentials become placeholders."""
    out = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if not k:
            continue
        if is_sensitive_key(k) and not re.search(r"<[^<>]+>|\{\{.+?\}\}|\{\w+\}", v):
            scheme = v.split(" ", 1)[0] if k.lower() == "authorization" and " " in v else ""
            v = f"{scheme} <token>".strip() if k.lower() == "authorization" else f"<{k}>"
        out.append(f"{k}: {v}")
    return out


def _keys(text) -> str:
    text = str(text or "").strip()
    if text[:1] != "{":
        return ""
    try:
        return ", ".join(list(json.loads(text).keys())[:8])
    except ValueError:
        return ""


def _matching_call(method: str, url: str, known: list):
    """The known call with the same method and path, which a written test was told to copy."""
    path = (_path(url).split("?")[0].rstrip("/") or "/").lower()
    return next((r for r in known if str(r.get("method") or "GET").upper() == method
                 and (_path(str(r.get("url"))).split("?")[0].rstrip("/") or "/").lower() == path), None)


def _with_setup_headers(headers: str, match, request_text: str) -> str:
    """Small models often drop headers they were told to copy. Add the matching known call's headers back,
    except those the request is about ('... without a token is refused' keeps its missing token)."""
    from .guard import _AUTH_WORDS, _mentions
    if match is None:
        return headers
    have = {line.split(":", 1)[0].strip().lower() for line in headers.splitlines() if ":" in line}
    lines = [line for line in headers.splitlines() if line.strip()]
    for line in _prompt_headers(str(match.get("headers") or "")):
        name = line.split(":", 1)[0].strip()
        if name.lower() in have or _mentions(request_text, name):
            continue
        if (is_sensitive_key(name) or name.lower() == "authorization") and _AUTH_WORDS.search(request_text):
            continue
        lines.append(line)
    return "\n".join(lines)


_WHOLE_GAP = re.compile(r"^\s*(<[^<>]+>|\{\{[^{}]+\}\}|\{\w+\})\s*$")


def _with_known_placeholders(body: str, match, request_text: str) -> str:
    """Put back the gaps of the copied call (<accountNumber>, <customerId>) where the model typed a made-up value,
    for fields the request is not about. The brain then fills them with real values from earlier rows."""
    from .guard import _mentions
    if match is None or not body.lstrip().startswith("{") or not str(match.get("body") or "").lstrip().startswith("{"):
        return body
    try:
        mine, theirs = json.loads(body), json.loads(match["body"])
    except ValueError:
        return body
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return body
    changed = False
    for k, v in theirs.items():
        if isinstance(v, str) and _WHOLE_GAP.match(v) and k in mine and mine[k] != v and not _mentions(request_text, k):
            mine[k] = v
            changed = True
    return json.dumps(mine, ensure_ascii=False) if changed else body


def _path(url: str) -> str:
    m = re.match(r"^[a-z]+://[^/]+(.*)$", url or "", re.I)
    return m.group(1) if m else (url or "")


def connect(ai_settings, api_key: str | None = None) -> LocalAI:
    """A LocalAI object that has checked whether the model is reachable (see .ready / .message)."""
    client = LLMClient(ai_settings.provider, ai_settings.url, ai_settings.model, ai_settings.timeout,
                       ai_settings.temperature, api_key)
    ai = LocalAI(ai_settings, client)
    ai.check()
    return ai
