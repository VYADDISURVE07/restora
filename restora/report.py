"""Reports that need nothing installed: one self-contained HTML page and one CSV."""
from __future__ import annotations

import csv
import html
from collections import Counter
from pathlib import Path

from . import __version__

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#5d6678;--line:#e1e5ec;--pass:#14794a;--passbg:#e4f5ec;
--fail:#b3261e;--failbg:#fde9e7;--skip:#5d6678;--skipbg:#eceff3;--code:#f1f3f7;--accent:#2457c5;
--heal:#6a3fc2;--healbg:#efe8fc;--brain:#f3f0fb}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1a1f29;--ink:#e8ebf2;--mute:#9aa3b5;--line:#2a3140;
--pass:#5ccf97;--passbg:#14301f;--fail:#ff8b82;--failbg:#3a1a18;--skip:#9aa3b5;--skipbg:#262c38;--code:#10131a;--accent:#8ab0ff;
--heal:#c3a6ff;--healbg:#2b2140;--brain:#1e1a2b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);font-size:13px;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat b{display:block;font-size:26px;line-height:1.1}.stat span{color:var(--mute);font-size:13px}
.stat.p b{color:var(--pass)}.stat.f b{color:var(--fail)}.stat.h b{color:var(--heal)}
.brainbox{background:var(--brain);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:16px}
.brainbox b{color:var(--heal)}.brainbox ul{margin:8px 0 0;padding-left:20px}.brainbox li{margin:2px 0}
.why{background:var(--failbg);color:var(--ink);border-radius:8px;padding:8px 12px;margin-top:10px}
.healed{background:var(--healbg);border-radius:8px;padding:8px 12px;margin-top:10px}
.healed ol{margin:4px 0 0;padding-left:20px}
.bar{display:flex;gap:8px;margin:0 0 12px;flex-wrap:wrap}
.bar button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;
padding:6px 12px;font:inherit;cursor:pointer}.bar button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
details.row{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--skip);
border-radius:10px;margin-bottom:8px}
details.row.PASSED{border-left-color:var(--pass)}details.row.FAILED{border-left-color:var(--fail)}
details.row.HEALED{border-left-color:var(--heal)}
details.row>summary{list-style:none;cursor:pointer;padding:10px 14px;display:grid;
grid-template-columns:34px 82px 54px minmax(0,1fr) auto;gap:10px;align-items:center}
details.row>summary::-webkit-details-marker{display:none}
.brainbox summary{cursor:pointer;margin-top:6px}
.num{color:var(--mute)}.method{font-weight:700;font-size:13px}.name{font-weight:600;overflow-wrap:anywhere}
.meta{color:var(--mute);font-size:13px;white-space:nowrap}
.url{grid-column:4/6;color:var(--mute);font-size:13px;overflow-wrap:anywhere;margin-top:-6px}
.badge{font-size:12px;font-weight:700;padding:2px 8px;border-radius:999px;text-align:center}
.badge.PASSED{background:var(--passbg);color:var(--pass)}.badge.FAILED{background:var(--failbg);color:var(--fail)}
.badge.SKIPPED{background:var(--skipbg);color:var(--skip)}.badge.HEALED{background:var(--healbg);color:var(--heal)}
.detail{padding:4px 14px 14px;border-top:1px solid var(--line)}
.detail h4{margin:14px 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
.detail ul{margin:0;padding-left:20px}.fail li{color:var(--fail)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
pre{background:var(--code);border-radius:8px;padding:10px 12px;margin:0;overflow:auto;max-height:340px;
font:12.5px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:720px){.cards{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}
details.row>summary{grid-template-columns:30px 76px 1fr}.method,.meta{grid-column:auto}.url{grid-column:1/-1;margin-top:0}}
"""

_JS = """
const rows=[...document.querySelectorAll('details.row')];
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.bar button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 const f=b.dataset.f;rows.forEach(r=>r.hidden=(f!=='ALL'&&!r.classList.contains(f)));});
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _headers_block(headers: dict) -> str:
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


