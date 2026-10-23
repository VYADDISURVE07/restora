# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Turn a Postman collection (v2.0 / v2.1 JSON export) into the Lite sheet format.

What is carried over : method, URL, enabled headers, raw / urlencoded / GraphQL bodies,
                       bearer / basic / API-key auth (inherited from folder or collection),
                       {{variables}} from the collection and from an optional environment file,
                       and the first *saved example response* of each request (as ExpectedStatus + ExpectedResponse).
What is NOT carried   : Postman scripts (pre-request / tests), form-data and file bodies, OAuth/Digest/AWS auth.
                        Lite tells you exactly which requests are affected.
"""
from __future__ import annotations

import base64
import csv
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from .loader import CANONICAL, InputError
from .text import PLACEHOLDER_RE, looks_like_json


class PostmanError(InputError):
    pass


@dataclass
class Imported:
    name: str
    records: list = field(default_factory=list)     # dicts keyed by loader.CANONICAL keys
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- variables

def _kv_list(items) -> dict:
    out = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("key") is not None and not it.get("disabled") and it.get("enabled", True):
            out[str(it["key"])] = "" if it.get("value") is None else str(it["value"])
    return out


def _dynamic(name: str):
    if name in ("$guid", "$randomUUID"):
        return str(uuid.uuid4())
    if name == "$timestamp":
        return str(int(time.time()))
    if name == "$isoTimestamp":
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if name == "$randomInt":
        return str(random.randint(0, 1000))
    return None


class _Resolver:
    def __init__(self, env_vars: dict):
        self.env = env_vars
        self.missing: set = set()
        self.dynamic: set = set()

    def __call__(self, text, scope: dict):
        if not isinstance(text, str) or "{{" not in text:
            return text
        variables = {**scope, **self.env}   # environment wins over collection (same as Postman)

        def sub(m):
            key = m.group(1).strip()
            if key.startswith("$"):
                value = _dynamic(key)
                if value is not None:
                    self.dynamic.add(key)
                    return value
            elif key in variables:
                return variables[key]
            self.missing.add(key)
            return m.group(0)

        for _ in range(5):          # variables may refer to other variables
            new = PLACEHOLDER_RE.sub(sub, text)
            if new == text:
                break
            text = new
        return text


# --------------------------------------------------------------------------- pieces of a request

def _url(req_url, resolve, scope) -> str:
    query_extra = []
    if isinstance(req_url, str):
        raw = req_url
    elif isinstance(req_url, dict):
        raw = req_url.get("raw")
        if raw is None:
            host = req_url.get("host", "")
            host = ".".join(host) if isinstance(host, list) else host
            path = req_url.get("path", "")
            path = "/".join(path) if isinstance(path, list) else path
            raw = f"{req_url.get('protocol', 'https')}://{host}" + (f"/{path}" if path else "")
        query = req_url.get("query")
        if isinstance(query, list):          # rebuild the query so disabled params are dropped
            raw = raw.split("?", 1)[0]
            enabled = [q for q in query if isinstance(q, dict) and not q.get("disabled") and q.get("key") is not None]
            if enabled:
                raw += "?" + "&".join(f"{q['key']}={q.get('value') or ''}" for q in enabled)
        for var in req_url.get("variable") or []:     # path variables like /users/:id
            if isinstance(var, dict) and var.get("key") is not None:
                raw = re.sub(rf"/:{re.escape(str(var['key']))}(?=[/?#]|$)", "/" + str(var.get("value", "")), raw)
    else:
        raw = ""
    return resolve(raw, scope).strip()


def _headers(req, resolve, scope) -> dict:
    hdrs = req.get("header") or []
    if isinstance(hdrs, str):
        hdrs = [{"key": k.strip(), "value": v.strip()} for k, _, v in
                (ln.partition(":") for ln in hdrs.splitlines()) if k.strip()]
    return {k: resolve(v, scope) for k, v in _kv_list(hdrs).items()}


def _auth_params(auth: dict, kind: str) -> dict:
    block = auth.get(kind)
    if isinstance(block, list):                 # v2.1: [{"key": "token", "value": "..."}]
        return _kv_list(block)
    return {k: str(v) for k, v in (block or {}).items()}   # v2.0: {"token": "..."}


def _apply_auth(auth, resolve, scope, headers: dict, url: str, warn) -> str:
    if not auth or not isinstance(auth, dict):
        return url
    kind = auth.get("type")
    if kind in (None, "noauth"):
        return url
    if any(k.lower() == "authorization" for k in headers) and kind in ("bearer", "basic"):
        return url
    if kind == "bearer":
        token = resolve(_auth_params(auth, "bearer").get("token", ""), scope)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif kind == "basic":
        p = _auth_params(auth, "basic")
        pair = f"{resolve(p.get('username', ''), scope)}:{resolve(p.get('password', ''), scope)}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    elif kind == "apikey":
        p = _auth_params(auth, "apikey")
        key, value = resolve(p.get("key", ""), scope), resolve(p.get("value", ""), scope)
        if key:
            if p.get("in", "header") == "query":
                url += ("&" if "?" in url else "?") + urlencode({key: value})
            else:
                headers[key] = value
    else:
        warn(f"auth type '{kind}' is not supported in Lite; imported without authentication")
    return url


def _body(req, resolve, scope, headers: dict):
    """Returns (body_text | None, unsupported_reason | None)."""
    body = req.get("body")
    if not isinstance(body, dict) or not body.get("mode"):
        return None, None
    mode = body["mode"]
    if mode == "raw":
        text = resolve(body.get("raw", ""), scope)
        language = ((body.get("options") or {}).get("raw") or {}).get("language")
        if language == "text" and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "text/plain"      # Postman sends 'Text' bodies as text/plain
        return (text if text.strip() else None), None
    if mode == "urlencoded":
        pairs = [(k, resolve(v, scope)) for k, v in _kv_list(body.get("urlencoded")).items()]
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return (urlencode(pairs) if pairs else None), None
    if mode == "graphql":
        g = body.get("graphql") or {}
        variables = resolve(g.get("variables") or "{}", scope)
        try:
            variables = json.loads(variables) if variables.strip() else {}
        except ValueError:
            variables = {}
        return json.dumps({"query": resolve(g.get("query", ""), scope), "variables": variables}), None
    if mode == "formdata":
        return None, "form-data (multipart) body"
    if mode in ("file", "binary"):
        return None, "file / binary body"
    return None, f"'{mode}' body"


def _has_script(item_or_collection: dict) -> bool:
    for ev in item_or_collection.get("event") or []:
        script = (ev or {}).get("script") or {}
        lines = script.get("exec") or []
        lines = [lines] if isinstance(lines, str) else lines
        if any(str(ln).strip() for ln in lines):
            return True
    return False


def _saved_example(item: dict):
    for ex in item.get("response") or []:
        if not isinstance(ex, dict):
            continue
        code = ex.get("code")
        if isinstance(code, int) and 100 <= code <= 599:
            body = ex.get("body")
            ok_json = isinstance(body, str) and looks_like_json(body)
            if ok_json:
                try:
                    json.loads(body)
                except ValueError:
                    ok_json = False
            return code, (body.strip() if ok_json else None), ex.get("name")
    return None


# --------------------------------------------------------------------------- walking the collection

def convert(path: str | Path, env_path: str | Path | None = None) -> Imported:
    path = Path(path)
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError) as e:
        raise PostmanError(f"Could not read {path.name} as JSON: {e}") from e
    if not isinstance(data, dict) or "item" not in data:
        if isinstance(data, dict) and "requests" in data:
            raise PostmanError("This is a Postman Collection v1 file. In Postman choose Export -> "
                               "Collection v2.1, and use that file instead.")
        if isinstance(data, dict) and "values" in data:
            raise PostmanError("This looks like a Postman ENVIRONMENT file, not a collection. "
                               "Give the collection as the input and pass this one with --env.")
        raise PostmanError(f"{path.name} does not look like a Postman collection export (no 'item' list).")

    env_vars = {}
    if env_path:
        try:
            env = json.loads(Path(env_path).read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError) as e:
            raise PostmanError(f"Could not read the environment file {env_path}: {e}") from e
        env_vars = _kv_list(env.get("values"))

    result = Imported(name=(data.get("info") or {}).get("name") or path.stem)
    resolve = _Resolver(env_vars)
    script_names: list = []
    unsupported: list = []

    def walk(items, trail, inherited_auth, scope):
        for item in items:
            if not isinstance(item, dict):
                continue
            here = trail + [str(item.get("name", "request"))]
            if "item" in item:                                   # a folder
                walk(item["item"], here, item.get("auth", inherited_auth),
                     {**scope, **_kv_list(item.get("variable"))})
                continue
            req = item.get("request")
            if isinstance(req, str):
                req = {"method": "GET", "url": req}
            if not isinstance(req, dict):
                continue
            display = " / ".join(here)
            notes: list = []
            headers = _headers(req, resolve, scope)
            url = _url(req.get("url"), resolve, scope)
            auth = item.get("auth", inherited_auth)
            url = _apply_auth(auth, resolve, scope, headers, url,
                              lambda msg, d=display: result.warnings.append(f"{d}: {msg}"))
            body, bad = _body(req, resolve, scope, headers)
            method = str(req.get("method") or "GET").upper()
            if _has_script(item):
                script_names.append(display)

            record = {
                "name": display, "method": method, "url": url,
                "headers": "\n".join(f"{k}: {v}" for k, v in headers.items()),
                "body": body or "", "expected_status": "", "expected_response": "",
            }
            example = _saved_example(item)
            if example:
                code, ex_body, ex_name = example
                record["expected_status"] = str(code)
                record["expected_response"] = ex_body or ""
                if ex_body is None:
                    notes.append("saved example has no JSON body, so only its status code was used")
            if bad:
                record["method"] = "#" + method
                record["name"] = f"[not supported in Lite: {bad}] {display}"
                unsupported.append(f"{display} ({bad})")
            for note in notes:
                result.warnings.append(f"{display}: {note}")
            result.records.append(record)

    collection_scope = _kv_list(data.get("variable"))
    walk(data["item"], [], data.get("auth"), collection_scope)

    if _has_script(data):
        result.warnings.append("The collection itself has scripts (collection-level pre-request/test). Lite ignores them.")
    if script_names:
        shown = "; ".join(script_names[:8]) + (f"; ... and {len(script_names) - 8} more" if len(script_names) > 8 else "")
        result.warnings.append(f"{len(script_names)} request(s) have Postman scripts (pre-request / tests) that Lite does "
                               f"NOT run: {shown}")
    if unsupported:
        result.warnings.append(f"{len(unsupported)} request(s) use a body type Lite cannot send and were imported as "
                               f"commented-out rows (#): " + "; ".join(unsupported[:8]))
    if resolve.missing:
        result.warnings.append("Variables with no value (left as {{name}} in the sheet; those rows will fail until you type "
                               "the real value or import again with --env): " + ", ".join(sorted(resolve.missing)))
    if resolve.dynamic:
        result.warnings.append("Postman dynamic variables (" + ", ".join(sorted(resolve.dynamic)) +
                               ") were replaced by ONE fixed value at import time.")
    return result


def write_csv(records: list, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([CANONICAL[k] for k in CANONICAL])
        for r in records:
            w.writerow([r.get(k, "") for k in CANONICAL])
    return path
