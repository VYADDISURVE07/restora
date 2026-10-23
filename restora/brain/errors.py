# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Read an API's error response the way a tester does, and turn it into structured issues.

Understands the common shapes: plain messages ("email is required"), Spring / ASP.NET / Rails / Laravel
field-error maps, express-validator and Joi lists, FastAPI / Pydantic `detail` lists, RFC 7807 problem+json
`invalid-params`, JSON:API `errors[].source.pointer`, and SOAP-ish `fault` strings.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field

FIELD = r"""['"`“”]?([A-Za-z_@$][\w.\[\]\-]{0,60})['"`“”]?"""
_STOP = {"the", "a", "an", "this", "that", "it", "value", "values", "field", "fields", "request", "body", "input",
         "data", "payload", "parameter", "param", "property", "one", "json", "header", "query", "object", "item",
         "entity", "resource", "argument", "format", "string", "number", "is", "are", "be", "must", "should", "and",
         "or", "not", "valid", "invalid", "required", "missing", "name_of", "error", "message", "please",
         "your", "provided", "given", "following", "content", "media", "unsupported", "unexpected", "unknown",
         "bad", "malformed", "duplicate", "id_of", "x", "you", "we", "has", "have", "with", "at", "in", "for", "of"}
_TYPES = {"number": "number", "numeric": "number", "float": "number", "decimal": "number", "double": "number",
          "integer": "integer", "int": "integer", "long": "integer", "whole number": "integer",
          "string": "string", "text": "string", "str": "string", "boolean": "boolean", "bool": "boolean",
          "array": "array", "list": "array", "object": "object", "dict": "object", "date": "date"}
_FORMATS = r"(e-?mail(?:\s+address)?|url|uri|uuid|guid|date-?time|date|phone(?:\s+number)?|mobile(?:\s+number)?|ip(?:\s+address)?|pan|ifsc(?:\s+code)?|pin\s?code|pincode|zip(?:\s+code)?|gstin|upi(?:\s+id)?|vpa|time)"


@dataclass
class Issue:
    kind: str                     # missing | format | type | enum | min_length | max_length | length | min | max |
                                  # unknown | duplicate | content_type | json | auth | method | date_format
    field: str = ""
    location: str = ""            # body | query | header | path | ""
    detail: dict = dc_field(default_factory=dict)
    message: str = ""

    def key(self):
        return (self.kind, self.field.lower(), self.location)


def _clean_field(name: str) -> tuple:
    """('customer.email', 'body') from 'body.customer.email' / '$.customer.email' / '/customer/email'."""
    f = (name or "").strip().strip("'\"`").rstrip(".:,")
    location = ""
    if f.startswith("/"):                                            # JSON pointer
        f = ".".join(p for p in f.split("/") if p)
    f = re.sub(r"^\$\.?", "", f)
    m = re.match(r"^(body|query|header|headers|path|params|request)\.(.+)$", f, re.I)
    if m:
        loc = m.group(1).lower()
        location = {"headers": "header", "params": "query", "request": "body"}.get(loc, loc)
        f = m.group(2)
    return f, location


def _ok_field(name: str) -> bool:
    n = (name or "").strip().lower()
    return bool(n) and n not in _STOP and not n.isdigit() and len(n) <= 60


def _allowed_values(text: str) -> list:
    text = text.strip().rstrip(".")
    quoted = re.findall(r"""['"]([^'"]{1,40})['"]""", text)
    if quoted:
        return quoted
    inner = re.search(r"[\[\(\{]([^\]\)\}]+)[\]\)\}]", text)
    body = inner.group(1) if inner else text
    parts = [p.strip(" .'\"") for p in re.split(r",|\bor\b|\||/", body) if p.strip(" .'\"")]
    return [p for p in parts if re.fullmatch(r"[\w\-. ]{1,40}", p)][:20]


