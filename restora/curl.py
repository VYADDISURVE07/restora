# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Turn cURL commands (copied from a browser's DevTools, Swagger UI or API docs) into sheet rows.

Understands bash style (\\ line endings) and Windows cmd style (^ escapes, as Chrome's "Copy as cURL (cmd)").
"""
from __future__ import annotations

import base64
import re
import shlex
from urllib.parse import urlencode, urlsplit

from .loader import CANONICAL


def looks_like_curl(text: str) -> bool:
    return bool(re.search(r"(^|\n)\s*curl(\.exe)?\s", text or ""))


def _split_commands(text: str) -> list:
    t = text.replace("\r\n", "\n")
    if '^"' in t:                                            # Windows cmd escaping
        t = t.replace('^\\^"', '\\"').replace('^"', '"').replace("^^", "^").replace("^%", "%").replace("^&", "&") \
             .replace("^<", "<").replace("^>", ">").replace("^|", "|").replace("^{", "{").replace("^}", "}")
        t = re.sub(r"\^\n", " ", t)
    t = re.sub(r"\\\n", " ", t)                              # bash line continuation
    t = re.sub(r"`\n", " ", t)                               # PowerShell line continuation
    commands, current = [], []
    for line in t.split("\n"):
        if re.match(r"^\s*curl(\.exe)?\s", line) and current:
            commands.append(" ".join(current))
            current = []
        if line.strip() and not line.strip().startswith("#"):
            current.append(line.strip())
    if current:
        commands.append(" ".join(current))
    return [c for c in commands if re.match(r"^curl(\.exe)?\s", c)]


def parse_command(cmd: str) -> tuple:
    """(record, warnings) for one curl command."""
    warnings = []
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError as e:
        raise ValueError(f"could not read this curl command ({e})") from e
    tokens = tokens[1:]
    method, url, headers, data, get_mode, form = None, None, {}, [], False, False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None

        def take():
            nonlocal i
            i += 1
            return nxt
        if t in ("-X", "--request"):
            method = (take() or "GET").upper()
        elif t.startswith("-X") and len(t) > 2:
            method = t[2:].upper()
        elif t in ("-H", "--header"):
            h = take() or ""
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            v = take() or ""
            if v.startswith("@"):
                warnings.append(f"the body is read from a file ({v}); paste the file's content into the Body cell")
            data.append(v)
        elif t == "--data-urlencode":
            v = take() or ""
            k, eq, val = v.partition("=")
            data.append(urlencode({k: val}) if eq else urlencode({"": v})[1:])
        elif t == "--json":
            data.append(take() or "")
            headers.setdefault("Content-Type", "application/json")
            headers.setdefault("Accept", "application/json")
        elif t in ("-u", "--user"):
            cred = take() or ""
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        elif t in ("-b", "--cookie"):
            headers["Cookie"] = take() or ""
        elif t in ("-A", "--user-agent"):
            headers["User-Agent"] = take() or ""
        elif t in ("-e", "--referer"):
            headers["Referer"] = take() or ""
        elif t == "--url":
            url = take()
        elif t in ("-G", "--get"):
            get_mode = True
        elif t in ("-I", "--head"):
            method = "HEAD"
        elif t in ("-F", "--form", "--form-string"):
            take()
            form = True
            warnings.append("multipart form (-F) bodies are not supported; that part was left out")
        elif t in ("-o", "--output", "-w", "--write-out", "-m", "--max-time", "--connect-timeout", "-x", "--proxy",
                   "--cacert", "--cert", "--key", "-E", "--retry", "-T", "--upload-file", "-r", "--range"):
            take()
        elif t.startswith("-"):
            pass                                             # -s -S -L -k -v -i --compressed ... do not change the request
        elif url is None:
            url = t
        i += 1
    if not url:
        raise ValueError("no URL found in this curl command")
    body = "&".join(data) if data else ""
    if get_mode and body:
        url += ("&" if "?" in url else "?") + body
        body = ""
    method = method or ("POST" if body or form else "GET")
    path = urlsplit(url).path or "/"
    record = {k: "" for k in CANONICAL}
    record.update(name=f"{method} {path}"[:70], method=method, url=url,
                  headers="\n".join(f"{k}: {v}" for k, v in headers.items()), body=body)
    return record, warnings


def convert_text(text: str) -> tuple:
    """(records, warnings) for every curl command in the text."""
    records, warnings = [], []
    for n, cmd in enumerate(_split_commands(text), start=1):
        try:
            rec, w = parse_command(cmd)
        except ValueError as e:
            warnings.append(f"command {n}: {e}")
            continue
        records.append(rec)
        warnings += [f"command {n}: {x}" for x in w]
    return records, warnings
