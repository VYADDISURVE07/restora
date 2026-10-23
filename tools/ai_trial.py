# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Try the local AI model on every job Restora gives it, and time each answer.

    python tools/ai_trial.py                      the model in restora.toml
    python tools/ai_trial.py --model qwen3:1.7b   another model

Writes reports/ai_trial.md (for people) and reports/ai_trial.json (for tools). The Demo Bank runs on this
computer, so nothing leaves it. Each case says what a good answer looks like, so a weak model shows up.
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo import mock_server  # noqa: E402
from restora import inputs  # noqa: E402
from restora.brain import errors  # noqa: E402
from restora.brain.ai import connect  # noqa: E402
from restora.brain.generate import Generator  # noqa: E402
from restora.brain.heal import HealContext, diagnose  # noqa: E402
from restora.brain.memory import Memory  # noqa: E402
from restora.client import Outcome, Prepared  # noqa: E402
from restora.loader import Row  # noqa: E402
from restora.session import Session  # noqa: E402
from restora.settings import load as load_settings  # noqa: E402

BANK = "http://127.0.0.1:18099/api/v1"
MESSY = ROOT / "examples" / "demo_bank_messy.csv"

VALUES = [  # field, method, path, test name, other fields, what a good answer looks like
    ("purposeCode", "POST", "/api/v1/transfers", "Transfer money",
     {"amount": 1500, "currency": "INR", "remarks": "Rent for October"}, "a short code such as P0101 or RENT"),
    ("nomineeRelation", "POST", "/api/v1/accounts", "Open a savings account",
     {"nomineeName": "Ravi Sharma", "accountType": "SAVINGS"}, "a relation such as Spouse or Father"),
    ("occupation", "POST", "/api/v1/customers", "Open a new customer",
     {"name": "Priya Sharma", "email": "priya@example.com"}, "an occupation such as Software Engineer"),
    ("branchName", "POST", "/api/v1/accounts", "Open a savings account",
     {"ifsc": "DEMO0001234", "city": "Pune"}, "a branch name, ideally in Pune"),
    ("annualIncomeBand", "POST", "/api/v1/customers", "Open a new customer",
     {"occupation": "Teacher"}, "a band such as 5-10 LPA"),
    ("otp", "POST", "/api/v1/transfers", "Confirm a transfer", {}, "nothing: a secret is never invented (no AI call)"),
]