def classify(message: str, field_name: str = "", location: str = "") -> list:
    """Issues found in one message. If the API told us the field separately, pass it as field_name."""
    msg = " ".join(str(message or "").split())
    low = msg.lower()
    out: list = []

    def add(kind, f=None, loc=None, **detail):
        f2, loc2 = _clean_field(f if f is not None else field_name)
        if kind not in ("content_type", "json", "auth", "method") and not _ok_field(f2):
            return
        out.append(Issue(kind, f2, loc or loc2 or location, detail, msg))

    # --- request-level problems (no field)
    if re.search(r"content[- ]?type|media\s+type|unsupported\s+media|expected\s+(?:application/)?json\s+body|must\s+be\s+(?:sent\s+as\s+)?(?:application/)?json", low):
        m = re.search(r"(application/[\w.+\-]+|text/[\w.+\-]+|multipart/[\w.+\-]+)", low)
        add("content_type", "", "", expected=m.group(1) if m else None)
    if re.search(r"(?:invalid|malformed|unparsable|could\s+not\s+(?:be\s+)?pars|cannot\s+parse|unexpected\s+(?:token|end|character)|syntax\s+error|not\s+valid)\s*(?:\w+\s+){0,3}?(?:json|request\s+body)|json\s+(?:parse|syntax|decode)", low):
        add("json", "", "")

    # --- the field is known (structured errors)
    if field_name:
        _classify_known(low, msg, add)
        return out

    # --- the field is inside the message
    patterns = [
        ("header", rf"(?:missing|required)\s+(?:request\s+|http\s+)?header\s*[:\-]?\s*{FIELD}"),
        ("header", rf"header\s+{FIELD}\s+(?:is\s+)?(?:required|missing|mandatory)"),
        ("header", rf"{FIELD}\s+header\s+(?:is\s+)?(?:required|missing|mandatory)"),
        ("query", rf"(?:missing|required)\s+query\s+param(?:eter)?\s*[:\-]?\s*{FIELD}"),
        ("query", rf"{FIELD}\s+query\s+param(?:eter)?\s+(?:is\s+)?(?:required|missing|mandatory)"),
        ("query", rf"query\s+param(?:eter)?\s+{FIELD}\s+(?:is\s+)?(?:required|missing)"),
        ("missing", rf"(?:the\s+)?{FIELD}\s+field\s+is\s+(?:required|mandatory)"),
        ("missing", rf"{FIELD}\s+(?:field\s+|parameter\s+|property\s+|attribute\s+)?(?:is|are)\s+(?:a\s+)?(?:required|mandatory|missing)"),
        ("missing", rf"(?:missing|required)\s+(?:required\s+)?(?:field|parameter|property|attribute|key|value|param|input)s?\s*[:\-]?\s*{FIELD}"),
        ("missing", rf"{FIELD}\s*[:\-]\s*(?:field\s+required|required|missing|is\s+required)"),
        ("missing", rf"{FIELD}\s+(?:must|should|cannot|can't|can\s+not|may)\s+not\s+be\s+(?:null|empty|blank|nil|missing)"),
        ("missing", rf"{FIELD}\s+(?:cannot|can't|can\s+not)\s+be\s+(?:null|empty|blank|nil)"),
        ("missing", rf"(?:please\s+)?(?:provide|enter|specify|supply)\s+(?:a\s+|an\s+|the\s+)?{FIELD}(?:\s|$|\.)"),
        ("duplicate", rf"(?:a|an)\s+\w+\s+with\s+(?:this|that|the\s+same|same)\s+{FIELD}\s+(?:already\s+exists|is\s+already\s+registered)"),
        ("duplicate", rf"{FIELD}\s+(?:already\s+exists|is\s+already\s+(?:registered|taken|in\s+use|used)|has\s+already\s+been\s+taken|must\s+be\s+unique|is\s+not\s+unique)"),
        ("duplicate", rf"duplicate\s+(?:value\s+for\s+)?{FIELD}"),
        ("unknown", rf"{FIELD}\s+is\s+not\s+allowed"),
        ("unknown", rf"(?:unknown|unexpected|unrecognized|unrecognised|extra|additional)\s+(?:field|property|key|parameter|attribute)s?\s*[:\-]?\s*{FIELD}"),
        ("field_rule", rf"{FIELD}\s+(?:must|should|needs\s+to|has\s+to)\s+(.+)"),
        ("field_rule", rf"{FIELD}\s+(?:is|was|looks)\s+(not\s+(?:a\s+)?valid.*|invalid.*|malformed.*|incorrect.*|too\s+(?:long|short).*)"),
        ("field_rule", rf"(?:invalid|malformed|bad)\s+(?:value\s+for\s+)?{FIELD}(.*)"),
        ("field_rule", rf"(?:expected|expecting)\s+(\w+)\s+(?:for|at|in)\s+{FIELD}"),
    ]
    for kind, rx in patterns:
        for m in re.finditer(rx, msg, re.I):
            if kind == "field_rule" and rx.startswith("(?:expected"):
                f, rest = m.group(2), "must be " + m.group(1)
            else:
                f = m.group(1)
                rest = m.group(2) if kind == "field_rule" and m.lastindex and m.lastindex >= 2 else ""
            if not _ok_field(_clean_field(f)[0]):
                continue
            if kind == "field_rule":
                before = len(out)
                _classify_known(rest.lower() if rest else low, msg, lambda k, ff=None, ll=None, **d: add(k, f, ll, **d))
                if len(out) == before and re.search(r"invalid|not\s+valid|malformed|incorrect", (rest or low).lower()):
                    add("format", f)
            elif kind in ("header", "query"):
                add("missing", f, kind)
            else:
                add(kind, f)
    return _dedupe(out)


