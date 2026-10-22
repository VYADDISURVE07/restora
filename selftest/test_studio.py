"""Studio's web API, end to end (no browser needed)."""
import json
import time
import urllib.request

import pytest

from restora.settings import Settings
from restora.studio.server import start_in_background


def call(url, path, body=None, header=True):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if header:
        req.add_header("X-Restora", "1")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read()
            return r.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture(scope="module")
def studio():
    s = Settings()
    s.brain.mode = "rules"
    s.ai.url = "http://127.0.0.1:9"            # nothing listens there: Studio must cope without AI
    server, url, st = start_in_background(0, s)
    yield url
    server.shutdown()
    if st.demo not in (None, "external"):
        st.demo.shutdown()


def test_pages_and_status(studio):
    code, html = call(studio, "/")
    assert code == 200 and b"Restora Studio" in html
    code, st = call(studio, "/api/status")
    assert code == 200 and st["version"] and "ai" in st


def test_api_needs_the_studio_header(studio):
    code, _ = call(studio, "/api/plan", {"records": []}, header=False)
    assert code == 403


def test_demo_plan_and_run(studio):
    code, ex = call(studio, "/api/example", {"name": "demo_bank_messy"})
    assert code == 200 and len(ex["records"]) == 12
    code, plan = call(studio, "/api/plan", {"records": ex["records"], "options": {"brain": "rules"}})
    assert code == 200 and plan["rows"][1]["name"] == "Login" and plan["rows"][1]["moved"]
    code, run = call(studio, "/api/run", {"records": ex["records"], "options": {"brain": "rules"}, "source": "demo.csv"})
    rid = run["run_id"]
    events, after = [], -1
    for _ in range(150):
        code, data = call(studio, f"/api/run/{rid}/events?after={after}")
        events += data["events"]
        if data["events"]:
            after = data["events"][-1]["seq"]
        if data["done"]:
            break
        time.sleep(0.1)
    fin = [e for e in events if e["type"] == "finished"][0]
    assert fin["counts"] == {"PASSED": 6, "HEALED": 5, "FAILED": 1, "SKIPPED": 0}
    done = {e["record"]["name"]: e["record"] for e in events if e["type"] == "done"}
    assert "{{Login.access_token}}" in done["My accounts"]["corrected"]["headers"]
    code, csv_bytes = call(studio, f"/runs/{rid}/corrected.csv")
    assert code == 200 and b"RestoraNotes" in csv_bytes
    code, report = call(studio, f"/runs/{rid}/report.html")
    assert code == 200 and b"Restora report" in report


def test_paste_curl(studio):
    code, data = call(studio, "/api/paste", {"text": "curl https://api.example.com/health\ncurl -X POST https://api.example.com/x -d 'a=1'"})
    assert code == 200 and [r["method"] for r in data["records"]] == ["GET", "POST"]


def test_ask_without_ai_explains_what_is_needed(studio):
    code, data = call(studio, "/api/ask", {"text": "login fails", "records": []})
    assert code == 200 and not data["ok"] and "AI" in data["message"]
