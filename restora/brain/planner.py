"""Before anything is sent: tidy each row, find what every row needs, and put the rows in a workable order.

Ordering follows producer -> consumer links (the idea behind Microsoft's RESTler, applied to a plain sheet
instead of an OpenAPI spec): if row 2 needs a `token` and row 10 is the Login that returns one, row 10 must
run first. Rows are only moved when such a link is clear; otherwise the sheet order is kept.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import jsonpath
from .intent import negative_reason
from .generate import kind_for
from .names import is_id_name, is_token_name, norm, resource_of_path, similarity
from .placeholders import scan_body_text, scan_headers, scan_json, scan_url
from .repair import RepairFailed, fix_url, infer_method, looks_relative, repair_json

_LOGIN = re.compile(r"log\s?in|sign\s?in|auth|token|session|oauth", re.I)


@dataclass
class Insight:
    kind: str            # order | fill | repair | method | url | negative | info | warning
    row: int             # row number (as numbered in the sheet)
    title: str
    detail: str = ""
    source: str = "rule"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "row": self.row, "title": self.title, "detail": self.detail, "source": self.source}


@dataclass
class Plan:
    rows: list
    insights: list = field(default_factory=list)
    moved: dict = field(default_factory=dict)        # row number -> reason it was moved

    def counts(self) -> dict:
        out: dict = {}
        for i in self.insights:
            out[i.kind] = out.get(i.kind, 0) + 1
        return out


def _url_path(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).path if "://" in url else url.split("?")[0]
    except ValueError:
        return url


def predicted_outputs(row) -> list:
    """Names of values this row will probably return (used only to decide the order)."""
    keys = []
    if row.expected_response and row.expected_response.lstrip()[:1] in "{[":
        try:
            sample = json.loads(row.expected_response)
            for parts, key, _ in jsonpath.leaves(sample):
                if isinstance(key, str):
                    keys.append(key)
                    tail = [p for p in parts[-2:] if isinstance(p, str)]
                    if len(tail) == 2:
                        keys.append(" ".join(tail))
        except ValueError:
            pass
    path = _url_path(row.url)
    if row.method == "POST" and (_LOGIN.search(path) or _LOGIN.search(row.name)):
        keys += ["token", "access_token"]
    if row.method in ("POST", "PUT"):
        res = resource_of_path(path)
        if res and not _LOGIN.search(path):
            keys += ["id", f"{res} id", f"{res} number", f"{res} no"]
    return keys


def _needs(row, implicit: bool) -> list:
    found = scan_url(row.url, implicit) + scan_headers(row.headers, implicit)
    if row.body:
        data = None
        if row.body.lstrip()[:1] in "{[":
            try:
                data = json.loads(row.body)
            except ValueError:
                data = None
        found += scan_json(data, implicit) if data is not None else scan_body_text(row.body, implicit)
    return found


def _score(need, key: str, producer, consumer) -> float:
    name = need.name
    if is_token_name(name) or (need.where == "header" and need.key.lower() == "authorization"):
        return 0.95 if is_token_name(key) else 0.0
    s = similarity(name, key)
    path = _url_path(producer.url)
    if need.resource and resource_of_path(path) == need.resource and norm(key) in ("id", need.resource + "id"):
        s += 0.25
    if producer.method == "POST" and not re.search(r"(\{[^}]*\}|<[^>]*>|:\w+|/\d+)/?$", path):
        s += 0.05                                       # POST to a collection creates the thing
    return s


def _is_reference(need) -> bool:
    """Only needs that must come from another response decide the order; plain test data (email, phone ...) is
    generated, so it never forces a row to move."""
    name = need.name
    if is_token_name(name) or (need.where == "header" and need.key.lower() in ("authorization", "proxy-authorization")):
        return True
    if need.explicit:
        return True
    if kind_for(name) == "uuid":
        return False
    return need.kind == "path" or is_id_name(name) or bool(need.key and is_id_name(need.key) and need.kind == "word")


def plan(rows: list, settings, explicit_only: bool = False) -> Plan:
    insights: list = []
    by_num = {r.number: r for r in rows}
    reorder = getattr(settings, "reorder", True) and not explicit_only
    implicit = not explicit_only

    # ------------------------------------------------------------ 1. tidy every row
    base = (getattr(settings, "base_url", "") or "").strip() or None
    first_abs = next((r.url for r in rows if r.url and not looks_relative(r.url)), None)
    last_abs = None
    url_fixes: list = []
    for row in rows:
        if not explicit_only and row.url and ("url" in row.problems):
            fixed, how = fix_url(row.url, last_abs or base or first_abs)
            if how and not looks_relative(fixed):
                url_fixes.append((row, row.url, fixed, how))
                from .repair import origin as _origin
                row.brain.setdefault("planned", []).append(f"URL completed with {_origin(fixed)}")
                row.url = fixed
                row.problems.pop("url", None)
                row.refresh_error()
        if row.url and not looks_relative(row.url):
            last_abs = row.url
    if len(url_fixes) > 2:
        from .repair import origin
        origins = sorted({origin(f) for _, _, f, _ in url_fixes})
        insights.append(Insight("url", 0, f"Completed {len(url_fixes)} short URLs",
                                f"They started with '/', so Restora put {', '.join(origins)} in front "
                                f"(taken from the rows that had a full address)."))
    else:
        for row, before, after, how in url_fixes:
            insights.append(Insight("url", row.sheet_row, f"Completed the URL of '{row.name}'", f"{before} -> {after} ({how})"))
    for row in rows:
        if row.skip:
            continue
        if not explicit_only and row.method_inferred:
            method, why = infer_method(row.name, row.body is not None)
            if method != row.method:
                row.notes = [n for n in row.notes if not n.startswith("Method was blank")]
                row.notes.append(f"Method was blank; the brain chose {method} because {why}.")
                insights.append(Insight("method", row.sheet_row, f"Chose {method} for '{row.name}'",
                                        f"The Method cell was blank and {why}."))
                row.method = method
            else:
                insights.append(Insight("method", row.sheet_row, f"Chose {method} for '{row.name}'",
                                        f"The Method cell was blank and {why}."))
        if not explicit_only and row.brain.get("body_invalid") and row.body:
            try:
                value, fixed, fixes = repair_json(row.body)
                row.body = json.dumps(value, ensure_ascii=False)
                row.notes = [n for n in row.notes if "not valid JSON" not in n]
                insights.append(Insight("repair", row.sheet_row, f"Repaired the JSON body of '{row.name}'",
                                        "; ".join(fixes).capitalize() + "."))
                row.brain["body_repaired"] = fixes
            except RepairFailed as e:
                insights.append(Insight("warning", row.sheet_row, f"The body of '{row.name}' is not valid JSON",
                                        f"Restora could not repair it ({e}); it will be sent exactly as typed."))
        if not explicit_only and "expected_json" in row.problems and row.expected_response:
            try:
                value, fixed, fixes = repair_json(row.expected_response)
                row.expected_response = json.dumps(value, ensure_ascii=False)
                row.problems.pop("expected_json", None)
                row.refresh_error()
                insights.append(Insight("repair", row.sheet_row, f"Repaired the sample response of '{row.name}'",
                                        "; ".join(fixes).capitalize() + "."))
            except RepairFailed:
                pass
        reason = negative_reason(row)
        row.brain["negative"] = reason
        if reason and not explicit_only:
            insights.append(Insight("negative", row.sheet_row, f"'{row.name}' is a negative test",
                                    f"Restora will never heal it, because {reason}."))
        row.brain["needs"] = _needs(row, implicit)

    # ------------------------------------------------------------ 2. who produces what
    active = [r for r in rows if not r.skip and not r.error]
    outputs = {r.number: predicted_outputs(r) for r in active if not r.brain.get("negative")}
    names = {re.sub(r"[^a-z0-9]", "", r.name.lower()): r.number for r in rows}
    edges: dict = {r.number: set() for r in active}          # consumer -> producers
    why: dict = {}
    for row in active:
        for need in row.brain.get("needs", []):
            producer, key = None, None
            if need.explicit:
                if need.name.startswith("$"):
                    continue
                for i, ch in enumerate(need.name):
                    if ch == ".":
                        num = names.get(re.sub(r"[^a-z0-9]", "", need.name[:i].lower()))
                        if num is not None:
                            producer, key = num, need.name[i + 1:]
                if producer is None and need.name.startswith("#"):
                    m = re.match(r"#(\d+)", need.name)
                    num = int(m.group(1)) if m else None
                    producer = num if num in by_num else None
                    key = need.name
            if producer is None and implicit and _is_reference(need):
                best = (0.0, None, None)
                for num, keys in outputs.items():
                    if num == row.number:
                        continue
                    for k in keys:
                        s = _score(need, k, by_num[num], row)
                        if s > best[0]:
                            best = (s, num, k)
                if best[0] >= 0.6:
                    producer, key = best[1], best[2]
            if producer is not None and producer in edges and producer != row.number:
                edges[row.number].add(producer)
                why.setdefault((producer, row.number), (need.label, key))
                row.brain.setdefault("sources", {})[need.raw] = producer
        row.brain["producers"] = sorted(edges.get(row.number, set()))

    # ------------------------------------------------------------ 3. order: pull producers up, keep everything else
    order = [r.number for r in rows]
    moved: dict = {}
    if reorder and any(edges.values()):
        position = {num: i for i, num in enumerate(order)}
        new_order: list = []
        done: set = set()
        visiting: set = set()
        cycle = False

        def emit(n):
            nonlocal cycle
            if n in done:
                return
            if n in visiting:
                cycle = True
                return
            visiting.add(n)
            for p in sorted(edges.get(n, ()), key=lambda x: position[x]):
                emit(p)
            visiting.discard(n)
            done.add(n)
            new_order.append(n)

        for n in order:
            emit(n)
        if cycle:
            insights.append(Insight("warning", 0, "Some rows depend on each other in a circle",
                                    "Restora kept the sheet order for those rows."))
        new_pos = {num: i for i, num in enumerate(new_order)}
        for consumer, producers in edges.items():
            for p in sorted(producers, key=lambda x: position[x]):
                if position[p] > position[consumer] and new_pos[p] < new_pos[consumer] and p not in moved:
                    need, key = why[(p, consumer)]
                    pr, co = by_num[p], by_num[consumer]
                    moved[p] = (f"'{co.name}' needs {need}, which '{pr.name}' returns"
                                + (f" ({key})" if key and norm(key) != norm(need.strip('<>{}')) else ""))
                    insights.append(Insight("order", pr.sheet_row, f"Moved '{pr.name}' up, before '{co.name}'",
                                            moved[p] + "."))
        order = new_order
    ordered = [by_num[n] for n in order]
    for i, r in enumerate(ordered, start=1):
        r.brain["position"] = i
    by_sheet_row = {r.sheet_row: r for r in rows}
    for ins in insights:
        target = by_sheet_row.get(ins.row)
        if target is not None and ins.kind not in ("negative", "url"):
            target.brain.setdefault("planned", []).append(f"{ins.title}: {ins.detail}" if ins.detail else ins.title)
    return Plan(ordered, insights, moved)
