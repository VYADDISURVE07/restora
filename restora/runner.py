# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Run one sheet row end-to-end and produce a Record (everything the reports need, secrets already masked).

    prepare (headers) -> brain.fill (gaps) -> plug-ins -> send -> compare -> brain.heal (if rejected)
    -> brain.learn (values for later rows) -> brain.explain (if it failed)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import Settings, prepare, send, to_curl
from .compare import check_body, check_status
from .loader import Row
from .mask import mask_headers, mask_text
from .text import clip


@dataclass
class Record:
    number: int
    sheet_row: int
    name: str
    method: str
    url: str
    result: str                         # PASSED | HEALED | FAILED | SKIPPED
    http_status: int | None = None
    expected_status: int | None = None
    elapsed_ms: float = 0.0
    failures: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    request_headers: dict = field(default_factory=dict)
    request_body: str | None = None
    response_headers: dict = field(default_factory=dict)
    response_body: str | None = None
    expected_response: str | None = None
    curl: str | None = None
    # --- what the brain did (all shown in the reports)
    position: int = 0                   # order in which it actually ran
    planned: list = field(default_factory=list)       # plan-time changes (method, URL, JSON repair, order)
    filled: list = field(default_factory=list)        # gaps filled just before sending
    heal_attempts: list = field(default_factory=list)  # [{"reason", "changes", "status_before", "status_after", "source"}]
    dynamic: list = field(default_factory=list)       # sample differences accepted, with the reason
    explanation: str = ""
    suggestion: str = ""                # heal mode 'suggest': the verified fix that was not applied
    negative: str | None = None         # why the row counts as a negative test
    unresolved: list = field(default_factory=list)
    fix: Any = None                     # raw material for the corrected sheet (not shown in reports)

    @property
    def ok(self) -> bool:
        return self.result in ("PASSED", "HEALED")


def evaluate(row: Row, outcome, tolerate=None) -> tuple:
    """(failures, accepted dynamic differences) for a response."""
    failures: list = []
    accepted: list = []
    if outcome.error:
        return [outcome.error], accepted
    problem = check_status(row.expected_status, outcome.status)
    if problem:
        failures.append(problem)
    failures += [d.describe() for d in check_body(row.expected_response, outcome, tolerate, accepted)]
    return failures, accepted


