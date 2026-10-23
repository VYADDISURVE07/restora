#!/usr/bin/env python
# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Restora - self-healing API tests from a spreadsheet, a Postman collection or cURL commands.

    python run.py                          run apis.csv
    python run.py my_apis.xlsx             run another sheet (.csv or .xlsx)
    python run.py collection.json          run a Postman collection (v2.x export)
    python run.py commands.txt             run cURL commands (one or more, as copied from DevTools / Swagger)
    python run.py --demo                   try it on the built-in Demo Bank API (no internet needed)
    python run.py --studio                 open Restora Studio, the point-and-click web app

The brain (on by default) fills gaps like <token> or TBD, puts rows in a workable order, heals requests the API
rejects, and explains failures in plain English. It uses a free local AI model when one is installed (Ollama),
and fast rules otherwise. See restora.toml for every setting.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import re
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):          # never crash on a Windows console code page
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

DEMO_SHEET = "examples/demo_bank_messy.csv"


def say(msg=""):
    print(msg, flush=True)


def find_input(name: str) -> Path:
    p = Path(name)
    for candidate in (p, ROOT / p):
        if candidate.exists():
            return candidate.resolve()
    sys.exit(f"ERROR: input file not found: {name}")


def _parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="sheet (.csv/.xlsx), Postman collection (.json) or cURL commands (.txt)")
    g = ap.add_argument_group("everyday")
    g.add_argument("--demo", action="store_true", help="start the built-in Demo Bank API for this run")
    g.add_argument("--studio", action="store_true", help="open Restora Studio (web app) in your browser")
    g.add_argument("--open", action="store_true", help="open the HTML report when the run finishes")
    g.add_argument("--env", metavar="FILE", help="Postman environment file (for {{variables}} in a collection)")
    g.add_argument("--convert", action="store_true", help="only convert a Postman collection / cURL file to CSV")
    b = ap.add_argument_group("brain")
    b.add_argument("--brain", choices=["ai", "rules", "off"], help="ai (default): rules + local AI model | rules | off")
    b.add_argument("--heal", choices=["auto", "suggest", "off"], help="auto (default) | suggest: report the fix only | off")
    b.add_argument("--plan", action="store_true", help="show what the brain would change, without calling any API")
    b.add_argument("--ask", metavar="TEXT", help='write new test rows from plain English, e.g. --ask "login fails with a wrong password"')
    b.add_argument("--no-reorder", action="store_true", help="keep the sheet order")
    b.add_argument("--strict", action="store_true", help="exact comparison: no automatic acceptance of ids / timestamps")
    b.add_argument("--seed", type=int, help="the same generated test data every run (any number except 0)")
    b.add_argument("--base-url", help="base address for rows whose URL starts with /")
    a = ap.add_argument_group("local AI")
    a.add_argument("--model", help="model name, e.g. qwen3:4b-instruct (default from restora.toml)")
    a.add_argument("--ai-url", help="Ollama (http://localhost:11434) or any OpenAI-compatible server")
    a.add_argument("--ai-check", action="store_true", help="check the local AI model and exit")
    o = ap.add_argument_group("other")
    o.add_argument("--config", metavar="FILE", help="settings file (default: restora.toml)")
    o.add_argument("--insecure", action="store_true", help="do not verify SSL certificates (test servers)")
    o.add_argument("--timeout", type=float, help="seconds to wait for each response (default 30)")
    o.add_argument("--junit", metavar="FILE", help="also write a JUnit XML report (for CI)")
    o.add_argument("--port", type=int, default=8765, help="Studio port (default 8765)")
    o.add_argument("--selftest", action="store_true", help="run Restora's own unit tests")
    return ap


