"""A fake API so you can try Restora on any machine, with no internet and no real system.

Started automatically by:  python run.py --demo   (and by Studio's "Use the demo API" button)
Or on its own:             python demo/mock_server.py

Two parts:
  /health, /users, /login, /echo ...   the small original demo
  /api/v1/...                          "Demo Bank", a fictional banking API whose endpoints report errors in
                                       different styles (Spring, FastAPI/Pydantic, Express, Rails), like real
                                       systems do - so you can watch the brain read them.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import re
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 18099
TOKEN = "demo-token-123"
SIGNING_KEY = b"demo-signing-key"          # /api/v1/beneficiaries wants X-Signature = HMAC-SHA256(body) in hex
USERS = {
    1: {"id": 1, "name": "Asha Rao", "email": "asha@example.com", "active": True},
    2: {"id": 2, "name": "Ravi Iyer", "email": "ravi@example.com", "active": True},
    3: {"id": 3, "name": "Neha Kulkarni", "email": "neha@example.com", "active": False},
}
RATES = {"USD": 0.012, "EUR": 0.011, "GBP": 0.0095, "AED": 0.044}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _jwt(sub: str) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=").decode()
    head, body = b64({"alg": "HS256", "typ": "JWT"}), b64({"sub": sub, "iat": int(datetime.datetime.now().timestamp()), "jti": secrets.token_hex(6)})
    sig = base64.urlsafe_b64encode(hmac.new(SIGNING_KEY, f"{head}.{body}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{head}.{body}.{sig}"


class Bank:
    """Demo Bank state (in memory, reset every time the server starts)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tokens: set = set()
        self.customers: dict = {"C1001": {"customerId": "C1001", "name": "Asha Rao", "email": "asha@example.com",
                                          "phone": "9820012345", "kycStatus": "VERIFIED", "createdAt": "2025-01-15T09:30:00Z"}}
        self.accounts: dict = {"500010001234": {"accountNumber": "500010001234", "ifsc": "DEMO0001234", "customerId": "C1001",
                                                "accountType": "SAVINGS", "balance": 25000.0, "currency": "INR",
                                                "openedAt": "2025-01-15T09:35:00Z"}}
        self.transfers: dict = {}
        self.requests_seen: dict = {}
        self.beneficiaries: dict = {}
        self.next_customer = 1001
        self.next_account = 10001234


BANK = Bank()


