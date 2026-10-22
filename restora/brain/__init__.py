"""Restora's brain: fills gaps, fixes the order, heals rejected requests and explains failures.

    plan()     before the run: tidy rows, find what each row needs, order rows by producer -> consumer
    fill()     just before a row is sent: put real values into <token>, {id}, TBD, {{Login.token}} ...
    heal()     when the API rejects a row that should succeed: read the error, patch, retry
    learn()    after every response: remember the values for later rows
    explain()  for every failure: one or two plain sentences

Rules come first (fast, free, repeatable). The local AI model is only asked when the rules have no answer,
and everything it suggests is checked before use. Brain mode 'off' keeps only the explicit {{...}} references.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from . import errors as _errors
from . import guard as _guard
from . import jsonpath
from .explain import explain as _explain
from .generate import Generator, is_secret_field, kind_for
from .heal import HealContext, Patch, apply, diagnose, diagnose_setup
from .memory import Memory
from .names import add_synonyms, core, is_id_name, is_token_name, norm
from .placeholders import Placeholder, scan_body_text, scan_headers, scan_json, scan_url
from .planner import Insight, Plan, plan as _plan
from .volatile import looks_volatile, why_dynamic

_LOOKUP_VERBS = re.compile(r"\b(get|find|search|fetch|check|verify|view|show|read|look\s?up|update|edit|delete|remove)\b", re.I)


@dataclass
class Fill:
    placeholder: Placeholder
    value: Any
    source: str               # ref | memory | generated | sample | ai
    reason: str
    ref: str = ""             # what the corrected sheet should contain instead of the literal value

    def note(self) -> str:
        shown = self.value if not isinstance(self.value, str) or len(self.value) <= 40 else self.value[:37] + "..."
        if is_token_name(self.placeholder.name) or (self.placeholder.where == "header" and self.placeholder.key.lower() == "authorization"):
            shown = "****"
        return f"{self.placeholder.label} -> {json.dumps(shown, ensure_ascii=False) if not isinstance(shown, str) else shown} ({self.reason})"


@dataclass
class FillResult:
    prepared: Any
    fills: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)      # (label, why)


@dataclass
class HealAttempt:
    patch: Patch
    status_before: int | None
    status_after: int | None


@dataclass
class HealResult:
    prepared: Any
    outcome: Any
    attempts: list = field(default_factory=list)
    healed: bool = False


class Brain:
    def __init__(self, settings, ai=None, plugins=None):
        self.settings = settings
        self.mode = settings.brain.mode                 # ai | rules | off
        self.memory = Memory()
        self.generator = Generator(seed=settings.brain.seed, overrides=settings.generators)
        self.plugins = plugins
        if plugins is not None:
            self.generator.custom.update(plugins.generators)
        self.ai = ai if self.mode == "ai" else None
        add_synonyms(settings.synonyms)
        self.insights: list = []

    @property
    def on(self) -> bool:
        return self.mode != "off"

    # ------------------------------------------------------------------ before the run
    def plan(self, rows: list) -> Plan:
        p = _plan(rows, self.settings, explicit_only=not self.on)
        self.insights = list(p.insights)
        return p

    # ------------------------------------------------------------------ before each request
    def fill(self, prepared, row) -> FillResult:
        res = FillResult(prepared)
        implicit = self.on
        body_data = None
        if prepared.body is not None and prepared.body.lstrip()[:1] in "{[":
            try:
                body_data = json.loads(prepared.body)
            except ValueError:
                body_data = None
        found = scan_url(prepared.url, implicit) + scan_headers(prepared.headers, implicit)
        found += scan_json(body_data, implicit) if body_data is not None else scan_body_text(prepared.body or "", implicit)
        if not found:
            return res
        sample = _parse(row.expected_response)
        for ph in found:
            value, source, reason, ref = self._resolve(ph, row, body_data, sample)
            if value is None:
                res.unresolved.append((ph.label, reason))
                continue
            if ph.where == "header" and ph.key.lower() in ("authorization", "proxy-authorization") and ph.whole \
                    and isinstance(value, str) and " " not in value.strip():
                value = "Bearer " + value
                ref = ("Bearer " + ref) if ref else ""
            res.fills.append(Fill(ph, value, source, reason, ref))
            if ph.where == "url" or ph.where == "query":
                prepared.url = prepared.url.replace(ph.raw, quote(str(value), safe="") if ph.where == "url" else quote(str(value), safe=""), 1)
            elif ph.where == "header":
                current = prepared.headers.get(ph.key, "")
                prepared.headers[ph.key] = current.replace(ph.raw, str(value), 1) if not ph.whole else str(value)
            elif ph.where == "body":
                if body_data is not None:
                    if ph.whole:
                        jsonpath.put(body_data, ph.path, value)
                    else:
                        cur = jsonpath.get(body_data, ph.path)
                        jsonpath.put(body_data, ph.path, str(cur).replace(ph.raw, str(value), 1))
                else:
                    text = prepared.body or ""
                    v = str(value)
                    if "=" in text and not text.lstrip().startswith("<"):
                        v = quote(v, safe="")
                    prepared.body = text.replace(ph.raw, v, 1)
        if body_data is not None:
            prepared.body = json.dumps(body_data, ensure_ascii=False)
        return res

    def _resolve(self, ph: Placeholder, row, body_data, sample):
        """(value, source, reason, ref) for one gap; value None means it could not be filled (reason says why)."""
        mem = self.memory
        if ph.explicit:
            name = ph.name
            if name.startswith("$"):
                v = self.generator.by_kind(name)
                if v is None:
                    return None, "", f"'{name}' is not a known test-data name (try $email, $phone, $uuid, $name)", ""
                return v, "generated", f"fresh test data ({name})", ph.raw
            m = mem.resolve_ref(name, row.number)
            if m is not None:
                return m.value, "ref", m.reason, ph.raw
            m = mem.lookup(name, exclude_row=row.number, exact_only=not self.on)
            if m is not None:
                return m.value, "memory", m.reason, ph.raw
            if not self.on:
                return None, "", f"no earlier response has a value called '{name}'", ""
        name = ph.name
        token_like = is_token_name(name) or (ph.where == "header" and ph.key.lower() in ("authorization", "proxy-authorization"))
        if token_like:
            m = mem.lookup("token", exclude_row=row.number)
            if m is not None:
                return m.value, "memory", f"the token {m.reason}", m.ref
            return None, "", "it needs a login token, but no earlier row returned one (put a Login row before it)", ""
        id_like = (ph.kind == "path" or is_id_name(name) or (ph.key and is_id_name(ph.key))
                   or norm(name).endswith("account")) and kind_for(name) != "uuid"
        if id_like:
            planned = (row.brain.get("sources") or {}).get(ph.raw)
            if planned is not None:
                m = mem.lookup(name, resource=ph.resource, only_row=planned, allow_request=False, exact_only=False)
                if m is None and ph.key and ph.key != name:
                    m = mem.lookup(ph.key, resource=ph.resource, only_row=planned, allow_request=False)
                if m is not None:
                    return m.value, "memory", m.reason, m.ref
                pname = mem.row_names.get(planned, f"row {planned}")
                return None, "", f"it should come from '{pname}', which did not succeed", ""
            m = mem.lookup(name, resource=ph.resource, exclude_row=row.number, allow_request=False)
            if m is None and ph.key and ph.key != name:
                m = mem.lookup(ph.key, resource=ph.resource, exclude_row=row.number, allow_request=False)
            if m is not None:
                return m.value, "memory", m.reason, m.ref
            return None, "", (f"no earlier response contains a {name} (it must come from a row that creates or "
                              f"returns it, or be typed in)"), ""
        if is_secret_field(name):
            return None, "", "it is a secret, so it has to be typed in the sheet (it will be hidden in reports)", ""
        prefer_memory = row.method in ("GET", "PUT", "PATCH", "DELETE") or bool(_LOOKUP_VERBS.search(row.name or ""))
        order = ("sample", "memory", "generate", "ai") if prefer_memory else ("sample", "generate", "memory", "ai")
        siblings = _parent(body_data, ph.path) if ph.where == "body" else None
        for how in order:
            if how == "sample" and sample is not None and ph.where in ("body", "query"):
                p = jsonpath.find_key(sample, "", lambda k: norm(k) == norm(ph.key or name))
                v = jsonpath.get(sample, p) if p else None
                if v not in (None, "*", "") and not isinstance(v, (dict, list)) and not _looks_volatile(ph.key or name, v):
                    return v, "sample", "as shown in this row's sample response", ""
            elif how == "memory":
                m = mem.lookup(ph.key or name, exclude_row=row.number)
                if m is not None and m.score >= 0.75:
                    return m.value, "memory", m.reason, m.ref
            elif how == "generate":
                field_name = ph.key or name
                if kind_for(field_name) is None and kind_for(name) is not None:
                    field_name = name
                if kind_for(field_name) is None and self.ai is not None:
                    continue                               # let the AI suggest something sensible first
                v = self.generator.value(field_name, siblings=siblings)
                if v is not None:
                    kind = kind_for(field_name)
                    ref = "{{$" + kind + "}}" if kind in ("email", "username", "phone", "uuid", "pan", "account_number") else ""
                    return v, "generated", f"generated {kind.replace('_', ' ') if kind else 'test'} data", ref
            elif how == "ai" and self.ai is not None:
                v = self.ai.suggest_value(ph.key or name, row=row, siblings=siblings)
                if v is not None:
                    return v, "ai", "suggested by the local AI model", ""
        if self.ai is not None:
            v = self.generator.value(ph.key or name, siblings=siblings)
            if v is not None:
                return v, "generated", "generated test data", ""
        return None, "", "Restora found no value for it", ""

    # ------------------------------------------------------------------ comparing
    def tolerate(self, row, prepared, fills: list):
        """A callback for compare.diff_json: may this difference be accepted, and why?"""
        if not self.on or not self.settings.brain.smart_compare:
            plugin_rules = (self.plugins.dynamics if self.plugins else [])
            if not plugin_rules:
                return None
        request_text = " ".join([prepared.url or "", prepared.body or ""])
        filled = {}
        for f in fills:
            if f.source in ("memory", "ref"):
                filled[str(f.value)] = f"the value Restora filled in for {f.placeholder.label.split(' = ')[0]} ({f.reason})"
            elif f.source == "generated":
                filled[str(f.value)] = f"test data Restora generated for {f.placeholder.key or f.placeholder.name}"
        for v, (num, fname) in self.memory.generated.items():
            filled.setdefault(v, f"test data Restora generated for {fname}")
        smart = self.on and self.settings.brain.smart_compare
        dynamics = list(self.plugins.dynamics) if self.plugins else []

        def check(path, key, expected, actual):
            for fn in dynamics:
                try:
                    why = fn(path, expected, actual)
                except Exception:  # noqa: BLE001
                    why = None
                if why:
                    return why
            if smart:
                return why_dynamic(str(key), expected, actual, request_text, filled)
            return None
        return check

    # ------------------------------------------------------------------ healing
    def heal(self, prepared, outcome, row, send, passes) -> HealResult:
        res = HealResult(prepared, outcome)
        hs = self.settings.heal
        if not self.on or hs.mode == "off" or row.brain.get("negative") or outcome.error is not None:
            return res
        if outcome.status is None or outcome.status >= 500 or outcome.status not in hs.statuses:
            return res
        if _errors.looks_like_crash(outcome.text):
            return res                                  # a server bug answered as 4xx: report it, never heal it
        ctx = HealContext(self.memory, self.generator, row, _parse(row.expected_response), self.ai, self.plugins)
        cur_p, cur_o = prepared, outcome
        for _ in range(max(1, hs.max_attempts)):
            issues = _errors.parse(cur_o.status, cur_o.text, cur_o.json_value if cur_o.is_json else None, cur_o.headers)
            patch = self._plugin_patch(cur_p, cur_o, row) or diagnose(cur_p, cur_o, issues, ctx)
            if patch is None and self.ai is not None:
                patch = self.ai.heal_patch(cur_p, cur_o, row, self.memory, ctx)
            if patch is None or not patch.changes:
                break
            for ch in patch.changes:
                if ch.where == "wait":
                    time.sleep(float(ch.value))
            new_p = apply(cur_p, patch)
            new_o = send(new_p)
            res.attempts.append(HealAttempt(patch, cur_o.status, new_o.status))
            cur_p, cur_o = new_p, new_o
            ok = passes(new_o)
            if ok:
                res.healed = True
                break
            if new_o.status is None or new_o.status >= 500 or 200 <= new_o.status < 300:
                break                                   # accepted now: a remaining body difference is a real finding
        res.prepared, res.outcome = cur_p, cur_o
        return res

    def recheck_negative(self, prepared, outcome, row, send) -> HealResult:
        """A negative test the API refused: if it was refused for a setup reason the test is not about (a missing
        header, the login token, the content type), fix only that and ask again - see guard.py.
        res.healed is True when a fix was sent; the caller judges the new outcome."""
        res = HealResult(prepared, outcome)
        hs = self.settings.heal
        if not self.on or hs.mode == "off" or not hs.check_negative or not row.brain.get("negative"):
            return res
        ctx = HealContext(self.memory, self.generator, row, None, None, self.plugins)
        cur_p, cur_o = prepared, outcome
        for _ in range(max(1, hs.max_attempts)):
            if cur_o.error is not None or cur_o.status is None or not 400 <= cur_o.status < 500:
                break
            issues = _errors.parse(cur_o.status, cur_o.text, cur_o.json_value if cur_o.is_json else None, cur_o.headers)
            setup, login = _guard.setup_issues(row, cur_p, cur_o, issues)
            if not setup and not login:
                break
            patch = diagnose_setup(cur_p, cur_o, setup, ctx, auth=login)
            if patch is None:
                break
            patch.reason = (f"The API refused this row, but only because {_guard.describe(setup, login)} - not for the "
                            f"reason this test checks. Restora fixed only that ({patch.summary()}) and asked again, "
                            f"so the API judges what the test is about.")
            new_p = apply(cur_p, patch)
            new_o = send(new_p)
            res.attempts.append(HealAttempt(patch, cur_o.status, new_o.status))
            cur_p, cur_o = new_p, new_o
        res.prepared, res.outcome, res.healed = cur_p, cur_o, bool(res.attempts)
        return res

    def _plugin_patch(self, prepared, outcome, row):
        if not self.plugins or not self.plugins.healers:
            return None
        from ..plugin import Attempt
        att = Attempt(prepared, outcome, row, self.memory)
        for fn in self.plugins.healers:
            try:
                p = fn(att)
            except Exception:  # noqa: BLE001 - a broken plug-in must not stop the run
                p = None
            if p is not None and p.changes:
                return p
        return None

    # ------------------------------------------------------------------ after each response
    def learn(self, row, prepared, outcome, fills: list) -> None:
        self.memory.learn_response(row, prepared.method, prepared.url, outcome.status,
                                   outcome.json_value if outcome.is_json else (outcome.text or None), outcome.headers)
        generated = {(f.placeholder.key or f.placeholder.name): f.value for f in fills if f.source == "generated"}
        body = None
        if prepared.body and prepared.body.lstrip()[:1] in "{[":
            try:
                body = json.loads(prepared.body)
            except ValueError:
                body = None
        self.memory.learn_request(row, prepared.method, prepared.url, body, generated)

    def remember_patch_values(self, row, patch: Patch) -> None:
        for ch in patch.changes:
            if ch.where in ("body", "query", "header") and "generated" in (ch.note or "") and ch.value is not None:
                self.memory.generated[str(ch.value)] = (row.number, ch.name.split(".")[-1])

    # ------------------------------------------------------------------ after a failure
    def explain(self, record, row) -> str:
        text = _explain(record, row)
        if self.ai is not None and self.settings.ai.explain_failures and record.result == "FAILED":
            better = self.ai.explain(record, row, text)
            if better:
                return better
        return text


def _parse(text):
    if not text or text.lstrip()[:1] not in "{[":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _parent(data, path: list):
    if not path or data is None:
        return data if isinstance(data, dict) else None
    parent = jsonpath.get(data, path[:-1]) if len(path) > 1 else data
    return parent if isinstance(parent, dict) else None


def _looks_volatile(key: str, value) -> bool:
    return looks_volatile(key, value)


__all__ = ["Brain", "Fill", "FillResult", "HealResult", "Insight", "Plan"]