def _classify_known(low: str, msg: str, add) -> None:
    """The rule part of a message whose field we already know (may add more than one issue)."""
    found = []

    def put(kind, **detail):
        found.append(kind)
        add(kind, **detail)

    m = re.search(r"(?:one\s+of|either|allowed\s+values?(?:\s+are)?|must\s+be\s+in|valid\s+values?\s+are|input\s+should\s+be)\s*[:\-]?\s*(.+)", msg, re.I)
    if m and (re.search(r"one\s+of|either|allowed|must\s+be\s+in|valid\s+values", m.group(0), re.I) or " or " in m.group(1)):
        values = _allowed_values(m.group(1))
        if len(values) >= 2 or re.search(r"one\s+of|allowed", m.group(0), re.I) and values:
            put("enum", allowed=values)
            return
    m = re.search(r"""(?:must|should)\s+be\s+((?:['"][^'"]{1,40}['"]\s*(?:,|or|\|)\s*)+(?:or\s+)?['"][^'"]{1,40}['"])""", msg, re.I)
    if m:
        put("enum", allowed=_allowed_values(m.group(1)))
        return
    m = re.search(rf"(?:valid|be\s+(?:a|an)|be\s+in|is\s+not\s+(?:a|an)|correct)\s+(?:valid\s+)?(?:[\w\-]+\s+)?{_FORMATS}\b", low) or \
        re.search(rf"\b(?:invalid|malformed|incorrect)\s+{_FORMATS}\b", low) or re.search(rf"\b{_FORMATS}\s+(?:is\s+)?(?:invalid|not\s+valid|format)", low)
    if m:
        fmt = re.sub(r"\s+(?:address|number|code|id)$", "", m.group(1).replace("e-mail", "email")).replace(" ", "")
        if fmt != "date" or not re.search(r"\d|format|[ymd]{2}", low):
            detail = {"format": fmt}
            d = re.search(r"(\d+)[- ]digit", low)
            if d:
                detail["length"] = int(d.group(1))
            put("format", **detail)
            return
    m = re.search(r"(?:format|pattern|like|as)\s*[:\-]?\s*['\"]?((?:yyyy|yy|dd|mm)[-/.](?:mm|dd)[-/.](?:yyyy|yy|dd))", low)
    if m:
        put("date_format", date_format=m.group(1).upper())
        return
    m = re.search(r"between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)", low)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if re.search(r"size|length|characters|chars|letters|digits|long", low):
            put("min_length", n=int(lo))
            put("max_length", n=int(hi))
        else:
            put("min", n=lo, strict=False)
            put("max", n=hi)
        return
    if re.search(r"\bpositive\b", low):
        put("min", n=0.0, strict=True)
    elif re.search(r"non[- ]?negative|zero\s+or\s+more", low):
        put("min", n=0.0, strict=False)
    m = re.search(r"(?:valid|be\s+(?:a|an)|of\s+type|expected|is\s+not\s+(?:a|an)|should\s+be\s+(?:a|an)?|must\s+be\s+(?:a|an)?|to\s+be\s+(?:a|an)?)\s*(?:valid\s+)?(?:positive\s+|negative\s+|non[- ]?negative\s+)?(whole\s+number|number|numeric|integer|int|long|float|decimal|double|string|text|boolean|bool|array|list|object|dict)\b", low)
    if m:
        put("type", type=_TYPES[m.group(1)])
        return
    if found:
        return
    if re.search(r"\b(required|is\s+missing|missing|must\s+not\s+be\s+(?:null|empty|blank)|cannot\s+be\s+(?:null|empty|blank)|can't\s+be\s+(?:blank|empty)|may\s+not\s+be\s+(?:null|empty|blank)|not\s+be\s+(?:empty|blank|null)|mandatory|field\s+required)\b", low):
        put("missing")
        return
    m = re.search(r"(?:at\s+least|minimum(?:\s+of)?|min(?:imum)?\s+length\s+(?:of|is)?|no\s+(?:fewer|less)\s+than|longer\s+than)\s+(\d+)\s*(characters|chars|digits|letters|symbols)", low)
    if m:
        put("min_length", n=int(m.group(1)))
        return
    m = re.search(r"(?:at\s+most|maximum(?:\s+of)?|max(?:imum)?\s+length\s+(?:of|is)?|no\s+more\s+than|shorter\s+than|up\s+to)\s+(\d+)\s*(characters|chars|digits|letters|symbols)", low)
    if m:
        put("max_length", n=int(m.group(1)))
        return
    m = re.search(r"(?:exactly\s+|be\s+|have\s+|of\s+)(\d+)\s*(?:characters|chars|digits|letters)", low)
    if m:
        put("length", n=int(m.group(1)))
        return
    m = re.search(r"(?:greater\s+than(?:\s+or\s+equal(?:\s+to)?)?|more\s+than|at\s+least|minimum(?:\s+(?:is|of))?|>=?|above|not\s+(?:be\s+)?less\s+than|min(?:imum)?\s+value(?:\s+is)?)\s*(-?\d+(?:\.\d+)?)", low)
    if m:
        strict = bool(re.search(r"greater\s+than(?!\s+or)|more\s+than|above|>(?!=)", m.group(0)))
        put("min", n=float(m.group(1)), strict=strict)
        return
    m = re.search(r"(?:less\s+than(?:\s+or\s+equal(?:\s+to)?)?|at\s+most|maximum(?:\s+(?:is|of))?|<=?|below|not\s+(?:be\s+)?(?:more|greater)\s+than|max(?:imum)?\s+value(?:\s+is)?|exceed)\s*(-?\d+(?:\.\d+)?)", low)
    if m:
        put("max", n=float(m.group(1)))
        return
    if re.search(r"already\s+(?:exists|registered|taken|in\s+use|used)|duplicate|must\s+be\s+unique|not\s+unique|has\s+already\s+been\s+taken", low):
        put("duplicate")
        return
    if re.search(r"not\s+allowed|unknown\s+(?:field|property|key)|unrecognized|unexpected\s+(?:field|property|key)|additional\s+propert", low):
        put("unknown")
        return
    if re.search(r"\b(invalid|not\s+valid|malformed|incorrect|bad\s+format|wrong\s+format|does\s+not\s+match)\b", low):
        put("format")


