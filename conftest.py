# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""pytest wiring: the brain plans the run, then one test per sheet row (in the planned order); reports at the end."""
import datetime
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from restora import settings as settings_mod             # noqa: E402
from restora.loader import InputError, load_sheet         # noqa: E402
from restora.session import Session                       # noqa: E402


def pytest_addoption(parser):
    g = parser.getgroup("Restora")
    g.addoption("--apis", default="apis.csv", help="CSV / Excel sheet with the APIs to test")
    g.addoption("--insecure", action="store_true", default=None, help="do not verify SSL certificates")
    g.addoption("--api-timeout", type=float, default=None, help="seconds to wait for each response")
    g.addoption("--run-dir", default=None, help="folder for this run's reports")
    g.addoption("--restora-config", default=None, help="settings file (default: restora.toml)")
    g.addoption("--brain", choices=["ai", "rules", "off"], default=None, help="ai (default) | rules | off")
    g.addoption("--heal", choices=["auto", "suggest", "off"], default=None, help="auto (default) | suggest | off")
    g.addoption("--model", default=None, help="local AI model, e.g. qwen3:4b-instruct")
    g.addoption("--ai-url", default=None, help="Ollama or OpenAI-compatible server URL")
    g.addoption("--no-reorder", action="store_true", default=False, help="keep the sheet order")
    g.addoption("--strict-compare", action="store_true", default=False, help="exact comparison (no dynamic values)")
    g.addoption("--restora-seed", type=int, default=None, help="same test data every run (any number but 0)")
    g.addoption("--restora-base-url", default=None, help="base address for rows whose URL starts with /")


def build_settings(opt) -> settings_mod.Settings:
    s = settings_mod.load(opt("--restora-config"), ROOT)
    if opt("--brain"):
        s.brain.mode = opt("--brain")
    if opt("--heal"):
        s.heal.mode = opt("--heal")
    if opt("--model"):
        s.ai.model = opt("--model")
    if opt("--ai-url"):
        s.ai.url = opt("--ai-url")
    if opt("--no-reorder"):
        s.brain.reorder = False
    if opt("--strict-compare"):
        s.brain.smart_compare = False
    if opt("--restora-seed") is not None:
        s.brain.seed = opt("--restora-seed")
    if opt("--restora-base-url"):
        s.brain.base_url = opt("--restora-base-url")
    if opt("--insecure"):
        s.run.insecure = True
    if opt("--api-timeout"):
        s.run.timeout = opt("--api-timeout")
    return s


def pytest_configure(config):
    for stream in (sys.stdout, sys.stderr):                # never crash on a Windows console code page
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    config._restora = None


def _session(config) -> Session:
    if config._restora is None:
        s = build_settings(config.getoption)
        config._restora = Session(s, ROOT)
    return config._restora


def pytest_generate_tests(metafunc):
    if "row" not in metafunc.fixturenames:
        return
    path = Path(metafunc.config.getoption("--apis"))
    try:
        rows = load_sheet(path).rows
    except InputError as e:
        raise pytest.UsageError(str(e))
    session = _session(metafunc.config)
    rows = session.prepare(rows)
    ids = [f"{r.number:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', r.name).strip('_')[:40]}" for r in rows]
    metafunc.parametrize("row", rows, ids=ids)


@pytest.fixture(scope="session")
def session(request):
    return _session(request.config)


@pytest.fixture(scope="session")
def input_name(request):
    return Path(request.config.getoption("--apis")).name


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    sess = config._restora
    if sess is None or not sess.records:
        return
    run_dir = config.getoption("--run-dir")
    apis = Path(config.getoption("--apis"))
    if not run_dir:
        run_dir = ROOT / "reports" / f"{apis.stem}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    paths = sess.write(Path(run_dir), apis.name, apis.suffix)
    c = sess.counts()
    b = sess.brain_summary()
    tr = terminalreporter
    tr.section("Restora")
    tr.write_line(f"{len(sess.records)} request(s): {c['PASSED']} passed, {c['HEALED']} healed, "
                  f"{c['FAILED']} failed, {c['SKIPPED']} skipped")
    labels = (("filled", "gap filled", "gaps filled"), ("moved", "row moved", "rows moved"),
              ("repaired", "input repaired", "inputs repaired"), ("healed", "request healed", "requests healed"),
              ("dynamic", "changing value accepted", "changing values accepted"))
    done = [f"{b[k]} {one if b[k] == 1 else many}" for k, one, many in labels if b.get(k)]
    tr.write_line("Brain       : " + (", ".join(done) if done else "nothing needed fixing"))
    tr.write_line(f"AI          : {b['ai']}" + (f"  ({b['ai_calls']} call(s), {b['ai_seconds']} s)" if b.get("ai_calls") else ""))
    tr.write_line(f"HTML report : {paths['html']}")
    tr.write_line(f"Corrected   : {paths.get('fixed_xlsx') or paths['fixed_csv']}   (review it, then use it as your sheet)")
    tr.write_line(f"Results CSV : {paths['csv']}")
    allure_dir = Path(run_dir) / "allure-results"
    if allure_dir.exists():
        note = "" if shutil.which("allure") else "   (needs the Allure command-line tool - optional)"
        tr.write_line(f'Allure      : allure serve "{allure_dir}"{note}')
    for w in sess.settings.warnings:
        tr.write_line(f"Settings    : {w}")
    if sess.plugins and sess.plugins.errors:
        for e in sess.plugins.errors:
            tr.write_line(f"Plug-in error: {e}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    rep.restora = getattr(item, "_restora_result", None)


def pytest_report_teststatus(report, config):
    if report.when == "call" and report.passed and getattr(report, "restora", None) == "HEALED":
        return "healed", "H", ("HEALED", {"purple": True})


def pytest_sessionfinish(session, exitstatus):
    sess = getattr(session.config, "_restora", None)
    if sess is not None and sess.records and exitstatus in (0, 1):
        session.exitstatus = sess.exit_code()
