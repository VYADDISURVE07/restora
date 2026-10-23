# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Send one request. Works out the headers you did not type."""
from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
import urllib3

from . import __version__
from .loader import Row
from .mask import mask_headers, mask_text
from .text import FORM_RE, looks_like_json

NOT_JSON = object()


@dataclass
class Settings:
    insecure: bool = False      # --insecure : do not verify SSL certificates
    timeout: float = 30.0       # --timeout  : seconds to wait for each response


@dataclass
class Prepared:
    method: str
    url: str
    headers: dict
    body: str | None
    notes: list = field(default_factory=list)


@dataclass
class Outcome:
    prepared: Prepared
    status: int | None = None
    elapsed_ms: float = 0.0
    headers: dict = field(default_factory=dict)
    text: str = ""
    json_value: Any = NOT_JSON
    error: str | None = None            # set when no response arrived at all

    @property
    def is_json(self) -> bool:
        return self.json_value is not NOT_JSON


def detect_content_type(body: str) -> str:
    s = body.strip()
    if looks_like_json(s):
        return "application/json"
    if s.startswith("<"):
        head = s[:300].lower()
        if "envelope" in head and "soap" in head:
            return "text/xml; charset=utf-8"
        return "application/xml"
    if FORM_RE.match(s):
        return "application/x-www-form-urlencoded"
    return "text/plain"


def prepare(row: Row) -> Prepared:
    given = {k.lower() for k in row.headers}
    headers: dict = {}
    notes: list = []
    if "user-agent" not in given:
        headers["User-Agent"] = f"Restora/{__version__}"
    if "accept" not in given:
        wants_json = bool(row.expected_response) and looks_like_json(row.expected_response)
        headers["Accept"] = "application/json" if wants_json else "*/*"
    if row.body is not None and "content-type" not in given:
        ct = detect_content_type(row.body)
        headers["Content-Type"] = ct
        notes.append(f"No Content-Type header was given, so '{ct}' was used (detected from the body).")
    headers.update(row.headers)
    return Prepared(row.method, row.url, headers, row.body, notes)


def _decode(content: bytes, declared: str | None) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode(declared or "latin-1", errors="replace")


def _connection_reason(error: Exception) -> str:
    text = str(error)
    for pattern in (r"Failed to establish a new connection: ([^'\")]+)",
                    r"(Failed to resolve '[^']+' \([^)]*\))",
                    r"(Connection (?:aborted|reset)[^'\")]*)"):
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return text[:100]


def send(prepared: Prepared, settings: Settings) -> Outcome:
    if settings.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    out = Outcome(prepared=prepared)
    data = prepared.body.encode("utf-8") if prepared.body is not None else None
    host = urlparse(prepared.url).netloc
    started = time.perf_counter()
    try:
        r = requests.request(prepared.method, prepared.url, headers=prepared.headers, data=data,
                             timeout=settings.timeout, verify=not settings.insecure)
    except requests.exceptions.SSLError:
        out.error = ("SSL certificate problem. If this is an internal/test server with a self-signed certificate, "
                     "run with --insecure, or point REQUESTS_CA_BUNDLE at your company's CA file.")
    except requests.exceptions.Timeout:
        out.error = f"No response from {host} within {settings.timeout:g} seconds (use --timeout to wait longer)."
    except requests.exceptions.ConnectionError as e:
        reason = _connection_reason(e)
        out.error = (f"Could not connect to {host} ({reason}). Is the server running, and reachable from this "
                     f"machine (VPN / proxy / firewall)?")
    except Exception as e:  # noqa: BLE001 - anything else becomes a readable failure on that row
        out.error = f"{type(e).__name__}: {e}"
    else:
        out.status = r.status_code
        out.headers = dict(r.headers)
        out.text = _decode(r.content, r.encoding)
        try:
            out.json_value = json.loads(out.text)
        except ValueError:
            pass
    out.elapsed_ms = (time.perf_counter() - started) * 1000
    return out


def to_curl(prepared: Prepared) -> str:
    """A copy-paste curl command (secrets masked) for reproducing the call by hand."""
    parts = ["curl", "-i", "-X", prepared.method, shlex.quote(prepared.url)]
    for k, v in mask_headers(prepared.headers).items():
        parts += ["-H", shlex.quote(f"{k}: {v}")]
    if prepared.body is not None:
        parts += ["--data-raw", shlex.quote(mask_text(prepared.body))]
    return " ".join(parts)
