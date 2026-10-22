"""Unit tests for Restora's core (sheet, Postman, compare, masking).  Run:  python run.py --selftest"""
import base64
import json

import pytest

from demo import mock_server
from restora import compare, mask, postman
from restora.client import Settings, detect_content_type, prepare
from restora.loader import InputError, Row, load_rows, parse_headers
from restora.runner import execute
from restora.text import loads_lenient


# ----------------------------------------------------------------------------- headers / content type

def test_headers_lines_and_json():
    assert parse_headers("A: 1\nB: two:three") == {"A": "1", "B": "two:three"}
    assert parse_headers('{"A": 1}') == {"A": "1"}
    assert parse_headers("") == {}


def test_headers_bad_line():
    with pytest.raises(ValueError):
        parse_headers("nocolon")


@pytest.mark.parametrize("body,expected", [
    ('{"a":1}', "application/json"), ("[1,2]", "application/json"),
    ("<a/>", "application/xml"),
    ('<soap:Envelope xmlns:soap="x"></soap:Envelope>', "text/xml; charset=utf-8"),
    ("a=1&b=2", "application/x-www-form-urlencoded"), ("hello world", "text/plain"),
])
def test_content_type_detection(body, expected):
    assert detect_content_type(body) == expected


def test_prepare_fills_only_what_is_missing():
    row = Row(1, 2, "n", "POST", "http://x", headers={"content-type": "text/csv"}, body="a,b")
    p = prepare(row)
    assert p.headers["content-type"] == "text/csv" and "Content-Type" not in p.headers
    row2 = Row(1, 2, "n", "POST", "http://x", body='{"a":1}', expected_response='{"a":1}')
    p2 = prepare(row2)
    assert p2.headers["Content-Type"] == "application/json" and p2.headers["Accept"] == "application/json"
    assert p2.notes


# ----------------------------------------------------------------------------- masking

def test_masking():
    assert mask.mask_headers({"Authorization": "Bearer abc", "X": "1"}) == {"Authorization": "****", "X": "1"}
    masked = json.loads(mask.mask_text('{"user":"a","password":"p","nested":{"accessToken":"t"},"shipping":"s"}'))
    assert masked["password"] == "****" and masked["nested"]["accessToken"] == "****"
    assert masked["user"] == "a" and masked["shipping"] == "s"          # 'pin' inside 'shipping' is not a secret
    assert mask.mask_text("grant=x&password=p") == "grant=x&password=****"
    assert mask.is_sensitive_key("client_secret") and not mask.is_sensitive_key("tokenizer")


# ----------------------------------------------------------------------------- compare

def test_compare_exact_and_wildcard():
    assert compare.diff_json({"a": 1, "b": "*"}, {"b": None, "a": 1}) == []
    kinds = {d.kind for d in compare.diff_json({"a": 1, "b": 2}, {"a": 2, "c": 3})}
    assert kinds == {"different", "missing", "unexpected"}


def test_compare_wildcard_still_requires_key():
    assert [d.kind for d in compare.diff_json({"id": "*"}, {})] == ["missing"]


def test_compare_lists_types_and_bools():
    assert compare.diff_json([1, 2], [1, 2]) == []
    assert compare.diff_json([1, 2], [1])[0].kind == "length"
    assert compare.diff_json({"a": 1}, {"a": "1"})[0].kind == "type"
    assert compare.diff_json({"a": True}, {"a": 1})[0].kind in ("type", "different")
    assert compare.diff_json({"a": 1}, {"a": 1.0}) == []


def test_compare_hides_secret_values():
    text = compare.diff_json({"password": "x"}, {"password": "y"})[0].describe()
    assert "x" not in text.replace("password", "") and "y" not in text.replace("password", "").replace("hidden", "")


def test_status_rules():
    assert compare.check_status(None, 204) is None
    assert compare.check_status(None, 404) is not None
    assert compare.check_status(404, 404) is None
    assert compare.check_status(200, 201) is not None


# ----------------------------------------------------------------------------- loader

def write(tmp_path, text, name="s.csv", encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding)
    return p


def test_loader_aliases_and_defaults(tmp_path):
    p = write(tmp_path, 'Request Type,API URL,Request Body,Sample Response\n'
                        'POST,http://h/x,"{""a"":1}","{""ok"":true}"\n'
                        ',http://h/y,,\n')
    rows = load_rows(p)
    assert [r.method for r in rows] == ["POST", "GET"]          # blank method with no body -> GET
    assert rows[0].body == '{"a":1}' and rows[0].expected_response == '{"ok":true}'
    assert rows[1].name == "GET /y" and rows[1].sheet_row == 3


def test_loader_blank_method_with_body_is_post(tmp_path):
    rows = load_rows(write(tmp_path, 'URL,Body\nhttp://h/x,abc\n'))
    assert rows[0].method == "POST" and rows[0].notes


def test_loader_comment_rows_and_blank_lines(tmp_path):
    rows = load_rows(write(tmp_path, 'Method,URL\n#GET,http://h/a\n\nGET,http://h/b\n'))
    assert rows[0].skip and not rows[1].skip and len(rows) == 2


