"""Tests for status classes (4xx) and for negative tests refused for the wrong reason (brain/guard.py)."""
import csv
import io
from pathlib import Path

import pytest

from demo import mock_server
from restora import inputs
from restora.brain.intent import negative_reason
from restora.brain.llm import LLMError, parse_json_answer
from restora.compare import check_status
from restora.loader import Row
from restora.session import Session
from restora.settings import Settings
from restora.status import StatusClass, parse_status

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18099/api/v1"


# ----------------------------------------------------------------------------- 4xx, 5xx ...

def test_status_classes():
    s = parse_status("4xx")
    assert isinstance(s, StatusClass) and str(s) == "4xx" and f"HTTP {s}" == "HTTP 4xx"
    assert s.matches(400) and s.matches(422) and not s.matches(200) and not s.matches(500)
    assert parse_status("201") == 201 and parse_status(" 5XX ").matches(503) and parse_status("") is None
    with pytest.raises(ValueError):
        parse_status("4x")
    assert check_status(s, 422) is None
    assert check_status(s, 201) == "HTTP status was 201, expected any 4xx"
    assert check_status(404, 404) is None and check_status(404, 400) == "HTTP status was 400, expected 404"


def test_a_4xx_row_is_a_negative_test():
    row = Row(1, 2, "Anything", "POST", "http://h/x")
    row.expected_status = parse_status("4xx")
    assert negative_reason(row) == "it expects HTTP 4xx"


# ----------------------------------------------------------------------------- wrong-reason refusals

@pytest.fixture(scope="module")
def bank():
    server = mock_server.start(18099)
    yield
    server.shutdown()


SETUP = [
    ["Login", "POST", f"{BASE}/auth/login", "", '{"username": "demo", "password": "demo123"}', "200", ""],
    ["Open a new customer", "POST", f"{BASE}/customers", "Authorization: Bearer <token>",
     '{"name": "Priya Sharma", "email": "<email>", "phone": "9876543210"}', "201", ""],
    ["Open a savings account", "POST", f"{BASE}/accounts", "Authorization: Bearer <token>",
     '{"customerId": "<customerId>", "accountType": "SAVINGS", "initialDeposit": 5000}', "201", ""],
]


def _run(extra: list, heal: str = "auto", check_negative: bool = True):
    mock_server.BANK.__init__()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Method", "URL", "Headers", "Body", "ExpectedStatus", "ExpectedResponse"])
    w.writerows(SETUP + extra)
    s = Settings()
    s.brain.mode = "rules"
    s.heal.mode = heal
    s.heal.check_negative = check_negative
    s.plugins_enabled = False
    sess = Session(s, ROOT, check_ai=False)
    sess.run_all(inputs.rows(inputs.from_text(buf.getvalue(), "guard.csv")))
    return {r.name: r for r in sess.records}, sess


def _transfer(name: str, amount, status: str = "4xx"):
    return [name, "POST", f"{BASE}/transfers", "Authorization: Bearer <token>",
            '{"fromAccount": "<accountNumber>", "toAccount": "500010009999", "amount": %s}' % amount, status, ""]


def test_refused_for_a_missing_header_is_checked_again(bank):
    by, _ = _run([_transfer("Transfer with a negative amount is rejected", -500)])
    r = by["Transfer with a negative amount is rejected"]
    assert r.result == "HEALED", r.failures
    assert r.heal_attempts[0]["source"] == "guard" and "X-Request-Id" in r.heal_attempts[0]["changes"][0]
    assert "amount must be greater than 0" in r.response_body                 # refused for the right reason now
    assert "not for the reason this test checks" in r.heal_attempts[0]["reason"]


def test_a_wrong_reason_pass_that_hides_a_bug_fails(bank):
    by, _ = _run([_transfer("Transfer of one rupee is rejected", 1)])
    r = by["Transfer of one rupee is rejected"]
    assert r.result == "FAILED" and r.http_status == 201                   # the API accepts it once the header is there
    assert r.failures[0].startswith("Accepted once Restora fixed the setup")
    assert "passed for the wrong reason" in r.explanation


def test_a_test_about_the_header_itself_is_left_alone(bank):
    by, _ = _run([_transfer("Transfer without a request id is rejected", 100)])
    r = by["Transfer without a request id is rejected"]
    assert r.result == "PASSED" and not r.heal_attempts and "X-Request-Id" in r.response_body


def test_login_tests_are_never_given_a_token(bank):
    by, _ = _run([["Login with a wrong password is rejected", "POST", f"{BASE}/auth/login", "",
                   '{"username": "demo", "password": "wrong"}', "401", ""],
                  ["My accounts without a token is refused", "GET", f"{BASE}/accounts/me", "", "", "401", ""]])
    for name in ("Login with a wrong password is rejected", "My accounts without a token is refused"):
        assert by[name].result == "PASSED" and not by[name].heal_attempts


def test_a_missing_login_is_a_setup_problem_for_other_tests(bank):
    row = _transfer("Transfer with a negative amount is rejected", -500)
    row[3] = ""                                                          # no Authorization at all
    by, _ = _run([row])
    r = by["Transfer with a negative amount is rejected"]
    assert r.result == "HEALED", r.failures
    notes = " ".join(c for a in r.heal_attempts for c in a["changes"])
    assert "Authorization" in notes and "X-Request-Id" in notes and "amount must be greater than 0" in r.response_body