HEALS = [  # name, method, path, headers, body, status, response, what a good answer looks like
    ("UPI is not allowed for large transfers", "POST", "/api/v1/transfers", {"Content-Type": "application/json"},
     {"fromAccount": "500010001234", "toAccount": "500010009999", "amount": 25000, "currency": "INR", "mode": "UPI"},
     400, {"status": "FAILURE", "responseCode": "U17",
           "responseMessage": "Transaction mode UPI is not permitted above Rs 1000. Use IMPS, NEFT or RTGS."},
     "set mode to IMPS, NEFT or RTGS"),
    ("A customer must be an adult", "POST", "/api/v1/customers", {"Content-Type": "application/json"},
     {"name": "Priya Sharma", "email": "priya@example.com", "dateOfBirth": "2014-05-01"},
     422, {"errCode": "AGE_001", "errDesc": "Customer must be at least 18 years old on the date of opening"},
     "set dateOfBirth to an adult's date, for example 1990-05-01"),
    ("Currency codes are upper case", "GET", "/api/v1/rates?currency=usd", {}, None,
     400, {"error": "Currency codes are case sensitive"}, "set the query parameter currency to USD"),
    ("A server bug wearing a 400", "POST", "/api/v1/beneficiaries", {"Content-Type": "application/json"},
     {"name": "Landlord", "accountNumber": "500010009999", "ifsc": "DEMO0001234"},
     400, {"error": "java.lang.NullPointerException at com.demo.BeneficiaryService.save(BeneficiaryService.java:42)"},
     "fixable = false (a server bug)"),
    ("An expired session", "GET", "/api/v1/accounts/me", {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.old.token"}, None,
     403, {"message": "Session token expired, please login again"},
     "no change to Authorization: a token is never invented"),
]

ASKS = [  # plain-English request, what a good answer looks like
    ("Check that a transfer with a negative amount is rejected",
     "POST /api/v1/transfers, a negative amount, expected 400 or 4xx, the Bearer token copied"),
    ("Check that opening an account with an unknown account type fails",
     "POST /api/v1/accounts with a made-up accountType, expected 4xx"),
    ("Get the exchange rate for EUR", "GET /api/v1/rates?currency=EUR, expected 200"),
]


def _outcome(prepared, status, body):
    o = Outcome(prepared, status=status, headers={"Content-Type": "application/json"}, text=json.dumps(body))
    o.json_value = body
    return o


def _timed(fn):
    started = time.perf_counter()
    value = fn()
    return value, round(time.perf_counter() - started, 2)


def main(argv=None) -> int:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--model", help="model name (default: restora.toml)")
    a.add_argument("--ai-url", help="AI server address (default: restora.toml)")
    a.add_argument("--out", default=str(ROOT / "reports"), help="folder for ai_trial.md and ai_trial.json")
    args = a.parse_args(argv)

    settings = load_settings(None, ROOT)
    if args.model:
        settings.ai.model = args.model
    if args.ai_url:
        settings.ai.url = args.ai_url
    settings.ai.max_calls = 200
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = io.StringIO()
    result: dict = {"model": settings.ai.model, "url": settings.ai.url, "started": time.strftime("%Y-%m-%d %H:%M:%S")}

    def say(line: str = ""):
        print(line, flush=True)
        md.write(line + "\n")

    say(f"# Restora AI trial: {settings.ai.model}")
    say()
    say(f"Server: {settings.ai.url} ({settings.ai.provider}). Started {result['started']}.")
    ai, secs = _timed(lambda: connect(settings.ai, settings.ai_key))
    say(f"Status: {ai.message} ({secs} s)")
    result["status"] = {"ready": ai.ready, "message": ai.message, "models": ai.models}
    if not ai.ready:
        say("\nThe model is not ready, so there is nothing to try. Run ai_setup.bat first.")
        _write(out_dir, md, result)
        return 2

    # warm-up: the first answer includes loading the model into memory
    _, cold = _timed(lambda: ai.suggest_value("city", Row(1, 2, "Open a new customer", "POST", BANK + "/customers"), {}))
    say(f"First answer (includes loading the model): {cold} s")
    result["cold_seconds"] = cold
    warm = []

    say("\n## 1. Values for fields the rules do not know\n")
    say("| Field | Value | Seconds | A good answer |")
    say("|---|---|---|---|")
    result["values"] = []
    for field, method, path, name, siblings, good in VALUES:
        calls = ai.client.calls
        value, s = _timed(lambda: ai.suggest_value(field, Row(1, 2, name, method, "http://127.0.0.1:18099" + path), siblings))
        asked = ai.client.calls > calls
        if asked:
            warm.append(s)
        shown = "(none)" if value is None else json.dumps(value, ensure_ascii=False)
        say(f"| {field} | {shown} | {s if asked else 'no call'} | {good} |")
        result["values"].append({"field": field, "value": value, "seconds": s, "asked": asked})

    say("\n## 2. Fixes for errors the rules cannot read\n")
    result["heals"] = []
    for name, method, path, headers, body, status, response, good in HEALS:
        prepared = Prepared(method, "http://127.0.0.1:18099" + path, dict(headers), json.dumps(body) if body else None)
        o = _outcome(prepared, status, response)
        row = Row(1, 2, name, method, prepared.url)
        ctx = HealContext(Memory(), Generator(seed=3), row)
        crash = errors.looks_like_crash(o.text)
        rules = None if crash else diagnose(prepared, o, errors.parse(status, o.text, response), ctx)
        patch, s = _timed(lambda: ai.heal_patch(prepared, o, row, Memory(), ctx))
        warm.append(s)
        said = patch.summary() if patch else "no change (not fixable, or nothing safe to change)"
        say(f"**{name}** - HTTP {status}: `{json.dumps(response)[:110]}`  ")
        if crash:
            say(f"Rules: a crash ('{crash}'), so Restora reports a server bug and never asks the AI. "
                f"Asked anyway, the AI ({s} s) said: {said}  ")
        else:
            say(f"Rules: {'a fix (' + rules.summary() + ')' if rules else 'no fix'}. AI ({s} s): {said}  ")
        if patch:
            say(f"Diagnosis: {patch.reason}  ")
        say(f"A good answer: {good}\n")
        result["heals"].append({"case": name, "crash": crash, "rules": rules.summary() if rules else None,
                                "ai": patch.summary() if patch else None, "reason": patch.reason if patch else None,
                                "seconds": s})

    say("## 3. A plain-English explanation\n")
    record = SimpleNamespace(name="Pay a merchant", expected_status=200, http_status=400,
                             failures=["HTTP status was 400, expected 200"],
                             response_body=json.dumps({"responseCode": "U30", "responseMessage": "Debit has been failed",
                                                       "npciCode": "ZM"}))
    text, s = _timed(lambda: ai.explain(record, None, ""))
    warm.append(s)
    say(f"({s} s) {text}\n")
    result["explain"] = {"text": text, "seconds": s}

    say("## 4. Tests written from plain English\n")
    known = inputs.load(MESSY).records
    result["asks"] = []
    written = []
    for ask, good in ASKS:
        tests, s = _timed(lambda: ai.write_tests(ask, known))
        warm.append(s)
        say(f"**\"{ask}\"** ({s} s). A good answer: {good}")
        for t in tests:
            say(f"- {t['method']} {t['url']} | expects {t['expected_status'] or 'any 2xx'} | {t['name']}")
            if t["headers"]:
                say(f"  - headers: `{t['headers'][:120]}`")
            if t["body"]:
                say(f"  - body: `{t['body'][:160]}`")
        say()
        result["asks"].append({"ask": ask, "tests": tests, "seconds": s})
        if ask.startswith("Check that a transfer"):
            written = tests

    say("## 5. End to end on the Demo Bank (brain = ai)\n")
    server = mock_server.start(18099)
    try:
        sess = Session(settings, ROOT)
        records = inputs.load(MESSY).records + [dict(t) for t in written]
        sess.run_all(inputs.rows(SimpleNamespace(records=records)))
        c = sess.counts()
        summary = sess.brain_summary()
        say(f"{c.get('PASSED', 0)} passed, {c.get('HEALED', 0)} healed, {c.get('FAILED', 0)} failed. "
            f"AI calls: {summary['ai_calls']} ({summary['ai_seconds']} s).")
        for r in sess.records[-len(written):] if written else []:
            say(f"- AI-written row **{r.name}**: {r.result} (HTTP {r.http_status})")
            for at in r.heal_attempts:
                say(f"  - {at['reason']}")
            if r.explanation:
                say(f"  - {r.explanation}")
        result["end_to_end"] = {"counts": dict(c), "brain": summary,
                                "written": [{"name": r.name, "result": r.result, "status": r.http_status,
                                             "attempts": r.heal_attempts, "explanation": r.explanation}
                                            for r in (sess.records[-len(written):] if written else [])]}
    finally:
        server.shutdown()

    if warm:
        say(f"\n## Timing\n\nWarm answers: median {statistics.median(warm):.1f} s, slowest {max(warm):.1f} s "
            f"({len(warm)} answers). First answer, including loading the model: {cold} s.")
        result["warm_median"] = round(statistics.median(warm), 2)
        result["warm_max"] = round(max(warm), 2)
    _write(out_dir, md, result)
    return 0


def _write(out_dir: Path, md: io.StringIO, result: dict) -> None:
    (out_dir / "ai_trial.md").write_text(md.getvalue(), encoding="utf-8")
    (out_dir / "ai_trial.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nWritten: {out_dir / 'ai_trial.md'}")


if __name__ == "__main__":
    sys.exit(main())