def _dedupe(issues: list) -> list:
    placed = {i.field.lower() for i in issues if i.location in ("header", "query", "path")}
    seen, out = set(), []
    for i in issues:
        if not i.location and i.field.lower() in placed:
            continue
        if i.key() not in seen:
            seen.add(i.key())
            out.append(i)
    return out


# ----------------------------------------------------------------------------- structured responses

_LIST_KEYS = ("errors", "details", "detail", "violations", "fieldErrors", "field_errors", "invalid-params",
              "invalidParams", "invalid_params", "validationErrors", "validation_errors", "issues", "problems",
              "messages", "subErrors", "fields")
_FIELD_KEYS = ("field", "param", "parameter", "path", "loc", "property", "propertyPath", "property_path", "name",
               "key", "pointer", "attribute", "fieldName", "field_name", "target", "location_name", "dataPath",
               "instancePath")
_MSG_KEYS = ("message", "msg", "defaultMessage", "detail", "reason", "description", "error", "title", "errorMessage",
             "error_description", "text", "summary", "code")


def _field_of(obj: dict) -> tuple:
    loc = ""
    for k in _FIELD_KEYS:
        if k in obj and obj[k] not in (None, "", []):
            v = obj[k]
            if isinstance(v, list):                              # pydantic: ["body", "accountType"]
                parts = [str(p) for p in v]
                if parts and parts[0] in ("body", "query", "header", "path", "cookie"):
                    loc = parts[0]
                    parts = parts[1:]
                return ".".join(p for p in parts if not p.isdigit()) or "", loc
            if isinstance(v, str):
                if k in ("location_name",):
                    continue
                f, l2 = _clean_field(v)
                return f, l2 or loc
    src = obj.get("source")
    if isinstance(src, dict):
        for k in ("pointer", "parameter", "header"):
            if src.get(k):
                f, l2 = _clean_field(str(src[k]))
                return f, ("query" if k == "parameter" else "header" if k == "header" else l2)
    location = obj.get("location") or obj.get("in")
    return "", (location if isinstance(location, str) and location in ("body", "query", "header", "path") else loc)


