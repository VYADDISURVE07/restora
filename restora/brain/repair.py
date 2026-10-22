"""Fixing what people type: almost-JSON bodies, relative URLs, blank methods.

Everything here is deterministic and explains itself; nothing is guessed silently.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

_SMART = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", " ": " "})
_LITERALS = {"true": "true", "false": "false", "null": "null", "True": "true", "False": "false", "None": "null",
             "undefined": "null", "NaN": "null", "nil": "null", "NULL": "null", "TRUE": "true", "FALSE": "false"}
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+\-]?\d+)?(?![\w.])")
_DESCRIBE = {
    "smart": "curly 'smart' quotes became normal quotes",
    "single": "single quotes became double quotes",
    "comments": "comments were removed",
    "trailing": "a trailing comma was removed",
    "keys": "unquoted keys were quoted",
    "literals": "True/False/None became true/false/null",
    "bare": "unquoted text was put in quotes",
    "comma": "a missing comma was added",
    "close": "missing closing brackets were added",
    "colon": "'=' between a key and value became ':'",
    "strings": "an unfinished string was closed",
}


class RepairFailed(ValueError):
    pass


def repair_json(text: str):
    """Returns (value, fixed_text, [what was fixed]) or raises RepairFailed."""
    try:
        return json.loads(text), text, []
    except ValueError:
        pass
    changes: list = []
    t = text.translate(_SMART)
    if t != text:
        changes.append("smart")
        try:
            return json.loads(t), t, [_DESCRIBE["smart"]]
        except ValueError:
            pass
    if t.lstrip()[:1] not in "{[":
        raise RepairFailed("it does not start with { or [")
    fixed, more = _rewrite(t)
    changes += [c for c in more if c not in changes]
    try:
        value = json.loads(fixed)
    except ValueError as e:
        raise RepairFailed(str(e)) from e
    if not isinstance(value, (dict, list)):
        raise RepairFailed("the repaired text is not an object or a list")
    return value, fixed, [_DESCRIBE[c] for c in changes]


def _skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def _read_string(text: str, i: int, quote: str):
    """From an opening quote at i, return (python string, index after closing quote, terminated?)."""
    out, j, n = [], i + 1, len(text)
    while j < n:
        ch = text[j]
        if ch == "\\" and j + 1 < n:
            nxt = text[j + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/", "\\": "\\", '"': '"', "'": "'"}
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[j + 2:j + 6] or ""):
                out.append(chr(int(text[j + 2:j + 6], 16)))
                j += 6
                continue
            out.append(mapping.get(nxt, nxt))
            j += 2
            continue
        if ch == quote:
            return "".join(out), j + 1, True
        if ch == "\n" and quote == "'":
            break
        out.append(ch)
        j += 1
    return "".join(out), j, False


def _rewrite(text: str):
    out: list = []
    changes: list = []
    stack: list = []
    expect = "value"            # value | key | colon | comma | done
    i, n = 0, len(text)

    def note(c):
        if c not in changes:
            changes.append(c)

    def after_value():
        return "comma" if stack else "done"

    while i < n:
        j = _skip_ws(text, i)
        if j != i:
            if text[i:j].strip():
                note("comments")
            out.append(" " if "\n" not in text[i:j] else "\n")
            i = j
            continue
        c = text[i]
        if expect == "done":
            break                                           # ignore trailing garbage after a complete value
        if expect == "comma":
            if c == ",":
                k = _skip_ws(text, i + 1)
                if k < n and text[k] in "}]":
                    note("trailing")
                    i += 1
                    continue
                out.append(",")
                i += 1
                expect = "key" if stack[-1] == "{" else "value"
                continue
            if c in "}]":
                opener = stack.pop()
                out.append("}" if opener == "{" else "]")
                i += 1
                expect = after_value()
                continue
            note("comma")                                   # two values with no comma between them
            out.append(",")
            expect = "key" if stack[-1] == "{" else "value"
            continue
        if expect == "colon":
            if c == ":":
                out.append(":")
                i += 1
                expect = "value"
                continue
            if c == "=":
                note("colon")
                out.append(":")
                i += 1
                expect = "value"
                continue
            if c in ",}":                                   # key with no value
                out.append(":null")
                expect = "comma"
                continue
            out.append(":")
            expect = "value"
            continue
        if c in "}]":
            if expect == "key" or expect == "value":
                if out and out[-1].strip() == ",":
                    out.pop()
                    note("trailing")
            if stack:
                opener = stack.pop()
                out.append("}" if opener == "{" else "]")
            i += 1
            expect = after_value()
            continue
        if c in "{[" and not (expect == "value" and text.startswith("{{", i)):
            stack.append(c)
            out.append(c)
            i += 1
            expect = "key" if c == "{" else "value"
            continue
        if c == ",":                                        # e.g. [1,,2] or {,"a":1}
            i += 1
            continue
        if c in "\"'":
            s, i, ok = _read_string(text, i, c)
            if c == "'":
                note("single")
            if not ok:
                note("strings")
            out.append(json.dumps(s, ensure_ascii=False))
            expect = "colon" if expect == "key" else after_value()
            continue
        if expect == "key":
            m = re.match(r"[A-Za-z_$@][\w$@\-.]*|\d+", text[i:])
            if m:
                note("keys")
                out.append(json.dumps(m.group(0)))
                i += m.end()
                expect = "colon"
                continue
            raise RepairFailed(f"unexpected '{c}' where a key was expected")
        # ---- a value
        m = _NUMBER.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            expect = after_value()
            continue
        if text.startswith("{{", i):
            k = text.find("}}", i)
            k = n if k < 0 else k + 2
            note("bare")
            out.append(json.dumps(text[i:k].strip()))
            i = k
            expect = after_value()
            continue
        if c == "<":
            k = text.find(">", i)
            if k > i:
                note("bare")
                out.append(json.dumps(text[i:k + 1]))
                i = k + 1
                expect = after_value()
                continue
        m = re.match(r"[A-Za-z_$][\w$]*", text[i:])
        if m and m.group(0) in _LITERALS and not re.match(r"[ \t]*[\w\-]", text[i + m.end():]):
            if m.group(0) not in ("true", "false", "null"):
                note("literals")
            out.append(_LITERALS[m.group(0)])
            i += m.end()
            expect = after_value()
            continue
        # bare text: read up to the next structural character
        k = i
        while k < n and text[k] not in ",}]\n":
            k += 1
        word = text[i:k].strip()
        if not word:
            raise RepairFailed(f"unexpected '{c}'")
        note("bare")
        out.append(json.dumps(word, ensure_ascii=False))
        i = k
        expect = after_value()
    if expect == "colon":
        out.append(":null")
    if stack:
        note("close")
        while stack:
            out.append("}" if stack.pop() == "{" else "]")
    return "".join(out).strip(), changes


# --------------------------------------------------------------------------- methods and URLs

_VERBS = [
    ("DELETE", r"delete|remove|cancel|close|deactivate|revoke|unsubscribe|purge|destroy|terminate"),
    ("PATCH", r"patch|partially\s+update|partial\s+update"),
    ("PUT", r"update|edit|modify|change|replace|rename|amend"),
    ("POST", r"create|add|new|register|sign\s?up|signup|submit|post|send|upload|log\s?in|login|sign\s?in|authenticate|"
             r"transfer|pay|apply|book|place|open|generate|verify|validate|initiate|subscribe|import|reset|issue|"
             r"approve|reject|calculate|convert"),
    ("GET", r"get|fetch|list|read|view|show|check|search|find|retrieve|download|look\s?up|lookup|status|health|"
            r"ping|details?|history|statement|balance|profile|summary|export"),
]


def infer_method(name: str, has_body: bool) -> tuple:
    """('POST', "the name starts with 'Create'") from the row name; the first verb in the name wins."""
    best = None
    for method, pattern in _VERBS:
        m = re.search(rf"\b({pattern})\b", name or "", re.I)
        if m and (best is None or m.start() < best[1]):
            best = (method, m.start(), m.group(1))
    if best is None:
        return ("POST", "there is a body") if has_body else ("GET", "there is no body")
    method, _, word = best
    if method == "GET" and has_body:
        return "POST", f"there is a body (and the name says '{word}')"
    return method, f"the name says '{word}'"


def looks_relative(url: str) -> bool:
    return bool(url) and not re.match(r"^[a-z][a-z0-9+.\-]*://", url, re.I)


def fix_url(url: str, base: str | None) -> tuple:
    """(fixed_url, what_was_done or None). base: an absolute URL to resolve relative paths against."""
    u = (url or "").strip().strip("`'\"").strip()
    notes = []
    if u.startswith("<") and u.endswith(">") and "://" in u:
        u = u[1:-1]
    if re.match(r"^https?:\\\\", u, re.I):
        u = re.sub(r"\\", "/", u)
        notes.append("backslashes became slashes")
    if re.match(r"^https?:/[^/]", u, re.I):
        u = re.sub(r"^(https?:)/", r"\1//", u, flags=re.I)
        notes.append("a missing '/' after http: was added")
    if looks_relative(u):
        if re.match(r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|\d{1,3}(\.\d{1,3}){3})(:\d+)?(/|$|\?)", u, re.I):
            u = "http://" + u
            notes.append("'http://' was added")
        elif re.match(r"^[\w\-]+(\.[\w\-]+)*\.[a-z]{2,}(:\d+)?(/|$|\?)", u, re.I) and not u.startswith("/"):
            u = "https://" + u
            notes.append("'https://' was added")
        elif base:
            joined = urljoin(base, u)
            notes.append(f"it was relative, so it was joined to {origin(base)}")
            u = joined
    return u, ("; ".join(notes) if notes else None)


def origin(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"
    except ValueError:
        return url