def test_the_check_can_be_switched_off_and_suggest_mode_only_reports(bank):
    by, _ = _run([_transfer("Transfer with a negative amount is rejected", -500)], check_negative=False)
    assert by["Transfer with a negative amount is rejected"].result == "PASSED"
    by, _ = _run([_transfer("Transfer with a negative amount is rejected", -500)], heal="suggest")
    r = by["Transfer with a negative amount is rejected"]
    assert r.result == "FAILED" and "X-Request-Id" in r.suggestion


def test_the_corrected_sheet_keeps_the_setup_fix(bank, tmp_path):
    _, sess = _run([_transfer("Transfer with a negative amount is rejected", -500)])
    text = sess.write(tmp_path / "g", "guard.csv")["fixed_csv"].read_text(encoding="utf-8-sig")
    row = next(r for r in csv.DictReader(io.StringIO(text)) if r["Name"].startswith("Transfer with a negative amount"))
    assert "X-Request-Id: {{$uuid}}" in row["Headers"] and row["ExpectedStatus"] == "4xx"


# ----------------------------------------------------------------------------- thinking models

def test_answers_from_thinking_models():
    assert parse_json_answer('<think>hmm</think>{"a": 1}') == {"a": 1}
    assert parse_json_answer('Let me think about this field...\n</think>\n\n{"a": 2}') == {"a": 2}
    with pytest.raises(LLMError):
        parse_json_answer('<think>the user wants a value for')


# ----------------------------------------------------------------------------- crashes answered as 4xx

def test_a_crash_answered_as_400_is_a_bug_not_a_heal():
    from restora.brain.errors import looks_like_crash
    assert looks_like_crash('{"error": "java.lang.NullPointerException at x.Y.save(Y.java:42)"}') == "NullPointerException"
    assert looks_like_crash("Traceback (most recent call last):\n  File \"app.py\", line 3") is not None
    for deliberate in ('{"type": "ValidationError", "message": "email is invalid"}', "Error: amount must be positive",
                       '{"code": "InsufficientFundsError"}', "Value error, amount must be positive"):
        assert looks_like_crash(deliberate) is None


def test_crash_rows_are_explained_and_not_healed():
    import json
    from types import SimpleNamespace
    from restora.brain import Brain
    from restora.brain.explain import explain
    from restora.client import Outcome, Prepared
    s = Settings()
    s.brain.mode = "rules"
    brain = Brain(s)
    row = Row(1, 2, "Add a payee", "POST", "http://h/payees")
    p = Prepared("POST", "http://h/payees", {"Content-Type": "application/json"}, '{"nick": "A"}')
    body = {"message": "name is required", "trace": "java.lang.NullPointerException at x.Payees.save(Payees.java:42)"}
    o = Outcome(p, status=400, headers={}, text=json.dumps(body))
    o.json_value = body
    assert not brain.heal(p, o, row, lambda q: o, lambda o2: False).attempts     # 'name is required' is not healed
    rec = SimpleNamespace(result="FAILED", failures=["HTTP status was 400, expected 201"], http_status=400,
                          expected_status=201, response_body=o.text, suggestion="", unresolved=[], heal_attempts=[])
    assert "crashed" in explain(rec) and "never heals" in explain(rec)


# ----------------------------------------------------------------------------- tests written by the AI

def test_ai_written_rows_get_the_known_setup_headers_back():
    from restora.brain.ai import _matching_call, _prompt_headers, _with_setup_headers
    known = [{"name": "Transfer money", "method": "POST", "url": "/api/v1/transfers",
              "headers": "Authorization: Bearer <token>\nX-Channel: MOBILE"},
             {"name": "Login", "method": "POST", "url": "/api/v1/auth/login", "headers": "Authorization: Bearer eyJsecret"}]
    assert _prompt_headers(known[1]["headers"]) == ["Authorization: Bearer <token>"]       # real secrets never reach the model
    match = _matching_call("POST", "http://127.0.0.1:18099/api/v1/transfers", known)
    got = _with_setup_headers("Content-Type: application/json", match, "Check that a transfer with a negative amount is rejected")
    assert got.splitlines() == ["Content-Type: application/json", "Authorization: Bearer <token>", "X-Channel: MOBILE"]
    kept = _with_setup_headers("", match, "A transfer without a token is refused")
    assert "Authorization" not in kept and "X-Channel: MOBILE" in kept


def test_ai_written_rows_keep_the_known_gaps():
    import json
    from restora.brain.ai import _with_known_placeholders
    match = {"body": '{"fromAccount": "<accountNumber>", "toAccount": "500010009999", "amount": 1500}'}
    body = '{"fromAccount": "500010009999", "toAccount": "500010010000", "amount": -1500}'
    fixed = json.loads(_with_known_placeholders(body, match, "Check that a transfer with a negative amount is rejected"))
    assert fixed == {"fromAccount": "<accountNumber>", "toAccount": "500010010000", "amount": -1500}
    about = _with_known_placeholders(body, match, "A transfer from an unknown account is rejected")
    assert json.loads(about)["fromAccount"] == "500010009999"          # the test is about that field: left alone
