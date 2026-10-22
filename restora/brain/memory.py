"""What Restora has learned during a run: every value an API returned, and every value it sent.

This is what lets a later row say <token> or {customerId} and get the real value from an earlier response,
the way a tester copies the token from the Login response into the next request by hand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import jsonpath
from .names import is_id_name, is_token_name, norm, resource_of_path, similarity, words

THRESHOLD = 0.6


@dataclass
class Entry:
    row_number: int
    row_name: str
    source: str              # response | header | request | generated
    parts: list
    key: str
    value: Any
    method: str
    path: str
    order: int

    @property
    def where(self) -> str:
        if self.source == "header":
            return f"header {self.key}"
        return jsonpath.fmt(self.parts)


@dataclass
class Match:
    value: Any
    entry: Entry | None
    score: float
    reason: str
    ref: str = ""            # an explicit reference that reproduces this match, e.g. {{Login.access_token}}


def row_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _url_path(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError:
        return url


@dataclass
class Memory:
    entries: list = field(default_factory=list)
    responses: dict = field(default_factory=dict)     # row number -> {"json":..., "status":..., "headers":...}
    names: dict = field(default_factory=dict)         # row_key(name) -> row number
    row_names: dict = field(default_factory=dict)     # row number -> display name
    generated: dict = field(default_factory=dict)     # str(value) -> (row number, field name)
    _order: int = 0

    # ------------------------------------------------------------------ learning
    def _add(self, row, source, parts, key, value, method, url):
        if value is None or isinstance(value, (dict, list)):
            return
        self._order += 1
        self.entries.append(Entry(row.number, row.name, source, list(parts), str(key), value, method,
                                  _url_path(url), self._order))

    def remember_row(self, row) -> None:
        self.names.setdefault(row_key(row.name), row.number)
        self.row_names[row.number] = row.name

    def learn_response(self, row, method: str, url: str, status: int | None, json_value: Any, headers: dict) -> None:
        self.remember_row(row)
        self.responses[row.number] = {"json": json_value, "status": status, "headers": dict(headers or {})}
        if status is None or not 200 <= status < 300:
            return
        if isinstance(json_value, (dict, list)):
            for parts, key, value in jsonpath.leaves(json_value):
                self._add(row, "response", parts, key, value, method, url)
        elif isinstance(json_value, (str, int, float)) and not isinstance(json_value, bool):
            self._add(row, "response", [], "value", json_value, method, url)
        for hk, hv in (headers or {}).items():
            if hk.lower() == "location" and hv:
                self._add(row, "header", [hk], "Location", hv, method, url)
                last = [s for s in _url_path(hv).split("/") if s]
                if last:
                    res = resource_of_path(_url_path(hv)) or "item"
                    self._add(row, "header", [hk], f"{res}Id", last[-1], method, url)
                    self._add(row, "header", [hk], "id", last[-1], method, url)
            elif hk.lower() in ("x-request-id", "etag", "x-correlation-id"):
                self._add(row, "header", [hk], hk, hv, method, url)

    def learn_request(self, row, method: str, url: str, body_obj: Any, generated: dict | None = None) -> None:
        self.remember_row(row)
        if isinstance(body_obj, (dict, list)):
            for parts, key, value in jsonpath.leaves(body_obj):
                self._add(row, "request", parts, key, value, method, url)
        for fname, value in (generated or {}).items():
            self.generated[str(value)] = (row.number, fname)

    def was_generated(self, value) -> tuple | None:
        return self.generated.get(str(value)) if value is not None else None

    # ------------------------------------------------------------------ explicit references
    def resolve_ref(self, text: str, current_row: int | None = None) -> Match | None:
        """{{Login.access_token}} / {{#3.id}} / {{Login.status}} / {{Login.headers.Location}}"""
        text = text.strip()
        candidates = []
        if text.startswith("#"):
            m = re.match(r"#(\d+)\.?(.*)$", text)
            if m:
                candidates.append((int(m.group(1)), m.group(2)))
        for i, ch in enumerate(text):
            if ch == ".":
                num = self.names.get(row_key(text[:i]))
                if num is not None:
                    candidates.append((num, text[i + 1:]))
        for num, path in sorted(candidates, key=lambda c: -len(str(c[1]))):
            if current_row is not None and num == current_row:
                continue
            resp = self.responses.get(num)
            if resp is None:
                continue
            p = path.strip()
            name = self.row_names.get(num, f"row {num}")
            if p.lower() == "status":
                return Match(resp["status"], None, 1.0, f"the HTTP status of '{name}'", "{{" + text + "}}")
            if p.lower().startswith(("headers.", "header.")):
                hname = p.split(".", 1)[1]
                for hk, hv in resp["headers"].items():
                    if hk.lower() == hname.lower():
                        return Match(hv, None, 1.0, f"the {hk} header of '{name}'", "{{" + text + "}}")
                return None
            p = re.sub(r"^(response\.)?(body\.)?", "", p, flags=re.I)
            value = jsonpath.get(resp["json"], jsonpath.parse(p)) if p else resp["json"]
            if value is not None:
                return Match(value, None, 1.0, f"'{name}' response {jsonpath.fmt(jsonpath.parse(p))}",
                             "{{" + text + "}}")
        return None

    # ------------------------------------------------------------------ inference
    def lookup(self, name: str, resource: str = "", exclude_row: int | None = None, exact_only: bool = False,
               allow_request: bool = True, only_row: int | None = None) -> Match | None:
        """Best earlier value for a gap called `name` (e.g. 'token', 'customer id'), or None.
        only_row: look only at what that row returned (the producer the planner picked)."""
        if not self.entries:
            return None
        want_token = is_token_name(name)
        want_id = is_id_name(name) or norm(name) == "id"
        max_order = self._order or 1
        best: tuple | None = None
        for e in self.entries:
            if exclude_row is not None and e.row_number == exclude_row:
                continue
            if only_row is not None and e.row_number != only_row:
                continue
            if e.source == "request" and not allow_request:
                continue
            if exact_only:
                score = 1.0 if norm(e.key) == norm(name) else 0.0
            else:
                tail = " ".join(str(p) for p in e.parts[-2:] if isinstance(p, str))
                score = max(similarity(name, e.key), similarity(name, tail) * 0.95 if tail else 0.0)
                if want_token:
                    if not (is_token_name(e.key) and isinstance(e.value, str) and len(e.value) >= 8):
                        continue
                    score = max(score, 0.75)
                elif is_token_name(e.key) and not want_token:
                    continue
                if want_id:
                    if isinstance(e.value, bool) or not isinstance(e.value, (str, int)):
                        continue
                    if resource and resource_of_path(e.path) == resource and norm(e.key) in ("id", resource + "id"):
                        score += 0.25
                    elif resource and norm(e.key) == "id" and resource_of_path(e.path) != resource:
                        score -= 0.2
                    if e.method == "POST" and e.source in ("response", "header"):
                        score += 0.05
            if e.source == "request":
                score *= 0.9
            if score < THRESHOLD:
                continue
            score += 0.03 * (e.order / max_order)                 # prefer the most recent value
            if best is None or score > best[0]:
                best = (score, e)
        if best is None:
            return None
        score, e = best
        where = e.where[2:] if e.where.startswith("$.") else e.where
        if e.source == "request":
            reason = f"sent earlier by '{e.row_name}' ({where})"
        else:
            reason = f"from '{e.row_name}' ({where})"
        ref = ""
        if e.source in ("response", "header"):
            path = e.where.lstrip("$.") if e.source == "response" else f"headers.{e.key}"
            if e.source == "header" and e.key not in ("Location",):
                path = "headers.Location" if e.key.endswith("Id") or e.key == "id" else f"headers.{e.key}"
                ref = ""          # derived ids from Location cannot be referenced exactly; keep value-only
            else:
                ref = "{{" + f"{e.row_name}.{path}" + "}}"
        return Match(e.value, e, min(score, 1.0), reason, ref)

    def candidates(self, name: str, limit: int = 5) -> list:
        """Top possible matches (for the AI to choose from when the rules are unsure)."""
        scored = []
        for e in self.entries:
            s = similarity(name, e.key)
            if s > 0.3:
                scored.append((s, e))
        scored.sort(key=lambda t: (-t[0], -t[1].order))
        return [e for _, e in scored[:limit]]

    def token_like(self) -> Match | None:
        return self.lookup("token")

    def snapshot(self) -> dict:
        """Compact view for reports / the AI: last value per key."""
        out = {}
        for e in self.entries:
            if e.source in ("response", "header"):
                out[f"{e.row_name} {e.where}"] = e.value
        return out


def words_of(name: str) -> tuple:
    return words(name)