def execute(row: Row, settings: Settings, brain=None, plugins=None, on_event=None) -> Record:
    emit = on_event or (lambda *a, **k: None)
    base = dict(number=row.number, sheet_row=row.sheet_row, name=row.name, method=row.method, url=row.url,
                expected_status=row.expected_status, notes=list(row.notes), position=row.brain.get("position", row.number),
                planned=list(row.brain.get("planned", [])), negative=row.brain.get("negative"))
    if row.skip:
        rec = Record(result="SKIPPED", failures=[row.skip], **base)
        rec.fix = {"row": row, "fills": [], "patches": [], "outcome": None, "prepared": None}
        emit("done", row=row, record=rec)
        return rec
    if row.error:
        rec = Record(result="FAILED", failures=[row.error], **base)
        rec.explanation = "This row could not be sent: " + row.error.rstrip(".") + "."
        rec.fix = {"row": row, "fills": [], "patches": [], "outcome": None, "prepared": None}
        emit("done", row=row, record=rec)
        return rec

    prepared = prepare(row)
    emit("start", row=row)
    fills = []
    if brain is not None:
        fr = brain.fill(prepared, row)
        prepared, fills = fr.prepared, fr.fills
        if fr.unresolved:
            rec = Record(result="FAILED", failures=[f"Could not fill {label}: {why}" for label, why in fr.unresolved],
                         unresolved=fr.unresolved, filled=[f.note() for f in fills],
                         request_headers=mask_headers(prepared.headers), **base)
            rec.explanation = brain.explain(rec, row)
            rec.fix = {"row": row, "fills": fills, "patches": [], "outcome": None, "prepared": prepared}
            emit("done", row=row, record=rec)
            return rec
        if fills:
            emit("filled", row=row, fills=[f.note() for f in fills])

    def deliver(p):
        for fn in (plugins.before if plugins else []):
            fn(p, row)
        o = send(p, settings)
        for fn in (plugins.after if plugins else []):
            fn(o, row)
        return o

    outcome = deliver(prepared)
    tolerate = brain.tolerate(row, prepared, fills) if brain is not None else None
    failures, accepted = evaluate(row, outcome, tolerate)
    result = "FAILED" if failures else "PASSED"
    attempts, patches, suggestion = [], [], ""
    learn_from = (prepared, outcome)

    if failures and brain is not None and not outcome.error:
        def passes(o):
            return not evaluate(row, o, brain.tolerate(row, prepared, fills))[0]
        emit("healing", row=row, status=outcome.status)
        hr = brain.heal(prepared, outcome, row, deliver, passes)
        for a in hr.attempts:
            attempts.append({"reason": a.patch.reason, "changes": [c.note for c in a.patch.changes],
                             "status_before": a.status_before, "status_after": a.status_after,
                             "source": a.patch.source})
            patches.append(a.patch)
            brain.remember_patch_values(row, a.patch)
        if hr.attempts:
            emit("healed" if hr.healed else "heal_failed", row=row, attempts=attempts)
        if hr.healed and brain.settings.heal.mode == "auto":
            prepared, outcome, result = hr.prepared, hr.outcome, "HEALED"
            failures, accepted = evaluate(row, outcome, brain.tolerate(row, prepared, fills))
            failures = []
        elif hr.healed:                                          # suggest mode: report, do not apply
            suggestion = "Verified fix (not applied, heal mode is 'suggest'): " + \
                         " then ".join("; ".join(c.note for c in p.changes) for p in patches)
        elif hr.attempts:
            prepared, outcome = hr.prepared, hr.outcome
            failures, accepted = evaluate(row, outcome, brain.tolerate(row, prepared, fills))
            result = "FAILED" if failures else "HEALED"
        if suggestion:
            learn_from = (hr.prepared, hr.outcome)

    # A negative test must be refused for its own reason, not for a missing header or login (brain/guard.py)
    if brain is not None and row.brain.get("negative") and not outcome.error and outcome.status is not None \
            and 400 <= outcome.status < 500:
        gr = brain.recheck_negative(prepared, outcome, row, deliver)
        if gr.healed:
            for a in gr.attempts:
                attempts.append({"reason": a.patch.reason, "changes": [c.note for c in a.patch.changes],
                                 "status_before": a.status_before, "status_after": a.status_after,
                                 "source": "guard"})
                patches.append(a.patch)
                brain.remember_patch_values(row, a.patch)
            emit("healed", row=row, attempts=attempts)
            new_failures, new_accepted = evaluate(row, gr.outcome, brain.tolerate(row, gr.prepared, fills))
            accepted_now = gr.outcome.status is not None and 200 <= gr.outcome.status < 300
            if brain.settings.heal.mode == "auto" or accepted_now:
                prepared, outcome, failures, accepted = gr.prepared, gr.outcome, new_failures, new_accepted
                if accepted_now:
                    failures = ["Accepted once Restora fixed the setup: the API does not refuse what this test checks"] + failures
                result = "FAILED" if failures else "HEALED"
            else:                                                # suggest mode: report, do not apply
                suggestion = "Verified fix (not applied, heal mode is 'suggest'): " + \
                             " then ".join(a.patch.summary() for a in gr.attempts)
                failures = failures or ["Refused for the wrong reason: " + gr.attempts[0].patch.reason.split(" - ")[0]]
                result = "FAILED"
                learn_from = (gr.prepared, gr.outcome)

    if brain is not None:
        if suggestion:
            brain.learn(row, *learn_from, fills)
        else:
            brain.learn(row, prepared, outcome, fills)

    expected = mask_text(row.expected_response) if row.expected_response else None
    rec = Record(
        result=result,
        http_status=outcome.status,
        elapsed_ms=outcome.elapsed_ms,
        failures=failures,
        request_headers=mask_headers(prepared.headers),
        request_body=clip(mask_text(prepared.body)) if prepared.body is not None else None,
        response_headers=mask_headers(outcome.headers),
        response_body=clip(mask_text(outcome.text)) if outcome.text else None,
        expected_response=expected,
        curl=to_curl(prepared),
        filled=[f.note() for f in fills],
        heal_attempts=attempts,
        dynamic=[f"{p}: {why}" for p, why in accepted],
        suggestion=suggestion,
        **{**base, "notes": base["notes"] + prepared.notes, "method": prepared.method, "url": prepared.url},
    )
    guarded = any(a.get("source") == "guard" for a in attempts)     # the setup fix belongs in the corrected sheet
    rec.fix = {"row": row, "fills": fills, "patches": patches if result == "HEALED" or suggestion or guarded else [],
               "outcome": outcome if rec.ok else None, "prepared": prepared, "dynamic": [p for p, _ in accepted],
               "generated": set(brain.memory.generated) if brain is not None else set()}
    if rec.result == "FAILED" and brain is not None:
        rec.explanation = brain.explain(rec, row)
    elif rec.result == "FAILED":
        from .brain.explain import explain
        rec.explanation = explain(rec, row)
    emit("done", row=row, record=rec)
    return rec