def _settings(args):
    from restora import settings as settings_mod
    s = settings_mod.load(args.config, ROOT)
    if args.brain:
        s.brain.mode = args.brain
    if args.heal:
        s.heal.mode = args.heal
    if args.model:
        s.ai.model = args.model
    if args.ai_url:
        s.ai.url = args.ai_url
    if args.no_reorder:
        s.brain.reorder = False
    if args.strict:
        s.brain.smart_compare = False
    if args.seed is not None:
        s.brain.seed = args.seed
    if args.base_url:
        s.brain.base_url = args.base_url
    if args.insecure:
        s.run.insecure = True
    if args.timeout:
        s.run.timeout = args.timeout
    return s


def _imported_csv(loaded, source: Path) -> Path:
    from restora import postman
    stem = re.sub(r"[^A-Za-z0-9]+", "_", source.name.split(".postman")[0].rsplit(".", 1)[0]).strip("_") or "imported"
    return postman.write_csv(loaded.records, ROOT / "imported" / f"{stem}.csv")


def show_plan(path: Path, args) -> int:
    from restora.inputs import load, rows as to_rows
    from restora.session import Session
    s = _settings(args)
    sess = Session(s, ROOT, check_ai=False)
    rows = sess.prepare(to_rows(load(path, args.env)))
    say(f"\nBrain plan for {path.name} (nothing is sent):\n")
    if not sess.plan.insights:
        say("  Nothing to change: the sheet is complete and in a workable order.")
    for i in sess.plan.insights:
        say(f"  [{i.kind}] {i.title}" + (f"\n          {i.detail}" if i.detail else ""))
    say("\nRun order:")
    for n, r in enumerate(rows, start=1):
        needs = r.brain.get("needs", [])
        gaps = ", ".join(sorted({x.raw if x.kind != 'word' else f'{x.key}={x.raw}' for x in needs})) if needs else ""
        state = "skip" if r.skip else ("ERROR" if r.error else "")
        say(f"  {n:>2}. {r.method:6} {r.name}" + (f"   gaps: {gaps}" if gaps else "") + (f"   [{state}]" if state else ""))
    return 0


def ask_ai(path: Path | None, args) -> int:
    from restora.brain.ai import connect
    from restora.inputs import load
    s = _settings(args)
    ai = connect(s.ai, s.ai_key)
    if not ai.ready:
        say(ai.message)
        say("Writing tests from plain English needs the local AI model. Install it with ai_setup.bat (Windows) "
            "or sh ai_setup.sh, then try again.")
        return 2
    known = load(path, args.env).records if path else []
    say(f"{ai.message}\nWriting tests for: {args.ask!r} ...")
    tests = ai.write_tests(args.ask, known)
    if not tests:
        say("The model did not produce usable rows. Try describing the test more precisely.")
        return 1
    from restora import postman
    out = ROOT / "imported" / f"ai_tests_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv"
    postman.write_csv(tests, out)
    for t in tests:
        say(f"  + {t['method']:6} {t['name']}  ->  {t['url']}" + (f"  (expects {t['expected_status']})" if t["expected_status"] else ""))
    say(f"\nSaved to {out}\nReview the rows, paste them into your sheet, and run it.")
    return 0


