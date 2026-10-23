# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""One door for every kind of input: CSV, Excel, Postman collection, or a text file of cURL commands."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import curl, postman
from .loader import CANONICAL, InputError, load_records, sheet_records


@dataclass
class Loaded:
    kind: str                       # sheet | postman | curl
    name: str
    records: list                   # CANONICAL-keyed dicts (the cells)
    warnings: list = field(default_factory=list)


def detect(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".csv", ".xlsx", ".xlsm"):
        return "sheet"
    if ext == ".json":
        return "postman"
    if ext in (".txt", ".sh", ".curl", ".bat", ".cmd", ".ps1"):
        try:
            head = path.read_text(encoding="utf-8-sig", errors="replace")[:4000]
        except OSError:
            head = ""
        return "curl" if curl.looks_like_curl(head) else "sheet"
    raise InputError(f"Unsupported input type '{ext}'. Use .csv, .xlsx, a Postman collection (.json) or a .txt of cURL commands.")


def load(path: str | Path, env: str | Path | None = None) -> Loaded:
    path = Path(path)
    if not path.exists():
        raise InputError(f"Input file not found: {path}")
    kind = detect(path)
    if kind == "postman":
        result = postman.convert(path, env)
        return Loaded("postman", result.name, result.records, result.warnings)
    if kind == "curl":
        records, warnings = curl.convert_text(path.read_text(encoding="utf-8-sig", errors="replace"))
        if not records:
            raise InputError(f"No curl commands found in {path.name}.")
        return Loaded("curl", path.stem, records, warnings)
    return Loaded("sheet", path.stem, sheet_records(path), [])


def from_text(text: str, name: str = "pasted") -> Loaded:
    """Studio: pasted cURL commands, or pasted CSV."""
    if curl.looks_like_curl(text):
        records, warnings = curl.convert_text(text)
        return Loaded("curl", name, records, warnings)
    import csv
    import io
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return Loaded("sheet", name, [], [])
    from .loader import ALIASES, _norm
    column_of = {}
    for i, heading in enumerate(rows[0]):
        for key, names in ALIASES.items():
            if _norm(heading) in names and key not in column_of:
                column_of[key] = i
                break
    if "url" not in column_of:
        raise InputError("Paste cURL commands, or CSV text whose first line has a URL column.")
    recs = [{k: (r[i] if i < len(r) else "") for k, i in column_of.items()} for r in rows[1:] if any(c.strip() for c in r)]
    return Loaded("sheet", name, recs, [])


def rows(loaded: Loaded):
    return load_records(loaded.records).rows


__all__ = ["Loaded", "load", "from_text", "rows", "detect", "CANONICAL"]
