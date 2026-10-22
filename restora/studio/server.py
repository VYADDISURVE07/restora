"""Restora Studio: a local web app for people who would rather click than type commands.

Runs on this computer only (127.0.0.1), needs nothing beyond Python's standard library, and works offline.
    python run.py --studio        (or double-click studio.bat)
"""
from __future__ import annotations

import base64
import copy
import datetime
import json
import mimetypes
import re
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__, inputs
from ..brain import fixed_sheet
from ..loader import CANONICAL, InputError, load_records
from ..mask import mask_headers, mask_text
from ..session import Session
from ..settings import Settings
from ..status import shown

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
EXAMPLES = {"demo_bank_messy": ROOT / "examples" / "demo_bank_messy.csv",
            "power_user": ROOT / "examples" / "power_user" / "beneficiaries.csv"}


def _normal_body(text: str) -> str:
    """A body as the brain sees it (repaired JSON, one line) - so tracked changes show meaning, not quotes."""
    if not text or text.lstrip()[:1] not in "{[":
        return text or ""
    from ..brain.repair import RepairFailed, repair_json
    try:
        value = repair_json(text)[0]
        return json.dumps(value, ensure_ascii=False)
    except RepairFailed:
        return text


def _cells(rec: dict) -> dict:
    return {k: str(rec.get(k, "") or "") for k in CANONICAL}


def _record_json(r) -> dict:
    fixed = fixed_sheet.build_row(r) if r.fix else {}
    row = (r.fix or {}).get("row")
    original = dict(row.original) if row is not None and row.original else {}
    return {
        "number": r.number, "position": r.position, "name": r.name, "result": r.result, "method": r.method, "url": r.url,
        "http_status": r.http_status, "expected_status": shown(r.expected_status), "elapsed_ms": round(r.elapsed_ms),
        "failures": r.failures, "explanation": r.explanation, "suggestion": r.suggestion, "negative": r.negative,
        "filled": r.filled, "heal_attempts": r.heal_attempts, "dynamic": r.dynamic, "planned": r.planned,
        "notes": r.notes, "request_headers": r.request_headers, "request_body": r.request_body,
        "response_headers": r.response_headers, "response_body": r.response_body, "curl": r.curl,
        "original": {**original, "body": _normal_body(original.get("body", ""))} if original else {},
        "corrected": _normal_corrected(fixed),
    }


def _normal_corrected(fixed: dict) -> dict:
    """The corrected row as the grid compares it with the original: both bodies normalised the same way."""
    out = {k.lower(): v for k, v in (fixed or {}).items()}
    if out.get("body"):
        out["body"] = _normal_body(out["body"])
    return out


class Run:
    def __init__(self, run_id: str):
        self.id = run_id
        self.events: list = []
        self.done = False
        self.result: dict = {}
        self.lock = threading.Lock()
        self.dir: Path | None = None

    def add(self, kind: str, **data):
        with self.lock:
            self.events.append({"seq": len(self.events), "type": kind, "time": time.time(), **data})