def _pre(text) -> str:
    return f"<pre>{_e(text)}</pre>" if text else "<pre>(empty)</pre>"


def counts(records) -> Counter:
    return Counter(r.result for r in records)


def _ul(items) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def write_html(records, path: Path, meta: dict) -> Path:
    c = counts(records)
    brain = meta.get("brain") or {}
    insights = meta.get("insights") or []
    cards = []
    for r in records:
        status = r.http_status if r.http_status is not None else "no response"
        timing = f"{r.elapsed_ms:,.0f} ms" if r.result != "SKIPPED" else ""
        parts = []
        if r.result == "FAILED" and getattr(r, "explanation", ""):
            parts.append(f"<div class='why'><b>In plain English:</b> {_e(r.explanation)}</div>")
        if getattr(r, "heal_attempts", None):
            steps = "".join(f"<li>{_e(a['reason'])} <span class='num'>(HTTP {a['status_before']} &rarr; "
                            f"{a['status_after']}{', AI' if a.get('source') == 'ai' else ', plug-in' if a.get('source') == 'plugin' else ''})</span>"
                            + _ul(a["changes"]) + "</li>" for a in r.heal_attempts)
            if any(a.get("source") == "guard" for a in r.heal_attempts):
                title = "Restora checked why the API refused this request"
            else:
                title = "Restora healed this request" if r.result == "HEALED" else "Restora tried to heal this request"
            parts.append(f"<div class='healed'><b>{title}</b><ol>{steps}</ol></div>")
        if getattr(r, "suggestion", ""):
            parts.append(f"<div class='healed'><b>Suggested fix</b> {_e(r.suggestion)}</div>")
        if r.failures:
            label = "Why it was skipped" if r.result == "SKIPPED" else "What went wrong"
            parts.append(f"<h4>{label}</h4><ul class='fail'>" + "".join(f"<li>{_e(f)}</li>" for f in r.failures) + "</ul>")
        if getattr(r, "planned", None):
            parts.append("<h4>Planned by the brain</h4>" + _ul(r.planned))
        if getattr(r, "filled", None):
            parts.append("<h4>Gaps filled</h4>" + _ul(r.filled))
        if getattr(r, "dynamic", None):
            parts.append("<h4>Differences accepted (they change on every call)</h4>" + _ul(r.dynamic))
        if getattr(r, "negative", None):
            parts.append(f"<h4>Negative test</h4><ul><li>Never healed, because {_e(r.negative)}.</li></ul>")
        if r.notes:
            parts.append("<h4>Adjusted automatically</h4>" + _ul(r.notes))
        if r.result != "SKIPPED":
            req = _headers_block(r.request_headers) + ("\n\n" + r.request_body if r.request_body else "")
            resp = _headers_block(r.response_headers) + ("\n\n" + r.response_body if r.response_body else "")
            parts.append("<div class='cols'><div><h4>Request sent</h4>" + _pre(req.strip()) +
                         f"</div><div><h4>Response received (HTTP {_e(status)})</h4>" + _pre(resp.strip()) + "</div></div>")
            if r.expected_response:
                parts.append("<h4>Sample (expected) response</h4>" + _pre(r.expected_response))
            if r.curl:
                parts.append("<h4>Same call as curl (secrets hidden)</h4>" + _pre(r.curl))
        moved = " &middot; ran as #" + str(r.position) if getattr(r, "position", r.number) != r.number else ""
        cards.append(
            f"<details class='row {r.result}'{' open' if r.result == 'FAILED' else ''}>"
            f"<summary><span class='num'>{r.number}</span><span class='badge {r.result}'>{r.result}</span>"
            f"<span class='method'>{_e(r.method)}</span><span class='name'>{_e(r.name)}</span>"
            f"<span class='meta'>{_e(status if r.result != 'SKIPPED' else '')} {_e(timing)}</span>"
            f"<span class='url'>{_e(r.url)} &middot; sheet row {r.sheet_row}{moved}</span></summary>"
            f"<div class='detail'>{''.join(parts)}</div></details>")

    bits = []
    if brain:
        for key, one, many in (("filled", "gap filled", "gaps filled"), ("moved", "row moved", "rows moved"),
                               ("repaired", "input repaired", "inputs repaired"),
                               ("healed", "request healed", "requests healed"),
                               ("dynamic", "changing value accepted", "changing values accepted"),
                               ("explained", "failure explained", "failures explained")):
            if brain.get(key):
                bits.append(f"<b>{brain[key]}</b> {one if brain[key] == 1 else many}")
    brain_html = ""
    if brain:
        ai_line = _e(brain.get("ai", ""))
        if brain.get("ai_calls"):
            ai_line += f" &middot; {brain['ai_calls']} AI call(s), {brain.get('ai_seconds', 0)} s"
        plan_items = "".join(f"<li>{_e(i['title'])}{(': ' + _e(i['detail'])) if i.get('detail') else ''}</li>"
                             for i in insights if i.get("kind") != "negative")
        brain_html = (f"<div class='brainbox'><div>Brain ({_e(brain.get('mode'))}, healing {_e(brain.get('heal_mode'))}): "
                      f"{' &middot; '.join(bits) if bits else 'nothing needed fixing'}</div>"
                      f"<div class='num' style='margin-top:4px'>{ai_line}</div>"
                      + (f"<details><summary class='num'>What the brain planned before the run "
                         f"({len([i for i in insights if i.get('kind') != 'negative'])})</summary><ul>{plan_items}</ul></details>" if plan_items else "")
                      + "</div>")

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Restora report</title>
<style>{_CSS}</style></head><body><div class="wrap">
<h1>Restora report</h1>
<div class="sub">{_e(meta.get('input'))} &middot; {_e(meta.get('started'))} &middot; {meta.get('duration', 0):.1f} s &middot; Restora {__version__}</div>
<div class="cards">
<div class="stat"><b>{len(records)}</b><span>requests</span></div>
<div class="stat p"><b>{c['PASSED']}</b><span>passed</span></div>
<div class="stat h"><b>{c['HEALED']}</b><span>healed</span></div>
<div class="stat f"><b>{c['FAILED']}</b><span>failed</span></div>
<div class="stat"><b>{c['SKIPPED']}</b><span>skipped</span></div></div>
{brain_html}
<div class="bar"><button class="on" data-f="ALL">All</button><button data-f="FAILED">Failed</button>
<button data-f="HEALED">Healed</button><button data-f="PASSED">Passed</button><button data-f="SKIPPED">Skipped</button></div>
{''.join(cards)}
</div><script>{_JS}</script></body></html>"""
    path.write_text(page, encoding="utf-8")
    return path


def write_csv(records, path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:   # utf-8-sig so Excel opens it cleanly
        w = csv.writer(fh)
        w.writerow(["#", "Sheet row", "Ran as", "Name", "Method", "URL", "Result", "HTTP status", "Expected status",
                    "Time (ms)", "Problems", "In plain English", "What the brain did", "Notes", "Actual response (secrets hidden)"])
        for r in records:
            brain = list(getattr(r, "planned", []) or []) + [f"filled {f}" for f in (getattr(r, "filled", []) or [])]
            brain += [a["reason"] for a in (getattr(r, "heal_attempts", []) or [])]
            brain += [f"accepted {d}" for d in (getattr(r, "dynamic", []) or [])]
            w.writerow([r.number, r.sheet_row, getattr(r, "position", r.number), r.name, r.method, r.url, r.result,
                        r.http_status if r.http_status is not None else "",
                        r.expected_status if r.expected_status is not None else "",
                        f"{r.elapsed_ms:.0f}" if r.result != "SKIPPED" else "",
                        "\n".join(r.failures), getattr(r, "explanation", "") or "", "\n".join(brain),
                        "\n".join(r.notes), r.response_body or ""])
    return path