def ai_check(args) -> int:
    from restora.brain.ai import connect
    s = _settings(args)
    ai = connect(s.ai, s.ai_key)
    say(ai.message)
    if ai.models:
        say("Models installed: " + ", ".join(m for m in ai.models if m))
    if not ai.ready:
        say("Restora still works without it (rules only). To install the free local model, run ai_setup.bat "
            "(Windows) or sh ai_setup.sh (Mac/Linux).")
        return 1
    answer = ai.suggest_value("beneficiaryNickname", None, {"name": "Priya Sharma"})
    say(f"Test question answered in {ai.client.seconds:.1f} s: beneficiaryNickname -> {answer!r}")
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)

    missing = [m for m in ("requests", "pytest") if importlib.util.find_spec(m) is None]
    if missing:
        sys.exit(f"ERROR: missing Python package(s): {', '.join(missing)}.\n"
                 f"Run setup.bat (Windows) or sh setup.sh (Mac/Linux) first - see SETUP_GUIDE.md.")

    if args.selftest:
        import pytest
        return int(pytest.main([str(ROOT / "selftest"), "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pytest.ini")]))
    if args.studio:
        from restora.studio.server import serve
        return serve(port=args.port, open_browser=True, settings=_settings(args), demo=args.demo)
    if args.ai_check:
        return ai_check(args)

    import pytest
    from restora.inputs import detect, load
    from restora.loader import InputError

    name = args.input or (DEMO_SHEET if args.demo else "apis.csv")
    if args.ask:
        return ask_ai(find_input(name) if (args.input or args.demo) else None, args)
    path = find_input(name)

    try:
        kind = detect(path)
        if kind in ("postman", "curl"):
            loaded = load(path, find_input(args.env) if args.env else None)
            csv_path = _imported_csv(loaded, path)
            label = "Postman collection" if kind == "postman" else "cURL file"
            say(f"{label} '{loaded.name}': {len(loaded.records)} request(s) converted -> {csv_path}")
            if loaded.warnings:
                notes = csv_path.with_name(csv_path.stem + "_import_notes.txt")
                notes.write_text("\n".join(f"- {w}" for w in loaded.warnings) + "\n", encoding="utf-8")
                say("Things to know about this import:")
                for w in loaded.warnings:
                    say(f"  - {w}")
                say(f"  (also saved in {notes})")
            if args.convert:
                say(f"\nEdit {csv_path.name} if you like, then run:  python run.py \"{csv_path}\"")
                return 0
            path = csv_path
        elif args.convert:
            sys.exit("ERROR: --convert only applies to a Postman collection (.json) or a cURL file (.txt).")
        if args.plan:
            return show_plan(path, args)
        from restora.loader import load_sheet
        sheet = load_sheet(path)
    except InputError as e:
        say(f"ERROR: {e}")
        return 2

    active = [r for r in sheet.rows if not r.skip]
    say(f"\n{path.name}: {len(sheet.rows)} row(s), {len(active)} to run, {len(sheet.rows) - len(active)} switched off")
    if sheet.ignored_columns:
        say(f"Ignored column(s) Restora does not use: {', '.join(sheet.ignored_columns)}")
    if not active:
        say("Nothing to run yet. Add your APIs to the sheet (rows starting with # are ignored),")
        say("or try the built-in example:  python run.py --demo")
        return 2

    server = None
    if args.demo:
        from demo import mock_server
        try:
            server = mock_server.start()
        except OSError:
            say(f"ERROR: port {mock_server.PORT} is already in use, so the demo API could not start.")
            return 2
        say(f"Demo API started on http://127.0.0.1:{mock_server.PORT}  (Demo Bank at /api/v1 - fictional)")

    run_dir = ROOT / "reports" / f"{path.stem}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    pytest_args = [str(ROOT / "tests" / "test_apis.py"), "-c", str(ROOT / "pytest.ini"), "--rootdir", str(ROOT),
                   "-v", "--tb=short", f"--apis={path}", f"--run-dir={run_dir}"]
    passthrough = {"--brain": args.brain, "--heal": args.heal, "--model": args.model, "--ai-url": args.ai_url,
                   "--restora-config": args.config, "--restora-base-url": args.base_url, "--api-timeout": args.timeout,
                   "--restora-seed": args.seed}
    pytest_args += [f"{k}={v}" for k, v in passthrough.items() if v is not None]
    for flag, on in (("--insecure", args.insecure), ("--no-reorder", args.no_reorder), ("--strict-compare", args.strict)):
        if on:
            pytest_args.append(flag)
    if args.junit:
        pytest_args.append(f"--junitxml={args.junit}")
    if importlib.util.find_spec("allure_pytest") is not None:
        pytest_args.append(f"--alluredir={run_dir / 'allure-results'}")

    say("")
    try:
        rc = int(pytest.main(pytest_args))
    finally:
        if server:
            server.shutdown()
            server.server_close()

    if args.open and (run_dir / "report.html").exists():
        webbrowser.open((run_dir / "report.html").as_uri())
    return rc


if __name__ == "__main__":
    sys.exit(main())
