"""Allure results (optional). If allure-pytest is not installed everything here quietly does nothing."""
from __future__ import annotations

try:
    import allure
    ENABLED = True
except ImportError:  # pragma: no cover
    allure = None
    ENABLED = False

from .text import looks_like_json


def _kind(text: str):
    return allure.attachment_type.JSON if looks_like_json(text) else allure.attachment_type.TEXT


def annotate(record, input_name: str) -> None:
    """Attach request / response / differences for one row to the running Allure test."""
    if not ENABLED:
        return
    allure.dynamic.title(f"{record.number:02d}. {record.name}")
    allure.dynamic.suite(input_name)
    allure.dynamic.tag(record.method)
    allure.dynamic.description(f"{record.method} {record.url}\n\nSheet row {record.sheet_row}")

    if record.result == "SKIPPED":
        return
    with allure.step(f"{record.method} {record.url}"):
        req = "\n".join(f"{k}: {v}" for k, v in record.request_headers.items())
        if record.request_body:
            req += "\n\n" + record.request_body
        if req.strip():
            allure.attach(req, "Request (secrets hidden)", allure.attachment_type.TEXT)
        if record.curl:
            allure.attach(record.curl, "curl", allure.attachment_type.TEXT)
    if record.http_status is not None:
        with allure.step(f"Response: HTTP {record.http_status} in {record.elapsed_ms:,.0f} ms"):
            if record.response_body:
                allure.attach(record.response_body, "Response body", _kind(record.response_body))
            if record.expected_response:
                allure.attach(record.expected_response, "Sample (expected) response", _kind(record.expected_response))
    if record.notes:
        allure.attach("\n".join(record.notes), "Adjusted automatically", allure.attachment_type.TEXT)
    brain = list(getattr(record, "planned", []) or []) + [f"filled {f}" for f in (getattr(record, "filled", []) or [])]
    brain += [f"accepted {d}" for d in (getattr(record, "dynamic", []) or [])]
    if brain:
        allure.attach("\n".join(brain), "What the brain did", allure.attachment_type.TEXT)
    for i, a in enumerate(getattr(record, "heal_attempts", []) or [], start=1):
        with allure.step(f"Heal attempt {i}: HTTP {a['status_before']} -> {a['status_after']}"):
            allure.attach(a["reason"] + "\n\n- " + "\n- ".join(a["changes"]), "Why and what changed", allure.attachment_type.TEXT)
    if record.result == "HEALED":
        allure.dynamic.tag("healed")
    if getattr(record, "explanation", ""):
        allure.attach(record.explanation, "In plain English", allure.attachment_type.TEXT)
    if record.failures:
        with allure.step("What went wrong"):
            allure.attach("\n".join(record.failures), "Differences", allure.attachment_type.TEXT)
