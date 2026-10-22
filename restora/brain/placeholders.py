"""Find the gaps in a request: values a tester left for later.

Explicit references (always understood, brain on or off):
    {{Login.access_token}}   a value from an earlier row's response   (row name + JSON path)
    {{token}}                the most recent value called 'token' from any earlier response
    {{$email}}               fresh test data (see generate.py for the list)
    ${token}                 same as {{token}}

Implicit gaps (understood by the brain):
    <token>  <customer id>   angle-bracket notes, as written in API documents
    /customers/{id}  /:id    path parameters (OpenAPI / Express style)
    TBD  ???  XXXX  CHANGE_ME  your-api-key   placeholder words typed as a whole value
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import jsonpath
from .names import resource_before, resource_of_path, words

REF_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
DOLLAR_RE = re.compile(r"\$\{\s*([A-Za-z_$][^{}]{0,80}?)\s*\}")
ANGLE_RE = re.compile(r"<\s*([A-Za-z_][A-Za-z0-9_ .\-]{0,48}?)\s*>")
PATH_BRACE_RE = re.compile(r"(?<!\{)\{\s*([A-Za-z_][\w\-]*)\s*\}(?!\})")
PATH_COLON_RE = re.compile(r"(?<=/):([A-Za-z_][\w\-]*)")
WORD_RE = re.compile(
    r"^\s*(tbd|tba|todo|to\s*do|\?{1,}|x{3,}|changeme|change[_ -]me|replace[_ -]?me|fill[_ -]?(?:me|in|this)?"
    r"|your[_ -][\w -]{1,40}|\.{3}|…|placeholder|insert[_ -]here|<\s*fill\s*>)\s*$", re.I)
_HTML_TAGS = {"p", "b", "i", "u", "a", "br", "hr", "div", "span", "ul", "ol", "li", "html", "body", "head", "strong",
              "em", "h1", "h2", "h3", "h4", "h5", "h6", "img", "table", "tr", "td", "th", "script", "style", "xml"}
_SCHEMES = re.compile(r"^(bearer|basic|token|apikey|api-key|digest)\s+", re.I)


@dataclass
class Placeholder:
    raw: str                    # exactly as typed:  {{Login.token}}  <token>  TBD  {id}  :id
    name: str                   # what it stands for: 'Login.token', 'token', 'phone', 'id'
    kind: str                   # ref | dollar | angle | path | word
    where: str                  # url | query | header | body
    key: str = ""               # the header / query parameter / JSON key holding it
    path: list = field(default_factory=list)    # JSON path inside the body
    whole: bool = True          # it is the entire value (so the value's type may change)
    resource: str = ""          # collection next to it: 'customer' for /customers/{id}

    @property
    def explicit(self) -> bool:
        return self.kind in ("ref", "dollar")

    @property
    def label(self) -> str:
        return self.raw if self.kind != "word" else f"{self.key or self.name} = {self.raw.strip()}"


def is_placeholder_word(value) -> bool:
    return isinstance(value, str) and bool(WORD_RE.match(value)) and len(value) <= 60


def _angle_ok(name: str, text: str, start: int) -> bool:
    n = name.strip().lower()
    if n in _HTML_TAGS or re.match(r"^/?[a-z]+:", n):
        return False
    return f"</{n}" not in text[start:].lower()


def _clean_name(raw_name: str) -> str:
    w = words(raw_name)
    return " ".join(w) if w else raw_name.strip()


def scan_text(text: str, where: str, key: str = "", implicit: bool = True, angle: bool = True,
              resource: str = "") -> list:
    """Placeholders inside a piece of text (a header value, a query value, a raw body)."""
    if not isinstance(text, str) or not text:
        return []
    found = []
    for m in REF_RE.finditer(text):
        found.append(Placeholder(m.group(0), m.group(1).strip(), "ref", where, key, whole=m.group(0) == text.strip(),
                                 resource=resource))
    for m in DOLLAR_RE.finditer(text):
        found.append(Placeholder(m.group(0), m.group(1).strip(), "dollar", where, key,
                                 whole=m.group(0) == text.strip(), resource=resource))
    if not implicit:
        return found
    if angle:
        for m in ANGLE_RE.finditer(text):
            if _angle_ok(m.group(1), text, m.end()):
                found.append(Placeholder(m.group(0), _clean_name(m.group(1)), "angle", where, key,
                                         whole=m.group(0) == text.strip(), resource=resource))
    if not found:
        rest = _SCHEMES.sub("", text) if where == "header" else text
        if is_placeholder_word(rest):
            name = key
            if where == "header" and key.lower() in ("authorization", "proxy-authorization"):
                name = "token"
            found.append(Placeholder(rest.strip() if rest != text else text, _clean_name(name) or "value", "word",
                                     where, key, whole=rest == text, resource=resource))
    return found


def scan_url(url: str, implicit: bool = True) -> list:
    if not url:
        return []
    found = []
    try:
        parts = urlsplit(url)
        path, query = parts.path, parts.query
    except ValueError:
        path, query = url, ""
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", url, re.I):      # relative URL: everything before ? is the path
        path, _, query = url.partition("?")
    for p in scan_text(path, "url", implicit=implicit):
        p.resource = resource_before(path, p.raw)
        found.append(p)
    if implicit:
        seen = {p.raw for p in found}
        for rx in (PATH_BRACE_RE, PATH_COLON_RE):
            for m in rx.finditer(path):
                if m.group(0) in seen or "{{" in path[max(0, m.start() - 1):m.end() + 1]:
                    continue
                found.append(Placeholder(m.group(0), _clean_name(m.group(1)), "path", "url",
                                         resource=resource_before(path, m.group(0))))
                seen.add(m.group(0))
    for pair in filter(None, query.split("&")):
        k, _, v = pair.partition("=")
        for p in scan_text(v, "query", key=k, implicit=implicit, resource=resource_of_path(path)):
            if p.kind == "word" and not p.name:
                p.name = _clean_name(k)
            found.append(p)
    return found


def scan_headers(headers: dict, implicit: bool = True) -> list:
    found = []
    for k, v in (headers or {}).items():
        found += scan_text(str(v), "header", key=str(k), implicit=implicit)
    return found


def scan_json(obj, implicit: bool = True) -> list:
    found = []
    for parts, key, value in jsonpath.leaves(obj):
        if not isinstance(value, str):
            continue
        k = str(key) if key not in (None, "") else ""
        for p in scan_text(value, "body", key=k, implicit=implicit):
            p.path = parts
            if p.kind == "word":
                p.name = _clean_name(k) or "value"
            found.append(p)
    return found


def scan_body_text(body: str, implicit: bool = True) -> list:
    """For bodies that are not JSON (form data, XML, text): only explicit references and angle notes in forms."""
    if not body:
        return []
    if body.lstrip().startswith("<"):                               # XML / SOAP: angle brackets are real markup
        return scan_text(body, "body", implicit=False)
    if "=" in body and "&" in body or re.match(r"^[\w.\-\[\]]+=", body.strip()):
        found = []
        for pair in body.split("&"):
            k, _, v = pair.partition("=")
            found += scan_text(v, "body", key=k, implicit=implicit)
        return found
    return scan_text(body, "body", implicit=implicit)