class Studio:
    def __init__(self, settings: Settings | None = None):
        self.base_settings = settings or Settings()
        self.runs: dict = {}
        self.demo = None
        self.ai_status = {"ready": False, "message": "Checking for a local AI model...", "model": self.base_settings.ai.model,
                          "models": []}
        self._ai = None
        threading.Thread(target=self.check_ai, daemon=True).start()

    # ------------------------------------------------------------------ helpers
    def check_ai(self, model: str | None = None) -> dict:
        from ..brain.ai import connect
        s = self.base_settings
        if model:
            s.ai.model = model
        ai = connect(s.ai, s.ai_key)
        self._ai = ai if ai.ready else None
        self.ai_status = {"ready": ai.ready, "message": ai.message, "model": s.ai.model,
                          "models": [m for m in (ai.models or []) if m], "provider": s.ai.provider}
        return self.ai_status

    def start_demo(self) -> str:
        from demo import mock_server
        if self.demo is None:
            try:
                self.demo = mock_server.start()
            except OSError:
                self.demo = "external"          # something (probably another Restora) already serves the port
        return f"http://127.0.0.1:{mock_server.PORT}"

    def settings_for(self, opts: dict) -> Settings:
        s = copy.deepcopy(self.base_settings)
        if opts.get("brain") in ("ai", "rules", "off"):
            s.brain.mode = opts["brain"]
        if opts.get("heal") in ("auto", "suggest", "off"):
            s.heal.mode = opts["heal"]
        if "strict" in opts:
            s.brain.smart_compare = not bool(opts["strict"])
        if "reorder" in opts:
            s.brain.reorder = bool(opts["reorder"])
        if opts.get("seed"):
            s.brain.seed = int(opts["seed"])
        return s

    # ------------------------------------------------------------------ API
    def plan(self, records: list, opts: dict) -> dict:
        s = self.settings_for(opts)
        sess = Session(s, ROOT, check_ai=False)
        rows = load_records(records).rows
        index = {r.number: i for i, r in enumerate(rows)}
        ordered = sess.prepare(rows)
        out_rows = []
        for r in ordered:
            needs = [{"raw": n.raw, "kind": n.kind, "name": n.name, "where": n.where, "key": n.key} for n in r.brain.get("needs", [])]
            out_rows.append({
                "number": r.number, "index": index[r.number], "position": r.brain.get("position", r.number),
                "name": r.name, "method": r.method, "url": r.url, "body": r.body or "",
                "planned": r.brain.get("planned", []), "negative": r.brain.get("negative"), "needs": needs,
                "error": r.error, "skip": r.skip, "moved": r.number in (sess.plan.moved or {}),
                "original": {**(r.original or {}), "body": _normal_body((r.original or {}).get("body", ""))},
            })
        return {"rows": out_rows, "insights": [i.as_dict() for i in sess.plan.insights], "mode": s.brain.mode}

    def start_run(self, records: list, opts: dict, source_name: str) -> str:
        run = Run(uuid.uuid4().hex[:10])
        self.runs[run.id] = run
        s = self.settings_for(opts)
        if opts.get("demo"):
            self.start_demo()

        def worker():
            try:
                def on_event(kind, row=None, **kw):
                    data = {"number": row.number if row is not None else None}
                    if kind == "filled":
                        data["fills"] = kw.get("fills", [])
                    elif kind == "healing":
                        data["status"] = kw.get("status")
                    elif kind in ("healed", "heal_failed"):
                        data["attempts"] = kw.get("attempts", [])
                    elif kind == "done":
                        data["record"] = _record_json(kw["record"])
                    run.add(kind, **data)
                sess = Session(s, ROOT, on_event=on_event, check_ai=(s.brain.mode == "ai"))
                run.add("ai", message=sess.ai_message, ready=sess.ai is not None)
                rows = load_records(records).rows
                ordered = sess.prepare(rows)
                run.add("plan", order=[r.number for r in ordered], insights=[i.as_dict() for i in sess.plan.insights])
                pace = min(max(float(opts.get("pace") or 0), 0.0), 3000.0) / 1000.0   # presentations: let people follow
                for r in ordered:
                    if pace:
                        time.sleep(pace)
                    sess.run_row(r)
                stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", Path(source_name or "studio").stem)[:40] or "studio"
                run.dir = ROOT / "reports" / f"studio_{stem}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
                suffix = Path(source_name or "x.csv").suffix or ".csv"
                paths = sess.write(run.dir, f"{stem}{suffix if suffix in ('.xlsx', '.xlsm') else '.csv'}",
                                   suffix if suffix in (".xlsx", ".xlsm") else ".csv")
                c = sess.counts()
                run.result = {"counts": {k: c.get(k, 0) for k in ("PASSED", "HEALED", "FAILED", "SKIPPED")},
                              "brain": sess.brain_summary(), "files": {k: Path(v).name for k, v in paths.items()},
                              "exit_code": sess.exit_code()}
                run.add("finished", **run.result)
            except Exception as e:  # noqa: BLE001 - show the problem in the app instead of a silent stop
                run.add("error", message=f"{type(e).__name__}: {e}")
            finally:
                run.done = True
        threading.Thread(target=worker, daemon=True).start()
        return run.id

    def ask(self, text: str, records: list) -> dict:
        if self._ai is None:
            self.check_ai()
        if self._ai is None:
            return {"ok": False, "message": self.ai_status["message"] + " Writing tests from plain English needs the local AI model."}
        self._ai.calls_left = max(self._ai.calls_left, 5)
        tests = self._ai.write_tests(text, records)
        if not tests:
            return {"ok": False, "message": "The AI model did not write a usable test. Try describing it more precisely."}
        return {"ok": True, "tests": tests, "seconds": round(self._ai.client.seconds, 1)}