def _msg_of(obj: dict) -> str:
    parts = []
    for k in _MSG_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " - ".join(dict.fromkeys(parts))


def _pydantic(obj: dict, f: str, loc: str) -> list:
    """FastAPI / Pydantic v1+v2 error entries carry a machine-readable 'type' and 'ctx'."""
    t = str(obj.get("type") or "")
    ctx = obj.get("ctx") if isinstance(obj.get("ctx"), dict) else {}
    msg = str(obj.get("msg") or "")
    if not t or not f:
        return []
    if t in ("missing", "value_error.missing"):
        return [Issue("missing", f, loc, {}, msg)]
    if t in ("enum", "literal_error", "type_error.enum") or "permitted" in ctx or "expected" in ctx and " or " in str(ctx.get("expected")):
        allowed = _allowed_values(str(ctx.get("expected") or ctx.get("permitted") or ctx.get("enum_values") or msg))
        if allowed:
            return [Issue("enum", f, loc, {"allowed": allowed}, msg)]
    m = re.match(r"(int|float|bool|string|str|list|dict|decimal)_(?:parsing|type)|type_error\.(integer|float|bool|str|list|dict|number)", t)
    if m:
        name = (m.group(1) or m.group(2))
        return [Issue("type", f, loc, {"type": {"int": "integer", "integer": "integer", "float": "number", "decimal": "number",
                                                "number": "number", "bool": "boolean", "str": "string", "string": "string",
                                                "list": "array", "dict": "object"}[name]}, msg)]
    if t in ("string_too_short", "too_short", "value_error.any_str.min_length"):
        return [Issue("min_length", f, loc, {"n": int(ctx.get("min_length") or ctx.get("limit_value") or 1)}, msg)]
    if t in ("string_too_long", "too_long", "value_error.any_str.max_length"):
        return [Issue("max_length", f, loc, {"n": int(ctx.get("max_length") or ctx.get("limit_value") or 1)}, msg)]
    for key, kind, strict in (("gt", "min", True), ("ge", "min", False), ("lt", "max", True), ("le", "max", False)):
        if key in ctx and t.startswith(("greater", "less", "value_error.number")):
            return [Issue(kind, f, loc, {"n": float(ctx[key]), "strict": strict}, msg)]
    if t in ("extra_forbidden", "value_error.extra"):
        return [Issue("unknown", f, loc, {}, msg)]
    return []