def test_loader_semicolons_and_cp1252(tmp_path):
    p = write(tmp_path, 'Method;URL;Body\nPOST;http://h/x;café\n', encoding="cp1252")
    assert load_rows(p)[0].body == "café"


def test_loader_bad_rows_fail_alone(tmp_path):
    rows = load_rows(write(tmp_path, 'Method,URL,ExpectedStatus\nGET,nope,\nGET,http://h/ok,abc\nGET,http://h/{{x}},\nGET,http://h/ok,200\n'))
    assert [bool(r.error) for r in rows] == [True, True, False, False]   # {{x}} is filled at run time now
    assert "http://" in rows[0].error and "status" in rows[1].error.lower()
    assert rows[0].problems == {"url": rows[0].error}                     # structured, so the brain can fix it


def test_loader_smart_quotes_fixed(tmp_path):
    body = "{“a”: “b”}"
    rows = load_rows(write(tmp_path, f'Method,URL,Body\nPOST,http://h/x,"{body}"\n'))
    assert json.loads(rows[0].body) == {"a": "b"} and any("smart" in n.lower() for n in rows[0].notes)


def test_loader_invalid_expected_json_is_an_error(tmp_path):
    rows = load_rows(write(tmp_path, 'Method,URL,ExpectedResponse\nGET,http://h/x,"{bad}"\n'))
    assert "not valid JSON" in rows[0].error


def test_loader_needs_url_column(tmp_path):
    with pytest.raises(InputError, match="URL column"):
        load_rows(write(tmp_path, "Method,Body\nGET,x\n"))


def test_loader_xlsx(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Method", "URL", "ExpectedStatus"])
    ws.append(["GET", "http://h/x", 200])
    p = tmp_path / "s.xlsx"
    wb.save(p)
    assert load_rows(p)[0].expected_status == 200


def test_lenient_json_only_fixes_quotes():
    assert loads_lenient("{“a”: 1}")[2] is True
    with pytest.raises(ValueError):
        loads_lenient("{a: 1}")


# ----------------------------------------------------------------------------- postman

def coll(items, **extra):
    return {"info": {"name": "T", "schema": "v2.1"}, "item": items, **extra}


def convert(tmp_path, data, env=None):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data))
    e = None
    if env is not None:
        e = tmp_path / "e.json"
        e.write_text(json.dumps(env))
    return postman.convert(p, e)


def req(name, method="GET", url="http://h/x", **kw):
    return {"name": name, "request": {"method": method, "url": url, **kw}}


def test_postman_basic_folders_and_headers(tmp_path):
    r = convert(tmp_path, coll([{"name": "F", "item": [req("A", header=[
        {"key": "X-A", "value": "1"}, {"key": "X-B", "value": "2", "disabled": True}])]}]))
    rec = r.records[0]
    assert rec["name"] == "F / A" and rec["headers"] == "X-A: 1"


def test_postman_variables_env_wins(tmp_path):
    data = coll([req("A", url="{{base}}/x?k={{k}}")], variable=[{"key": "base", "value": "http://c"}, {"key": "k", "value": "1"}])
    assert convert(tmp_path, data).records[0]["url"] == "http://c/x?k=1"
    env = {"values": [{"key": "base", "value": "http://env", "enabled": True}]}
    assert convert(tmp_path, data, env).records[0]["url"] == "http://env/x?k=1"


def test_postman_missing_variable_warns(tmp_path):
    r = convert(tmp_path, coll([req("A", url="{{nope}}/x")]))
    assert any("nope" in w for w in r.warnings) and "{{nope}}" in r.records[0]["url"]


def test_postman_bearer_inherited_and_overridden(tmp_path):
    auth = {"type": "bearer", "bearer": [{"key": "token", "value": "T1"}]}
    data = coll([{"name": "F", "item": [req("A"), {**req("B"), "auth": {"type": "noauth"}}]}], auth=auth)
    a, b = convert(tmp_path, data).records
    assert a["headers"] == "Authorization: Bearer T1" and b["headers"] == ""


def test_postman_basic_and_apikey_auth(tmp_path):
    data = coll([{**req("A"), "auth": {"type": "basic", "basic": [{"key": "username", "value": "u"}, {"key": "password", "value": "p"}]}},
                 {**req("B"), "auth": {"type": "apikey", "apikey": [{"key": "key", "value": "k"}, {"key": "value", "value": "v"}, {"key": "in", "value": "query"}]}}])
    a, b = convert(tmp_path, data).records
    assert a["headers"] == "Authorization: Basic " + base64.b64encode(b"u:p").decode()
    assert b["url"].endswith("?k=v")


def test_postman_unsupported_auth_warns(tmp_path):
    r = convert(tmp_path, coll([{**req("A"), "auth": {"type": "oauth2"}}]))
    assert any("oauth2" in w for w in r.warnings)


