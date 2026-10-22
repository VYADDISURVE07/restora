"""Tests for the brain: gaps, order, healing, comparison, explanations, the corrected sheet, plug-ins and AI paths."""
import json
import re
from pathlib import Path

import pytest

from demo import mock_server
from restora import inputs, plugin
from restora.brain import errors, jsonpath
from restora.brain.generate import Generator, kind_for
from restora.brain.heal import Change, HealContext, Patch, apply, diagnose
from restora.brain.intent import negative_reason
from restora.brain.memory import Memory
from restora.brain.names import similarity, words
from restora.brain.placeholders import scan_headers, scan_json, scan_url
from restora.brain.repair import RepairFailed, fix_url, infer_method, repair_json
from restora.brain.volatile import why_dynamic
from restora.client import Outcome, Prepared
from restora.loader import Row, load_records
from restora.session import Session
from restora.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
MESSY = ROOT / "examples" / "demo_bank_messy.csv"


# ----------------------------------------------------------------------------- names and gaps

def test_names_match_like_a_person():
    assert words("X-Request-ID") == ("x", "request", "id")
    assert similarity("token", "access_token") >= 0.9
    assert similarity("customerId", "customer_id") == 1.0
    assert similarity("phone", "mobileNumber") >= 0.9
    assert similarity("fromAccount", "accountNumber") >= 0.7
    assert similarity("beneficiaryName", "email") == 0.0


def test_placeholders_everywhere():
    url = scan_url("http://h:8080/api/customers/{customerId}/orders/:orderId?x=<page>&y=TBD&z={{Login.id}}")
    kinds = {(p.kind, p.name) for p in url}
    assert ("path", "customer id") in kinds and ("path", "order id") in kinds
    assert ("angle", "page") in kinds and ("word", "y") in kinds and ("ref", "Login.id") in kinds
    assert next(p for p in url if p.kind == "path" and "customer" in p.name).resource == "customer"
    hdr = scan_headers({"Authorization": "Bearer <token>", "X-Api-Key": "your-api-key", "Accept": "application/json"})
    assert {(p.name, p.whole) for p in hdr} == {("token", False), ("x api key", True)}
    body = scan_json({"name": "Priya", "email": "<email>", "phone": "TBD", "note": "<b>bold</b>", "n": 5})
    assert {(p.kind, p.name) for p in body} == {("angle", "email"), ("word", "phone")}


def test_explicit_references_survive_when_the_brain_is_off():
    assert {p.kind for p in scan_url("http://h/x/{{id}}/<y>/{z}", implicit=False)} == {"ref"}


# ----------------------------------------------------------------------------- repairs

@pytest.mark.parametrize("text,fixed", [
    ("{'a': 'b',}", {"a": "b"}),
    ('{a: 1, b: True, c: None}', {"a": 1, "b": True, "c": None}),
    ('{"a": 1 "b": 2}', {"a": 1, "b": 2}),
    ('{"a": [1, 2,], // note\n "b": TBD}', {"a": [1, 2], "b": "TBD"}),
    ('{"amount": 10, "to": {{toAccount}} }', {"amount": 10, "to": "{{toAccount}}"}),
    ('{"a": {"b": 1}', {"a": {"b": 1}}),
    ('{“x”: “y”}', {"x": "y"}),
])
def test_json_repair(text, fixed):
    assert repair_json(text)[0] == fixed


def test_json_repair_refuses_plain_text():
    with pytest.raises(RepairFailed):
        repair_json("hello world")


def test_method_and_url_inference():
    assert infer_method("Get the new customer", False)[0] == "GET"
    assert infer_method("Open a new customer", True)[0] == "POST"
    assert infer_method("Update phone", True)[0] == "PUT"
    assert infer_method("Search customers", True)[0] == "POST"
    assert infer_method("Remove payee", False)[0] == "DELETE"
    assert fix_url("/api/x", "http://h:1/api/health")[0] == "http://h:1/api/x"
    assert fix_url("localhost:8080/x", None)[0] == "http://localhost:8080/x"
    assert fix_url("api.example.com/v1", None)[0] == "https://api.example.com/v1"


# ----------------------------------------------------------------------------- test data