def parse(status: int | None, text: str, json_value=None, headers: dict | None = None) -> list:
    """All issues an error response tells us about."""
    issues: list = []
    data = json_value
    if data is None and text and text.lstrip()[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    seen_structured = False

    def walk_list(items, default_loc=""):
        nonlocal seen_structured
        for it in items:
            if isinstance(it, dict):
                f, loc = _field_of(it)
                found = _pydantic(it, f, loc or default_loc)
                if not found:
                    found = classify(_msg_of(it), f, loc or default_loc) if f else classify(_msg_of(it))
                if found:
                    seen_structured = True
                issues.extend(found)
            elif isinstance(it, str):
                issues.extend(classify(it))

    def walk(obj, depth=0):
        nonlocal seen_structured
        if depth > 3:
            return
        if isinstance(obj, list):
            walk_list(obj)
            return
        if not isinstance(obj, dict):
            return
        for k in _LIST_KEYS:
            v = obj.get(k)
            if isinstance(v, list):
                walk_list(v)
            elif isinstance(v, dict) and v:
                # {"errors": {"email": ["is invalid"], "Name": ["The Name field is required."]}}
                for fk, fv in v.items():
                    msgs = fv if isinstance(fv, list) else [fv]
                    for mm in msgs:
                        if isinstance(mm, str):
                            f, loc = _clean_field(fk)
                            found = classify(mm, f, loc)
                            if found:
                                seen_structured = True
                            issues.extend(found)
                        elif isinstance(mm, dict):
                            walk_list([{**mm, "field": mm.get("field") or fk}])
        for k in ("error", "fault", "data", "result", "response"):
            if isinstance(obj.get(k), dict):
                walk(obj[k], depth + 1)
        # Rails style at the top level: {"email": ["has already been taken"]}
        if depth == 0 and obj and all(isinstance(v, list) and v and all(isinstance(x, str) for x in v) for v in obj.values()):
            for fk, msgs in obj.items():
                for mm in msgs:
                    issues.extend(classify(mm, *_clean_field(fk)))
        for k in ("message", "error", "error_description", "detail", "title", "msg", "errorMessage", "description",
                  "faultstring", "reason", "error_message"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                issues.extend(classify(v))

    if isinstance(data, (dict, list)):
        walk(data)
    elif text:
        issues.extend(classify(text[:2000]))

    if status in (401, 403) and not any(i.kind == "auth" for i in issues):
        issues.append(Issue("auth", "", "", {"www": (headers or {}).get("WWW-Authenticate") or _hget(headers, "www-authenticate")},
                            _first_message(data, text)))
    if status == 405:
        issues.append(Issue("method", "", "", {"allow": _hget(headers, "allow")}, _first_message(data, text)))
    if status == 415 and not any(i.kind == "content_type" for i in issues):
        issues.append(Issue("content_type", "", "", {"expected": _hget(headers, "accept-post") or _hget(headers, "accept")},
                            _first_message(data, text)))
    if status == 406:
        issues.append(Issue("accept", "", "", {}, _first_message(data, text)))
    if status == 409 and not any(i.kind == "duplicate" for i in issues):
        issues.append(Issue("duplicate", "", "", {}, _first_message(data, text)))
    return _dedupe(issues)


def _hget(headers: dict | None, name: str):
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return None


def _first_message(data, text) -> str:
    if isinstance(data, dict):
        for k in ("message", "error_description", "detail", "error", "title", "msg"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return (text or "").strip()[:200]


def first_message(data, text) -> str:
    return _first_message(data, text)


_RUNTIME = (r"(?:NullPointer|ArrayIndexOutOfBounds|StringIndexOutOfBounds|IndexOutOfBounds|ClassCast|Arithmetic|"
            r"NumberFormat|ConcurrentModification|NoSuchElement|UnsupportedOperation|NullReference|InvalidCast|"
            r"IndexOutOfRange|InvalidOperation|DivideByZero|ArgumentNull|KeyNotFound)Exception|StackOverflowError|"
            r"OutOfMemoryError|(?:Type|Key|Attribute|ZeroDivision|Index|Name|UnboundLocal|Recursion|Reference|NoMethod)Error")
_CRASH = [re.compile(x) for x in (       # runtime failures that no API reports on purpose; most telling first
    r"\b(" + _RUNTIME + r")\b",
    r"(Traceback \(most recent call last\))",
    r"(Object reference not set to an instance of an object)",
    r"(Cannot read propert(?:y|ies) of (?:undefined|null))",
    r"(undefined method \S+ for nil)",
    r"(Segmentation fault|panic: runtime error|goroutine \d+ \[running\])",
    r"(SQLSTATE\[\w+\]|ORA-\d{5})",
    r"\b(at [\w$.]+\([\w$]+\.(?:java|kt|scala|cs):\d+\))",
    r"(File \"[^\"]+\.py\", line \d+)",
)]


def looks_like_crash(text: str | None) -> str | None:
    """The API crashed but answered 4xx: a runtime exception or a stack trace leaked into the response.
    That is a server bug, never something to heal. Returns the telltale words, or None.
    Errors an API reports on purpose ('ValidationError', 'amount must be positive') never match."""
    for rx in _CRASH:
        m = rx.search(text or "")
        if m:
            return m.group(1)
    return None