def test_postman_bodies(tmp_path):
    raw = req("R", "POST", body={"mode": "raw", "raw": '{"a":1}'})
    enc = req("E", "POST", body={"mode": "urlencoded", "urlencoded": [{"key": "a", "value": "x y"}]})
    gql = req("G", "POST", body={"mode": "graphql", "graphql": {"query": "{ me }", "variables": '{"v":1}'}})
    txt = req("T", "POST", body={"mode": "raw", "raw": '{"a":1}', "options": {"raw": {"language": "text"}}})
    r, e, g, t = convert(tmp_path, coll([raw, enc, gql, txt])).records
    assert r["body"] == '{"a":1}'
    assert e["body"] == "a=x+y" and "x-www-form-urlencoded" in e["headers"]
    assert json.loads(g["body"]) == {"query": "{ me }", "variables": {"v": 1}}
    assert t["headers"] == "Content-Type: text/plain"


def test_postman_formdata_is_commented_out(tmp_path):
    r = convert(tmp_path, coll([req("U", "POST", body={"mode": "formdata", "formdata": [{"key": "f", "type": "file"}]})]))
    assert r.records[0]["method"] == "#POST" and any("form-data" in w for w in r.warnings)


def test_postman_query_disabled_and_path_variables(tmp_path):
    url = {"raw": "http://h/users/:id?a=1&b=2", "query": [{"key": "a", "value": "1"}, {"key": "b", "value": "2", "disabled": True}],
           "variable": [{"key": "id", "value": "7"}]}
    assert convert(tmp_path, coll([req("A", url=url)])).records[0]["url"] == "http://h/users/7?a=1"


def test_postman_saved_example_and_scripts(tmp_path):
    item = {**req("A"), "response": [{"code": 201, "body": '{"id": 1}'}],
            "event": [{"listen": "test", "script": {"exec": ["pm.test('x')"]}}]}
    r = convert(tmp_path, coll([item]))
    assert r.records[0]["expected_status"] == "201" and r.records[0]["expected_response"] == '{"id": 1}'
    assert any("scripts" in w for w in r.warnings)


def test_postman_wrong_files(tmp_path):
    with pytest.raises(postman.PostmanError, match="ENVIRONMENT"):
        convert(tmp_path, {"values": []})
    with pytest.raises(postman.PostmanError, match="v1"):
        convert(tmp_path, {"requests": []})
    with pytest.raises(postman.PostmanError):
        convert(tmp_path, {"hello": 1})


def test_postman_csv_roundtrip(tmp_path):
    r = convert(tmp_path, coll([req("A", "POST", body={"mode": "raw", "raw": '{"a":"b,c"}\n'},
                                    header=[{"key": "H", "value": "v"}])]))
    csv_path = postman.write_csv(r.records, tmp_path / "o.csv")
    row = load_rows(csv_path)[0]
    assert row.method == "POST" and row.headers == {"H": "v"} and json.loads(row.body) == {"a": "b,c"}


# ----------------------------------------------------------------------------- end to end against the demo API

@pytest.fixture(scope="module")
def api():
    server = mock_server.start(18098)
    yield "http://127.0.0.1:18098"
    server.shutdown()
    server.server_close()


def run(api, **kw):
    kw.setdefault("name", "t")
    kw["url"] = api + kw.pop("path")
    kw.setdefault("method", "GET")
    return execute(Row(number=1, sheet_row=2, **kw), Settings(timeout=5))


def test_e2e_pass_and_fail(api):
    ok = run(api, path="/users/1", expected_status=200,
             expected_response='{"id":1,"name":"Asha Rao","email":"asha@example.com","active":true}')
    assert ok.result == "PASSED"
    bad = run(api, path="/users/1", expected_response='{"id":2}')
    assert bad.result == "FAILED" and len(bad.failures) >= 2


def test_e2e_auto_content_type_and_masking(api):
    r = run(api, path="/login", method="POST", body='{"username":"demo","password":"demo123"}',
            expected_response='{"token":"demo-token-123","expiresIn":3600}')
    assert r.result == "PASSED"
    assert "demo123" not in r.request_body and "demo-token-123" not in r.response_body
    assert "Content-Type" in r.request_headers


def test_e2e_auth_header_is_masked_in_report(api):
    r = run(api, path="/secure/profile", headers={"Authorization": "Bearer demo-token-123"}, expected_status=200)
    assert r.result == "PASSED" and r.request_headers["Authorization"] == "****" and "demo-token-123" not in r.curl


def test_e2e_unreachable_and_skip(api):
    r = execute(Row(1, 2, "n", "GET", "http://127.0.0.1:1/"), Settings(timeout=5))
    # Linux refuses at once; Windows retries a closed port for about 2 seconds before refusing
    assert r.result == "FAILED" and ("connect" in r.failures[0].lower() or "no response" in r.failures[0].lower())
    assert run(api, path="/x", skip="off").result == "SKIPPED"


def test_e2e_text_response(api):
    assert run(api, path="/ping", expected_response="pong").result == "PASSED"
    assert run(api, path="/ping", expected_response="peng").result == "FAILED"