def test_generated_data_is_realistic_and_safe():
    g = Generator(seed=1)
    assert g.value("email", siblings={"name": "Priya Sharma"}).startswith("priya.sharma.")
    assert g.value("email").endswith("@example.com")
    assert re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", g.value("pan"))
    assert re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", g.value("ifscCode"))
    assert re.fullmatch(r"[6-9]\d{9}", g.value("mobileNumber"))
    assert g.value("X-Request-Id") and len(g.value("X-Request-Id")) == 36
    for secret in ("password_token", "apiKey", "aadhaar", "accessToken", "client_secret"):
        assert g.value(secret) is None
    assert kind_for("initialDeposit") == "amount" and kind_for("createdAt") == "timestamp"
    assert g.value("accountType", hint={"enum": ["SAVINGS", "CURRENT"]}) == "SAVINGS"


def test_gstin_checksum():
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    g = Generator(seed=3).value("gstin")
    total = sum((lambda v: v // 36 + v % 36)(chars.index(c) * (2 if i % 2 else 1)) for i, c in enumerate(g[:14]))
    assert g[14] == chars[(36 - total % 36) % 36]


# ----------------------------------------------------------------------------- error messages

@pytest.mark.parametrize("status,body,expect", [
    (400, {"error": "name is required"}, ("missing", "name")),
    (400, {"message": "\"email\" is required"}, ("missing", "email")),
    (400, {"errors": [{"field": "phone", "defaultMessage": "must not be blank"}]}, ("missing", "phone")),
    (422, {"detail": [{"type": "enum", "loc": ["body", "accountType"], "msg": "x", "ctx": {"expected": "'A' or 'B'"}}]}, ("enum", "accountType")),
    (422, {"detail": [{"type": "float_parsing", "loc": ["body", "amount"], "msg": "x"}]}, ("type", "amount")),
    (400, {"errors": {"Email": ["The Email field is required."]}}, ("missing", "Email")),
    (400, {"email": ["has already been taken"]}, ("duplicate", "email")),
    (400, {"errors": [{"msg": "Invalid value", "param": "email", "location": "body"}]}, ("format", "email")),
    (400, {"code": "X", "message": "Header 'X-Request-Id' is required"}, ("missing", "X-Request-Id")),
    (400, {"error": "currency query parameter is required"}, ("missing", "currency")),
    (400, {"message": "password must be at least 8 characters"}, ("min_length", "password")),
    (400, {"message": "amount must be greater than 0"}, ("min", "amount")),
    (400, {"message": "dob must be in format DD-MM-YYYY"}, ("date_format", "dob")),
    (400, {"message": "\"nickname\" is not allowed"}, ("unknown", "nickname")),
    (409, {"message": "A customer with this email already exists"}, ("duplicate", "email")),
])
def test_error_styles(status, body, expect):
    issues = errors.parse(status, json.dumps(body), body)
    assert expect in {(i.kind, i.field) for i in issues}


def test_error_locations():
    q = errors.parse(400, '{"error": "currency query parameter is required"}')
    h = errors.parse(400, '{"message": "Header \'X-Request-Id\' is required"}')
    assert q[0].location == "query" and h[0].location == "header"


# ----------------------------------------------------------------------------- dynamic values

def test_dynamic_values():
    assert why_dynamic("createdAt", "2026-01-01T00:00:00Z", "2026-10-22T08:00:00Z")
    assert why_dynamic("transferId", "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "6fa459ea-ee8a-3ca4-894e-db77e160355e")
    assert why_dynamic("customerId", "C1042", "C1002")
    assert why_dynamic("id", 7, 8)
    assert why_dynamic("status", "ACTIVE", "BLOCKED") is None                    # real values stay strict
    assert why_dynamic("amount", 1500, 1600) is None
    assert why_dynamic("id", 2, 1, request_text="http://h/users/1") is None      # the API echoes what we typed
    assert why_dynamic("email", "a@x.com", "b@x.com", filled={"b@x.com": "test data"}) == "it is test data"


# ----------------------------------------------------------------------------- intent

def test_negative_tests_are_recognised():
    def r(name, status=None, exp=None):
        return Row(1, 2, name, "POST", "http://h", expected_status=status, expected_response=exp)
    assert negative_reason(r("Create user", 400))
    assert negative_reason(r("Create user", 201)) is None
    assert negative_reason(r("Login with a wrong password"))
    assert negative_reason(r("Transfer 500 rupees")) is None
    assert negative_reason(r("X", None, '{"error": "nope"}'))


# ----------------------------------------------------------------------------- healing rules (no network)

def _ctx(row, memory=None, sample=None):
    return HealContext(memory or Memory(), Generator(seed=5), row, sample)


def _out(status, body, headers=None):
    o = Outcome(Prepared("POST", "http://h/x", {}, None), status=status, headers=headers or {}, text=json.dumps(body))
    o.json_value = body
    return o


def test_heal_missing_field_and_enum_and_type():
    row = Row(1, 2, "Open account", "POST", "http://h/accounts")
    p = Prepared("POST", "http://h/accounts", {"Content-Type": "application/json"},
                 json.dumps({"accountType": "savings", "initialDeposit": "5000"}))
    body = {"detail": [{"type": "enum", "loc": ["body", "accountType"], "msg": "x", "ctx": {"expected": "'SAVINGS' or 'CURRENT'"}},
                       {"type": "float_parsing", "loc": ["body", "initialDeposit"], "msg": "x"},
                       {"type": "missing", "loc": ["body", "currency"], "msg": "Field required"}]}
    o = _out(422, body)
    patch = diagnose(p, o, errors.parse(422, o.text, body), _ctx(row))
    fixed = json.loads(apply(p, patch).body)
    assert fixed == {"accountType": "SAVINGS", "initialDeposit": 5000, "currency": "INR"}


def test_heal_auth_uses_an_earlier_token_only():
    row = Row(5, 6, "Profile", "GET", "http://h/me")
    p = Prepared("GET", "http://h/me", {}, None)
    o = _out(401, {"error": "unauthorized"})
    assert diagnose(p, o, errors.parse(401, o.text, o.json_value), _ctx(row)) is None      # no token: nothing invented
    mem = Memory()
    login = Row(1, 2, "Login", "POST", "http://h/login")
    mem.learn_response(login, "POST", "http://h/login", 200, {"access_token": "abcdefghij123", "token_type": "Bearer"}, {})
    patch = diagnose(p, o, errors.parse(401, o.text, o.json_value), _ctx(row, mem))
    assert apply(p, patch).headers["Authorization"] == "Bearer abcdefghij123"
    assert patch.changes[0].ref == "Bearer {{Login.access_token}}"


def test_heal_method_content_type_header_query():
    row = Row(1, 2, "Update customer", "POST", "http://h/c/1")
    p = Prepared("POST", "http://h/c/1", {"Content-Type": "text/plain"}, '{"a": 1}')
    m = diagnose(p, _out(405, {}, {"Allow": "GET, PUT"}), errors.parse(405, "{}", {}, {"Allow": "GET, PUT"}), _ctx(row))
    assert apply(p, m).method == "PUT"
    ct = diagnose(p, _out(415, {"message": "Content-Type must be application/json"}),
                  errors.parse(415, '{"message": "Content-Type must be application/json"}'), _ctx(row))
    assert apply(p, ct).headers["Content-Type"] == "application/json"
    o = _out(400, {"message": "Header 'X-Request-Id' is required"})
    h = diagnose(p, o, errors.parse(400, o.text, o.json_value), _ctx(row))
    assert len(apply(p, h).headers["X-Request-Id"]) == 36
    g = Prepared("GET", "http://h/rates", {}, None)
    o = _out(400, {"error": "currency query parameter is required"})
    q = diagnose(g, o, errors.parse(400, o.text, o.json_value), _ctx(Row(1, 2, "Rates", "GET", "http://h/rates"), sample={"currency": "USD"}))
    assert apply(g, q).url.endswith("?currency=USD")


def test_heal_duplicate_makes_fresh_data():
    row = Row(1, 2, "Register", "POST", "http://h/u")
    p = Prepared("POST", "http://h/u", {"Content-Type": "application/json"}, json.dumps({"name": "A B", "email": "a@example.com"}))
    o = _out(409, {"message": "email already exists"})
    patch = diagnose(p, o, errors.parse(409, o.text, o.json_value), _ctx(row))
    assert json.loads(apply(p, patch).body)["email"] != "a@example.com"


def test_heal_never_repeats_the_same_patch():
    row = Row(1, 2, "x", "POST", "http://h/x")
    p = Prepared("POST", "http://h/x", {}, '{"a": 1}')
    o = _out(400, {"error": "name is required"})
    ctx = _ctx(row)
    first = diagnose(p, o, errors.parse(400, o.text, o.json_value), ctx)
    assert first is not None and diagnose(p, o, errors.parse(400, o.text, o.json_value), ctx) is None


# ----------------------------------------------------------------------------- end to end on Demo Bank

@pytest.fixture(scope="module")
def bank():
    server = mock_server.start(18099)
    yield
    server.shutdown()
    server.server_close()


def run_sheet(path, tmp_path=None, **over):
    s = Settings()
    s.brain.mode = over.pop("mode", "rules")
    s.heal.mode = over.pop("heal", "auto")
    s.plugins_enabled = False
    sess = Session(s, ROOT, check_ai=False)
    sess.run_all(inputs.rows(inputs.load(path)))
    return sess


def test_messy_sheet_is_rescued(bank):
    mock_server.BANK.__init__()
    sess = run_sheet(MESSY)
    by = {r.name: r for r in sess.records}
    c = sess.counts()
    assert (c["PASSED"], c["HEALED"], c["FAILED"]) == (6, 5, 1)
    assert [r.name for r in sess.records][:3] == ["Health check", "Login", "My accounts"]      # Login pulled up
    assert by["Monthly statement"].result == "FAILED" and "never heals server errors" in by["Monthly statement"].explanation
    assert by["Login with a wrong password is rejected"].result == "PASSED" and not by["Login with a wrong password is rejected"].heal_attempts
    assert by["Update customer phone"].method == "PUT"
    assert len(by["Open a new customer"].heal_attempts) == 2                                   # auth, then phone


def test_brain_off_runs_rows_exactly_as_typed(bank):
    mock_server.BANK.__init__()
    sess = run_sheet(MESSY, mode="off")
    assert sess.counts()["FAILED"] == 12


def test_suggest_mode_reports_but_does_not_apply(bank):
    mock_server.BANK.__init__()
    sess = run_sheet(MESSY, heal="suggest")
    rates = next(r for r in sess.records if r.name == "USD exchange rate")
    assert rates.result == "FAILED" and "currency" in rates.suggestion


def test_corrected_sheet_works_even_without_the_brain(bank, tmp_path):
    mock_server.BANK.__init__()
    sess = run_sheet(MESSY)
    paths = sess.write(tmp_path / "run", MESSY.name)
    text = paths["fixed_csv"].read_text(encoding="utf-8-sig")
    assert "{{Login.access_token}}" in text and "{{$email}}" in text and "X-Request-Id: {{$uuid}}" in text
    mock_server.BANK.__init__()
    again = run_sheet(paths["fixed_csv"], mode="off")
    c = again.counts()
    assert c["PASSED"] == 11 and c["FAILED"] == 1, [(r.name, r.failures) for r in again.records if r.result == "FAILED"]


def test_reports_mention_the_brain(bank, tmp_path):
    mock_server.BANK.__init__()
    sess = run_sheet(MESSY)
    html = sess.write(tmp_path / "r", MESSY.name)["html"].read_text(encoding="utf-8")
    assert "Restora healed this request" in html and "In plain English" in html and "HEALED" in html
    assert "demo123" not in html                                                              # secrets stay hidden


# ----------------------------------------------------------------------------- plug-ins

def test_plugins_sign_and_generate(bank):
    mock_server.BANK.__init__()
    s = Settings()
    s.brain.mode = "rules"
    s.plugins_folder = str(ROOT / "examples" / "power_user" / "plugins")
    sess = Session(s, ROOT, check_ai=False)
    sess.run_all(inputs.rows(inputs.load(ROOT / "examples" / "power_user" / "beneficiaries.csv")))
    by = {r.name: r for r in sess.records}
    assert by["Add a beneficiary (signed by a plug-in)"].result == "PASSED"
    assert "Rent - Landlord" in by["Add a beneficiary (signed by a plug-in)"].request_body
    plugin.REGISTRY.clear()


# ----------------------------------------------------------------------------- cURL input

def test_curl_input(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("curl -X POST 'http://h/login' -H 'Content-Type: application/json' --data-raw '{\"u\":1}'\n"
                 "curl ^\"http://h/rates?c=USD^\" -H ^\"accept: application/json^\"\n", encoding="utf-8")
    loaded = inputs.load(p)
    assert loaded.kind == "curl" and [r["method"] for r in loaded.records] == ["POST", "GET"]
    assert loaded.records[1]["url"] == "http://h/rates?c=USD"


# ----------------------------------------------------------------------------- AI paths (with a fake model)

@pytest.fixture()
def fake_ai():
    from fake_llm import FakeLLM

    def answer(system, user, schema):
        if "Field name:" in user:
            return {"value": "Mumbai Fort branch", "type": "string"}
        if "smallest change" in user:
            return {"fixable": True, "diagnosis": "The API needs a channel.",
                    "changes": [{"where": "header", "action": "set", "name": "X-Channel", "value": "MOBILE", "type": "string"},
                                {"where": "header", "action": "set", "name": "Authorization", "value": "Bearer invented", "type": "string"}]}
        if "Write test rows" in user:
            return {"tests": [{"name": "Transfer with a negative amount is rejected", "method": "POST",
                               "url": "http://127.0.0.1:18099/api/v1/transfers", "headers": "Authorization: Bearer <token>",
                               "body": "{\"fromAccount\": \"<accountNumber>\", \"toAccount\": \"1\", \"amount\": -5}",
                               "expected_status": "400", "expected_response": ""}]}
        return {"explanation": "Something specific went wrong."}
    fake = FakeLLM(answer)
    yield fake
    fake.stop()


def test_ai_connect_and_value(fake_ai):
    from restora.brain.ai import connect
    s = Settings()
    s.ai.url = fake_ai.url
    ai = connect(s.ai)
    assert ai.ready and "qwen3:4b-instruct" in ai.message
    assert ai.suggest_value("branchName", Row(1, 2, "x", "POST", "http://h"), {}) == "Mumbai Fort branch"
    assert fake_ai.requests[-1][1]["format"]["required"] == ["value", "type"]          # constrained to a schema
    assert fake_ai.requests[-1][1].get("think") is False


def test_ai_heal_patch_drops_invented_credentials(fake_ai):
    from restora.brain.ai import connect
    s = Settings()
    s.ai.url = fake_ai.url
    ai = connect(s.ai)
    p = Prepared("GET", "http://h/x", {}, None)
    o = _out(400, {"error": "policy check failed"})
    patch = ai.heal_patch(p, o, Row(1, 2, "x", "GET", "http://h/x"), Memory())
    assert [c.name for c in patch.changes] == ["X-Channel"] and patch.source == "ai"


def test_ai_missing_model_falls_back_to_rules(fake_ai):
    from restora.brain.ai import connect
    s = Settings()
    s.ai.url = fake_ai.url
    s.ai.model = "not-installed:1b"
    ai = connect(s.ai)
    assert not ai.ready and "ollama pull not-installed:1b" in ai.message


def test_ai_writes_tests(fake_ai):
    from restora.brain.ai import connect
    s = Settings()
    s.ai.url = fake_ai.url
    ai = connect(s.ai)
    tests = ai.write_tests("a transfer with a negative amount is rejected", inputs.load(MESSY).records)
    assert tests[0]["expected_status"] == "400" and tests[0]["method"] == "POST"


def test_ai_is_used_only_when_rules_have_no_answer(bank, fake_ai):
    mock_server.BANK.__init__()
    s = Settings()
    s.brain.mode = "ai"
    s.ai.url = fake_ai.url
    s.plugins_enabled = False
    sess = Session(s, ROOT)
    assert sess.ai is not None
    sess.run_all(inputs.rows(inputs.load(MESSY)))
    c = sess.counts()
    assert (c["PASSED"], c["HEALED"], c["FAILED"]) == (6, 5, 1)
    tasks = [t for t, _, _ in sess.ai.log]
    assert "heal" not in tasks                                    # every heal on Demo Bank is solved by rules


def test_openai_compatible_server(fake_ai):
    from restora.brain.llm import LLMClient
    c = LLMClient("openai", fake_ai.url, "qwen3:4b-instruct")
    assert c.status()[0]
    assert c.chat_json("s", "Field name: x", {"type": "object"})["value"] == "Mumbai Fort branch"


def test_jsonpath_helpers():
    d = {"a": {"b": [1, {"c": 2}]}}
    assert jsonpath.get(d, jsonpath.parse("a.b[1].c")) == 2
    jsonpath.put(d, jsonpath.parse("a.new.x"), 5)
    assert d["a"]["new"]["x"] == 5 and jsonpath.delete(d, ["a", "new"])
    assert load_records([{"url": "http://h", "method": "GET"}]).rows[0].method == "GET"


def test_patch_apply_does_not_touch_the_original():
    p = Prepared("POST", "http://h/x", {"A": "1"}, '{"k": 1}')
    q = apply(p, Patch([Change("body", "set", "k", 2), Change("header", "set", "B", "2")], "r"))
    assert json.loads(p.body)["k"] == 1 and "B" not in p.headers and json.loads(q.body)["k"] == 2