def _handler(studio: Studio):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"RestoraStudio/{__version__}"

        def log_message(self, *args):
            pass

        def _send(self, code: int, payload, ctype: str = "application/json", headers: dict | None = None):
            data = payload if isinstance(payload, bytes) else (
                payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload, default=str).encode("utf-8"))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 30 * 1024 * 1024:
                raise ValueError("That file is too large for Studio (30 MB).")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def _file(self, path: Path, download: str | None = None):
            if not path.is_file():
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix == ".woff2":
                ctype = "font/woff2"
            headers = {"Content-Disposition": f'attachment; filename="{download}"'} if download else None
            self._send(200, path.read_bytes(), ctype, headers)

        # ---------------------------------------------------------- GET
        def do_GET(self):
            url = urlparse(self.path)
            p = url.path
            if p in ("/", "/index.html"):
                return self._file(STATIC / "index.html")
            if p.startswith("/static/"):
                target = (STATIC / p[len("/static/"):]).resolve()
                if STATIC.resolve() in target.parents:
                    return self._file(target)
                return self._send(404, {"error": "not found"})
            if p == "/api/status":
                q = parse_qs(url.query)
                if q.get("recheck"):
                    studio.check_ai()
                return self._send(200, {"version": __version__, "ai": studio.ai_status,
                                        "settings": {"brain": studio.base_settings.brain.mode,
                                                     "heal": studio.base_settings.heal.mode},
                                        "examples": list(EXAMPLES)})
            m = re.fullmatch(r"/api/run/([a-f0-9]+)/events", p)
            if m:
                run = studio.runs.get(m.group(1))
                if run is None:
                    return self._send(404, {"error": "unknown run"})
                after = int((parse_qs(url.query).get("after") or ["-1"])[0])
                with run.lock:
                    events = [e for e in run.events if e["seq"] > after]
                return self._send(200, {"events": events, "done": run.done})
            m = re.fullmatch(r"/runs/([a-f0-9]+)/(report\.html|corrected\.csv|corrected\.xlsx|results\.csv)", p)
            if m:
                run = studio.runs.get(m.group(1))
                if run is None or run.dir is None:
                    return self._send(404, {"error": "run not finished"})
                name = m.group(2)
                files = run.result.get("files", {})
                key = {"report.html": "html", "corrected.csv": "fixed_csv", "corrected.xlsx": "fixed_xlsx", "results.csv": "csv"}[name]
                if key not in files:
                    return self._send(404, {"error": "not available"})
                return self._file(run.dir / files[key], None if name == "report.html" else files[key])
            if p == "/favicon.ico":
                return self._file(STATIC / "logo.svg")
            return self._send(404, {"error": "not found"})

        # ---------------------------------------------------------- POST
        def do_POST(self):
            if self.headers.get("X-Restora") != "1":           # only Studio's own page may call the API
                return self._send(403, {"error": "forbidden"})
            p = urlparse(self.path).path
            try:
                body = self._body()
                if p == "/api/import":
                    name = Path(str(body.get("filename") or "upload.csv")).name
                    data = base64.b64decode(body.get("data") or "")
                    up = ROOT / "reports" / "_uploads"
                    up.mkdir(parents=True, exist_ok=True)
                    target = up / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}"
                    target.write_bytes(data)
                    loaded = inputs.load(target)
                    return self._send(200, {"kind": loaded.kind, "name": name, "records": [_cells(r) for r in loaded.records],
                                            "warnings": loaded.warnings})
                if p == "/api/paste":
                    loaded = inputs.from_text(str(body.get("text") or ""))
                    if not loaded.records:
                        return self._send(400, {"error": "No cURL commands or CSV rows were found in the pasted text."})
                    return self._send(200, {"kind": loaded.kind, "name": "pasted", "records": [_cells(r) for r in loaded.records],
                                            "warnings": loaded.warnings})
                if p == "/api/example":
                    key = str(body.get("name") or "demo_bank_messy")
                    path = EXAMPLES.get(key)
                    if path is None:
                        return self._send(404, {"error": "unknown example"})
                    url = studio.start_demo()
                    loaded = inputs.load(path)
                    return self._send(200, {"kind": "example", "name": path.name, "records": [_cells(r) for r in loaded.records],
                                            "warnings": [], "demo_url": url})
                if p == "/api/demo":
                    return self._send(200, {"url": studio.start_demo()})
                if p == "/api/plan":
                    return self._send(200, studio.plan(body.get("records") or [], body.get("options") or {}))
                if p == "/api/run":
                    rid = studio.start_run(body.get("records") or [], body.get("options") or {}, str(body.get("source") or ""))
                    return self._send(200, {"run_id": rid})
                if p == "/api/ask":
                    return self._send(200, studio.ask(str(body.get("text") or ""), body.get("records") or []))
                if p == "/api/ai":
                    return self._send(200, studio.check_ai(str(body.get("model") or "") or None))
                return self._send(404, {"error": "not found"})
            except InputError as e:
                return self._send(400, {"error": str(e)})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e)})

    return Handler


def serve(port: int = 8765, open_browser: bool = True, settings: Settings | None = None, demo: bool = False,
          host: str = "127.0.0.1") -> int:
    studio = Studio(settings)
    if demo:
        studio.start_demo()
    try:
        server = ThreadingHTTPServer((host, port), _handler(studio))
    except OSError:
        print(f"Port {port} is busy - is Studio already open? Try:  python run.py --studio --port {port + 1}")
        return 2
    url = f"http://{host}:{port}/"
    print(f"Restora Studio is running at {url}\nLeave this window open while you use it. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def start_in_background(port: int = 0, settings: Settings | None = None):
    """For tests and demos: (server, url) with Studio running on a background thread."""
    studio = Studio(settings)
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(studio))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/", studio


__all__ = ["serve", "start_in_background", "mask_headers", "mask_text"]