class Handler(BaseHTTPRequestHandler):
    next_id = 100
    server_version = "DemoBank/1.0"

    def log_message(self, *args):          # keep the console quiet
        pass

    # ---- helpers
    def _send(self, code, payload, content_type="application/json", headers=None):
        data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        data = data.encode("utf-8") if isinstance(data, str) else data
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _raw(self) -> bytes:
        if not hasattr(self, "_raw_cache"):
            n = int(self.headers.get("Content-Length") or 0)
            self._raw_cache = self.rfile.read(n) if n else b""
        return self._raw_cache

    def _body(self) -> str:
        return self._raw().decode("utf-8", errors="replace")

    def _json_body(self):
        try:
            return json.loads(self._body())
        except ValueError:
            return None

    # ================================================================== original small demo
    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/v1/"):
            return self._bank("GET", url)
        path, query = url.path, parse_qs(url.query)
        if path == "/health":
            return self._send(200, {"status": "UP"})
        if path == "/ping":
            return self._send(200, "pong", "text/plain")
        if path == "/users":
            users = list(USERS.values())
            if query.get("active") == ["true"]:
                users = [u for u in users if u["active"]]
            return self._send(200, {"count": len(users), "users": [{"id": u["id"], "name": u["name"]} for u in users]})
        if path.startswith("/users/"):
            try:
                user = USERS.get(int(path.rsplit("/", 1)[1]))
            except ValueError:
                user = None
            return self._send(200, user) if user else self._send(404, {"error": "user not found"})
        if path == "/secure/profile":
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"error": "missing or invalid token"})
            return self._send(200, {"username": "demo", "role": "tester"})
        self._send(404, {"error": "no such endpoint"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/v1/"):
            return self._bank("POST", url)
        path = url.path
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if path == "/users":
            body = self._json_body()
            if not isinstance(body, dict) or not body.get("name"):
                return self._send(400, {"error": "name is required"})
            Handler.next_id += 1
            return self._send(201, {"id": Handler.next_id, "name": body["name"], "email": body.get("email"),
                                    "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        if path == "/login":
            body = self._json_body() or {}
            if body.get("username") == "demo" and body.get("password") == "demo123":
                return self._send(200, {"token": TOKEN, "expiresIn": 3600})
            return self._send(401, {"error": "invalid credentials"})
        if path == "/echo":
            raw = self._body()
            if ctype == "application/json":
                try:
                    received = json.loads(raw)
                except ValueError:
                    return self._send(400, {"error": "body is not valid JSON"})
            elif ctype == "application/x-www-form-urlencoded":
                received = {k: v[0] for k, v in parse_qs(raw).items()}
            else:
                received = raw
            return self._send(200, {"contentType": ctype, "received": received})
        if path == "/xml":
            return self._send(200, "<?xml version=\"1.0\"?><result><ok>true</ok></result>", "text/xml")
        self._send(404, {"error": "no such endpoint"})

    def do_PUT(self):
        return self._bank("PUT", urlparse(self.path))

    def do_PATCH(self):
        return self._bank("PATCH", urlparse(self.path))

    def do_DELETE(self):
        return self._bank("DELETE", urlparse(self.path))

    # ================================================================== Demo Bank (fictional)
    def _authorised(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        return auth.startswith("Bearer ") and auth[7:].strip() in BANK.tokens

    def _need_auth(self):
        return self._send(401, {"error": "unauthorized", "message": "Missing or invalid bearer token"},
                          headers={"WWW-Authenticate": 'Bearer realm="demo-bank"'})

    def _need_json(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send(415, {"error": "unsupported_media_type", "message": "Content-Type must be application/json"})
            return None
        body = self._json_body()
        if not isinstance(body, dict):
            self._send(400, {"error": "invalid_json", "message": "Request body is not valid JSON"})
            return None
        return body

    def _bank(self, method: str, url):
        path = url.path.rstrip("/")
        query = parse_qs(url.query)
        with BANK.lock:
            if path == "/api/v1/health" and method == "GET":
                return self._send(200, {"status": "UP", "service": "demo-bank", "time": _now()})
            if path == "/api/v1/auth/login":
                return self._login() if method == "POST" else self._not_allowed("POST")
            if path == "/api/v1/rates" and method == "GET":
                return self._rates(query)
            if not self._authorised():
                return self._need_auth()
            if path == "/api/v1/accounts/me" and method == "GET":
                c = BANK.customers["C1001"]
                accts = [{"accountNumber": a["accountNumber"], "type": a["accountType"], "balance": a["balance"],
                          "currency": a["currency"]} for a in BANK.accounts.values() if a["customerId"] == "C1001"]
                return self._send(200, {"customerId": c["customerId"], "name": c["name"], "accounts": accts})
            if path == "/api/v1/customers":
                return self._create_customer() if method == "POST" else self._not_allowed("POST")
            m = re.fullmatch(r"/api/v1/customers/([^/]+)", path)
            if m:
                return self._customer(method, m.group(1))
            if path == "/api/v1/accounts":
                return self._open_account() if method == "POST" else self._not_allowed("POST")
            if path == "/api/v1/transfers":
                return self._transfer() if method == "POST" else self._not_allowed("POST")
            m = re.fullmatch(r"/api/v1/transfers/([^/]+)", path)
            if m and method == "GET":
                t = BANK.transfers.get(m.group(1))
                return self._send(200, t) if t else self._send(404, {"error": "not_found", "message": f"Transfer {m.group(1)} not found"})
            if path == "/api/v1/statements" and method == "GET":
                return self._send(500, {"error": "internal_error", "message": "Unexpected error while generating the statement",
                                        "traceId": str(uuid.uuid4())})
            if path == "/api/v1/beneficiaries":
                return self._beneficiary() if method == "POST" else self._not_allowed("POST")
        return self._send(404, {"error": "not_found", "message": f"No endpoint {method} {url.path}"})

    def _not_allowed(self, allow: str):
        return self._send(405, {"error": "method_not_allowed", "message": f"Use {allow}"}, headers={"Allow": allow})

    def _login(self):
        body = self._need_json()
        if body is None:
            return
        missing = [f for f in ("username", "password") if not body.get(f)]
        if missing:
            return self._send(400, {"errors": [{"field": f, "message": f"{f} is required"} for f in missing]})
        if body.get("username") == "demo" and body.get("password") == "demo123":
            tok = _jwt("demo")
            BANK.tokens.add(tok)
            return self._send(200, {"access_token": tok, "token_type": "Bearer", "expires_in": 900})
        return self._send(401, {"error": "invalid_credentials", "message": "Username or password is incorrect"})

    def _rates(self, query):
        cur = (query.get("currency") or [""])[0].upper()
        if not cur:
            return self._send(400, {"error": "currency query parameter is required"})
        if cur not in RATES:
            return self._send(400, {"error": f"currency must be one of {', '.join(RATES)}"})
        return self._send(200, {"base": "INR", "currency": cur, "rate": RATES[cur], "asOf": _now()})

    # Spring-style validation errors
    def _create_customer(self):
        body = self._need_json()
        if body is None:
            return
        errors = []
        if not str(body.get("name") or "").strip():
            errors.append({"field": "name", "defaultMessage": "must not be blank"})
        email = str(body.get("email") or "").strip()
        if not email:
            errors.append({"field": "email", "defaultMessage": "must not be blank"})
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email):
            errors.append({"field": "email", "defaultMessage": "must be a well-formed email address"})
        phone = re.sub(r"[\s\-]", "", str(body.get("phone") or ""))
        if not phone:
            errors.append({"field": "phone", "defaultMessage": "must not be blank"})
        elif not re.fullmatch(r"(\+91)?[6-9]\d{9}", phone):
            errors.append({"field": "phone", "defaultMessage": "must be a valid 10-digit mobile number"})
        if body.get("pan") and not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", str(body["pan"])):
            errors.append({"field": "pan", "defaultMessage": "is invalid"})
        if errors:
            return self._send(400, {"timestamp": _now(), "status": 400, "error": "Bad Request",
                                    "message": "Validation failed", "errors": errors})
        if any(c["email"].lower() == email.lower() for c in BANK.customers.values()):
            return self._send(409, {"error": "conflict", "message": "A customer with this email already exists"})
        BANK.next_customer += 1
        cid = f"C{BANK.next_customer}"
        c = {"customerId": cid, "name": body["name"].strip(), "email": email, "phone": phone, "kycStatus": "PENDING",
             "createdAt": _now()}
        BANK.customers[cid] = c
        return self._send(201, c, headers={"Location": f"/api/v1/customers/{cid}"})

    def _customer(self, method: str, cid: str):
        c = BANK.customers.get(cid)
        if method in ("POST", "PATCH"):
            return self._send(405, {"error": "method_not_allowed", "message": "Use PUT to update a customer"},
                              headers={"Allow": "GET, PUT, DELETE"})
        if c is None:
            return self._send(404, {"error": "not_found", "message": f"Customer {cid} not found"})
        if method == "GET":
            return self._send(200, c)
        if method == "PUT":
            body = self._need_json()
            if body is None:
                return
            for k in ("name", "email", "phone"):
                if body.get(k):
                    c[k] = body[k]
            c["updatedAt"] = _now()
            return self._send(200, c)
        if method == "DELETE":
            del BANK.customers[cid]
            return self._send(204, b"")
        return self._not_allowed("GET, PUT, DELETE")

    # FastAPI / Pydantic-style validation errors
    def _open_account(self):
        body = self._need_json()
        if body is None:
            return
        detail = []
        for f in ("customerId", "accountType", "initialDeposit"):
            if f not in body or body[f] in (None, ""):
                detail.append({"type": "missing", "loc": ["body", f], "msg": "Field required", "input": body})
        at = body.get("accountType")
        if at not in (None, "") and at not in ("SAVINGS", "CURRENT"):
            detail.append({"type": "enum", "loc": ["body", "accountType"], "msg": "Input should be 'SAVINGS' or 'CURRENT'",
                           "input": at, "ctx": {"expected": "'SAVINGS' or 'CURRENT'"}})
        dep = body.get("initialDeposit")
        if dep not in (None, ""):
            if isinstance(dep, bool) or not isinstance(dep, (int, float)):
                detail.append({"type": "float_parsing", "loc": ["body", "initialDeposit"],
                               "msg": "Input should be a valid number, unable to parse string as a number", "input": dep})
            elif dep < 1000:
                detail.append({"type": "greater_than_equal", "loc": ["body", "initialDeposit"],
                               "msg": "Input should be greater than or equal to 1000", "input": dep, "ctx": {"ge": 1000}})
        if detail:
            return self._send(422, {"detail": detail})
        if body["customerId"] not in BANK.customers:
            return self._send(404, {"detail": f"Customer {body['customerId']} not found"})
        BANK.next_account += 1
        num = f"5000{BANK.next_account:08d}"
        a = {"accountNumber": num, "ifsc": "DEMO0001234", "customerId": body["customerId"], "accountType": at,
             "balance": float(dep), "currency": "INR", "openedAt": _now()}
        BANK.accounts[num] = a
        return self._send(201, a)

    # Express-style plain messages
    def _transfer(self):
        rid = self.headers.get("X-Request-Id")
        if not rid:
            return self._send(400, {"code": "MISSING_HEADER", "message": "Header 'X-Request-Id' is required for payments"})
        body = self._need_json()
        if body is None:
            return
        if rid in BANK.requests_seen:
            return self._send(200, BANK.transfers[BANK.requests_seen[rid]])
        for f in ("fromAccount", "toAccount", "amount"):
            if body.get(f) in (None, ""):
                return self._send(400, {"message": f"{f} is required"})
        amount = body["amount"]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return self._send(400, {"message": "amount must be a number"})
        if amount <= 0:
            return self._send(400, {"message": "amount must be greater than 0"})
        if body.get("currency", "INR") != "INR":
            return self._send(400, {"message": "currency must be one of [INR]"})
        src = BANK.accounts.get(str(body["fromAccount"]))
        if src is None:
            return self._send(404, {"message": f"Account {body['fromAccount']} not found"})
        if src["balance"] < amount:
            return self._send(422, {"message": "Insufficient balance"})
        src["balance"] -= amount
        t = {"transferId": str(uuid.uuid4()), "status": "COMPLETED", "fromAccount": src["accountNumber"],
             "toAccount": str(body["toAccount"]), "amount": amount, "currency": "INR",
             "remarks": body.get("remarks", ""), "createdAt": _now()}
        BANK.transfers[t["transferId"]] = t
        BANK.requests_seen[rid] = t["transferId"]
        return self._send(201, t)

    # Rails-style field error map + a signature only a plug-in can make
    def _beneficiary(self):
        sig = self.headers.get("X-Signature") or ""
        expected = hmac.new(SIGNING_KEY, self._raw(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return self._send(401, {"error": "invalid_signature", "message": "X-Signature header missing or invalid"})
        body = self._need_json()
        if body is None:
            return
        errors = {}
        if not body.get("name"):
            errors["name"] = ["can't be blank"]
        if not re.fullmatch(r"\d{9,18}", str(body.get("accountNumber") or "")):
            errors["accountNumber"] = ["must be 9 to 18 digits"]
        if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", str(body.get("ifsc") or "")):
            errors["ifsc"] = ["Invalid IFSC code"]
        if errors:
            return self._send(400, {"errors": errors})
        bid = f"B{len(BANK.beneficiaries) + 501}"
        b = {"beneficiaryId": bid, "name": body["name"], "accountNumber": str(body["accountNumber"]),
             "ifsc": body["ifsc"], "status": "ACTIVE"}
        BANK.beneficiaries[bid] = b
        return self._send(201, b)


def start(port: int = PORT) -> ThreadingHTTPServer:
    """Start the fake API in a background thread and return the server (call .shutdown() to stop)."""
    global BANK
    BANK = Bank()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Demo API running on http://127.0.0.1:{PORT}  (Demo Bank at /api/v1)  - Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
