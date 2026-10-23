#!/usr/bin/env python
# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Record Restora's demo video: a scripted walk-through captured frame by frame from the browser, encoded to MP4.

Everything on screen is real: Studio, the Demo Bank, the brain, the local AI model and the commands.
Only the pacing is scripted (captions, a visible cursor, and the AI's thinking time shown faster).

    pip install -r requirements-demo.txt
    python tools/demo_video.py                 (Windows: tools\\record_demo.bat)
    python tools/demo_video.py --headless --browser chromium --skip-ai      (quick check without a model)

Output: demo_video_out/restora_demo.mp4
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).resolve().parent / "demo_assets"
sys.path.insert(0, str(ROOT))
W, H = 1920, 1080                 # the video
VW, VH, DSF = 1440, 810, 4 / 3    # the page: a laptop-sized screen, drawn sharp at 1080p
WINDOWS = os.name == "nt"

OVERLAY_JS = r"""
(() => {
  if (window.__restoraDemo) return; window.__restoraDemo = true;
  const css = `
  #__cap { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); max-width: 1240px; z-index: 2147483646;
    background: rgba(20, 32, 51, .94); color: #fff; font: 600 23px/1.35 "Hyperlegible", "Segoe UI", sans-serif;
    padding: 11px 22px; border-radius: 12px; text-align: center; opacity: 0; transition: opacity .35s ease; pointer-events: none;
    box-shadow: 0 18px 40px -18px rgba(0,0,0,.55); }
  #__cap.on { opacity: 1; }
  #__cur { position: fixed; left: 0; top: 0; width: 24px; height: 24px; z-index: 2147483647; pointer-events: none;
    transform: translate(720px, 450px); }
  #__cur svg { width: 24px; height: 24px; filter: drop-shadow(0 2px 3px rgba(0,0,0,.45)); }
  .__ripple { position: fixed; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 50%; z-index: 2147483646;
    border: 3px solid #6a3fd0; pointer-events: none; animation: __rip .55s ease-out forwards; }
  @keyframes __rip { from { transform: scale(.4); opacity: 1; } to { transform: scale(3.2); opacity: 0; } }
  #__ff { position: fixed; top: 16px; right: 20px; z-index: 2147483646; background: #6a3fd0; color: #fff; pointer-events: none;
    font: 700 18px "Hyperlegible", "Segoe UI", sans-serif; padding: 5px 12px; border-radius: 999px; display: none; }
  #__ff.on { display: block; }`;
  function ensure() {
    if (!document.body || document.getElementById("__cap")) return;
    const s = document.createElement("style"); s.textContent = css; document.head.appendChild(s);
    const cap = document.createElement("div"); cap.id = "__cap"; document.body.appendChild(cap);
    const cur = document.createElement("div"); cur.id = "__cur";
    cur.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 2l15 11.5-6.6.9 3.9 7.4-2.9 1.4-3.8-7.5L4 20.4z" fill="#fff" stroke="#142033" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    document.body.appendChild(cur);
    const ff = document.createElement("div"); ff.id = "__ff"; document.body.appendChild(ff);
    if (window.__curPos) cur.style.transform = `translate(${window.__curPos[0]}px, ${window.__curPos[1]}px)`;
  }
  document.addEventListener("DOMContentLoaded", ensure);
  window.__ensure = ensure;
  window.__caption = (text) => { ensure(); const c = document.getElementById("__cap"); if (!c) return;
    if (!text) { c.classList.remove("on"); return; } c.textContent = text; c.classList.add("on"); };
  window.__cursorTo = (x, y, ms) => { ensure(); const c = document.getElementById("__cur"); window.__curPos = [x, y];
    c.style.transition = `transform ${ms}ms cubic-bezier(.25,.75,.25,1)`; c.style.transform = `translate(${x}px, ${y}px)`; };
  window.__clickFx = (x, y) => { const r = document.createElement("div"); r.className = "__ripple"; r.style.left = x + "px"; r.style.top = y + "px";
    document.body.appendChild(r); setTimeout(() => r.remove(), 700); };
  window.__fastForward = (text) => { ensure(); const f = document.getElementById("__ff"); if (!f) return; f.textContent = text || ""; f.classList.toggle("on", !!text); };
})();
"""


class Recorder:
    """Chrome DevTools screencast: a JPEG for every frame that changes, with its timestamp."""

    def __init__(self, page, folder: Path):
        self.page, self.folder = page, folder
        self.frames: list = []
        self.fast: list = []                 # (first frame index, last frame index, factor)
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._frame)
        self.started_at = None
        self.stopped_at = None

    def _frame(self, params):
        n = len(self.frames)
        path = self.folder / f"{n:06d}.jpg"
        path.write_bytes(base64.b64decode(params["data"]))
        self.frames.append((path, float(params.get("metadata", {}).get("timestamp") or time.time()), time.time()))
        with contextlib.suppress(Exception):
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    def start(self):
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": W, "maxHeight": H, "everyNthFrame": 1})
        self.started_at = self.started_at or time.time()

    def stop(self):
        with contextlib.suppress(Exception):
            self.cdp.send("Page.stopScreencast")
        self.stopped_at = time.time()

    @contextlib.contextmanager
    def faster(self, factor: float, page_label: str):
        self.page.evaluate("t => window.__fastForward(t)", f"{factor:g}× faster")
        first = len(self.frames)
        try:
            yield
        finally:
            self.page.evaluate("t => window.__fastForward(t)", "")
            self.fast.append((first, len(self.frames), factor))

    def encode(self, out: Path, ffmpeg: str, fps: int = 30) -> Path:
        if not self.frames:
            raise RuntimeError("no frames were captured")
        lines = []
        for i, (path, ts, arrived) in enumerate(self.frames):
            if i + 1 < len(self.frames):
                dur = max(0.0, self.frames[i + 1][1] - ts)
            else:
                dur = max(1.0, (self.stopped_at or arrived) - arrived)
            for a, b, f in self.fast:
                if a <= i < b:
                    dur /= f
            dur = min(dur, 30.0)
            lines.append(f"file '{path.name}'\nduration {dur:.4f}")
        lines.append(f"file '{self.frames[-1][0].name}'")
        listing = self.folder / "frames.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
               "-vf", f"fps={fps},scale={W}:{H}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
               "-crf", "18", "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, check=True, cwd=self.folder)
        return out


def pytest_output():
    """A filter for pytest's output: hide the preamble and the FAILURES section, keep results and the summary."""
    state = {"skip": False}
    noise = ("platform ", "cachedir", "rootdir", "configfile", "plugins:", "collecting", "collected", "cachedir:")

    def keep(line: str) -> bool:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
        if re.search(r"=+ FAILURES =+", plain):
            state["skip"] = True
            return False
        if re.search(r"=+ (Restora|short test summary info) =+", plain):
            state["skip"] = False
        return not state["skip"] and bool(plain.strip()) and not plain.startswith(noise)
    return keep


class Director:
    def __init__(self, page, rec: Recorder, studio: str, skip_ai: bool, before_cli=None, shots: Path | None = None):
        self.page, self.rec, self.studio, self.skip_ai, self.shots = page, rec, studio, skip_ai, shots
        self.before_cli = before_cli or (lambda: None)      # frees the Demo Bank port for the command-line scenes
        self.cursor = (720, 450)
        self.log: list = []

    # ---------------------------------------------------------------- basics
    def hold(self, ms: int):
        self.page.wait_for_timeout(ms)

    def goto(self, url: str):
        self.page.goto(url)
        self.page.wait_for_load_state("load")
        self.page.evaluate("p => { window.__curPos = p; window.__ensure(); }", list(self.cursor))
        self.rec.stop()
        self.rec.start()

    def caption(self, text: str, hold: int = 0):
        self.page.evaluate("t => window.__caption(t)", text)
        self.log.append((round(time.time() - (self.rec.started_at or time.time()), 1), text))
        if hold:
            self.hold(hold)

    def move(self, locator, ms: int = 650):
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if not box:
            return None
        x, y = box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2
        self.page.evaluate("([x, y, ms]) => window.__cursorTo(x, y, ms)", [x, y, ms])
        self.cursor = (x, y)
        self.hold(ms + 120)
        return x, y

    def click(self, selector_or_locator, ms: int = 650):
        loc = self.page.locator(selector_or_locator).first if isinstance(selector_or_locator, str) else selector_or_locator
        pos = self.move(loc, ms)
        if pos:
            self.page.evaluate("([x, y]) => window.__clickFx(x, y)", list(pos))
        loc.click()
        self.hold(250)

    def type(self, selector: str, text: str, delay: int = 42):
        self.click(selector)
        self.page.locator(selector).first.type(text, delay=delay)

    def snapshot(self, name: str):
        """A clean PNG for the README (no caption, cursor or toast). The video holds its last frame meanwhile."""
        if not self.shots:
            return
        self.rec.stop()
        hide = ("v => { for (const id of ['__cap', '__cur', '__ff', 'toast']) { const e = document.getElementById(id); "
                "if (e) e.style.visibility = v; } document.querySelectorAll('.__ripple').forEach(r => r.remove()); }")
        self.page.evaluate(hide, "hidden")
        self.page.screenshot(path=str(self.shots / f"{name}.png"))
        self.page.evaluate(hide, "")
        self.rec.start()

    def wait_summary(self, timeout: int = 180000):
        self.page.wait_for_selector("#summary:not([hidden])", timeout=timeout)
        self.page.wait_for_function("() => !document.querySelector('#runBtn').disabled", timeout=timeout)

    def row(self, name: str):
        return self.page.locator("#grid tbody tr", has_text=name).first

    # ---------------------------------------------------------------- terminal + code pages
    def terminal(self, title: str = "Command Prompt - Restora"):
        self.goto((ASSETS / "terminal.html").as_uri())
        self.page.evaluate("t => term.title(t)", title)

    def command(self, shown: str, args: list, slow: float = 0.045, max_lines: int = 60, keep=None):
        """Type a command in the terminal page, run it for real, and stream its output."""
        self.page.evaluate("c => term.prompt(c)", shown)
        self.hold(int(len(shown) * 45) + 500)
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PY_COLORS": "1", "FORCE_COLOR": "1", "COLUMNS": "108"}
        if WINDOWS:
            proc_args = ["cmd", "/c", "run.bat", *args]
        else:
            proc_args = [sys.executable, "run.py", *args]
        proc = subprocess.Popen(proc_args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        shown_lines = 0
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            line = line.replace(str(ROOT) + os.sep, "").replace(str(ROOT), ".")
            if keep is not None and not keep(line):
                continue
            if shown_lines >= max_lines:
                continue
            self.page.evaluate("l => term.line(l)", line)
            shown_lines += 1
            self.hold(int(slow * 1000))
        proc.wait()
        self.hold(400)
        self.page.evaluate("() => term.idle()")
        return proc.returncode

    def code(self, name: str, rel_path: str, lang: str, highlight=(), first: int = 1, last: int | None = None):
        """Show a slice of a real file. `highlight` holds regular expressions: the lines they match are marked."""
        self.goto((ASSETS / "code.html").as_uri())
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        lines = text.splitlines()[first - 1:last]
        marked = [i + 1 for i, line in enumerate(lines) if any(re.search(rx, line) for rx in highlight)]
        self.page.evaluate("([n, p, t, l, h, s]) => code.show(n, p, t, l, h, s)",
                           [name, rel_path, "\n".join(lines), lang, marked, first])

    def ai_rows(self) -> list:
        """What happened to the rows the AI wrote (from Studio's own state)."""
        return self.page.evaluate("""() => {
          const names = new Set(state.records.filter(r => r.ai).map(r => r.name));
          return Object.values(state.results).filter(x => names.has(x.name)).map(x => ({ name: x.name, result: x.result,
            status: x.http_status, explanation: x.explanation || "",
            guard: (x.heal_attempts || []).some(a => a.source === "guard") }));
        }""")

    def card(self, **params):
        self.goto((ASSETS / "card.html").as_uri() + "?" + urlencode(params, quote_via=quote))


# ======================================================================== the script

def scenes(d: Director, author: str, repo: str, model: str):
    # ---- 1. title
    d.card(sub="Self-healing API tests from a spreadsheet, with a local AI brain",
           lines="Free and open source|Runs on your own computer: no data leaves it", by=author)
    d.hold(5200)

    # ---- 2. the problem
    d.goto(d.studio + "?pace=420")
    d.hold(900)
    d.caption("Restora Studio runs on this PC. Let's open a typical test sheet, the way people really write them.", 3600)
    d.click("#demoBtn")
    d.page.wait_for_selector("#grid tbody tr")
    d.caption("Login is at the bottom. Tokens and ids are left as <token> and {customerId}.", 300)
    d.move(d.page.locator("mark.gap").first, 900)
    d.hold(3000)
    d.caption("One body is half-written JSON, one method is blank, most URLs are only paths.", 300)
    d.move(d.row("Open a new customer").locator("td.c-body"), 800)
    d.hold(3400)
    d.caption("First, the way any normal tool runs it: exactly as typed.", 300)
    d.click('[data-brain="off"]')
    d.hold(600)
    d.click("#runBtn")
    d.wait_summary()
    d.hold(600)
    d.caption("12 out of 12 fail. Now switch the brain on.", 300)
    d.move(d.page.locator("#summary .tally"), 700)
    d.hold(2600)

    # ---- 3. the brain
    d.click('[data-brain="ai"]')
    d.page.wait_for_timeout(1200)
    d.caption("Before sending anything, the brain plans: Login moves up, short URLs are completed, the JSON is repaired.", 300)
    d.move(d.page.locator("#notes .note").first, 800)
    d.hold(4200)
    d.click("#runBtn")
    d.caption("Now it runs. Gaps are filled from earlier responses; rejected requests are healed by reading the API's own errors.")
    d.wait_summary()
    d.hold(700)
    d.caption("6 passed, 5 healed, 1 failed. Every change appears on the sheet as a tracked change.", 300)
    d.move(d.page.locator("#summary .tally"), 700)
    d.hold(3300)
    d.click(d.row("Open a new customer"))
    d.caption("Healed in two steps: the API said 401, so the brain added the Login token; then 'phone must not be blank', so it added one.", 300)
    d.move(d.page.locator("#rowPanel ol.steps").first, 800)
    d.hold(5600)
    d.click(d.row("Open a savings account"))
    d.snapshot("studio-after-run")
    d.caption("Here the API wanted 'SAVINGS', not 'savings', and a number, not \"5000\". Fixed in one go.", 300)
    d.move(d.row("Open a savings account").locator("td.c-body"), 700)
    d.hold(4800)
    d.click(d.row("Monthly statement"))
    d.caption("The one failure is a real server bug (HTTP 500). Restora explains it in plain English and never hides it.", 300)
    d.move(d.page.locator("#rowPanel .explain").first, 700)
    d.hold(5200)
    d.click(d.row("Login with a wrong password"))
    d.caption("Negative tests stay negative: a rejected login is never 'fixed' into a pass.", 300)
    d.hold(3800)

    # ---- 4. a test from plain English (the local AI)
    if not d.skip_ai:
        d.caption("Need another test? Describe it in plain English.", 300)
        d.type("#askInput", "Check that a transfer with a negative amount is rejected")
        d.hold(500)
        d.click("#askBtn")
        d.caption(f"{model} is writing the row, on this PC's own GPU. No cloud, no cost.")
        with d.rec.faster(4, "AI"):
            d.page.wait_for_selector(".ai-written", timeout=240000)
        d.hold(800)
        d.caption("A new row, marked for review. Run again to check it.", 300)
        d.move(d.page.locator(".ai-written").first, 700)
        d.hold(3200)
        d.click("#runBtn")
        d.wait_summary()
        d.hold(600)
        d.click(d.page.locator("#grid tbody tr", has=d.page.locator(".ai-written")).first)
        got = (d.ai_rows() or [{}])[0]
        if got.get("result") == "HEALED" and got.get("guard"):
            d.snapshot("studio-wrong-reason")
            d.caption("At first the API refused it for the wrong reason: a header it needs was missing.", 300)
            d.move(d.page.locator("#rowPanel ol.steps").first, 800)
            d.hold(4200)
            d.caption("Most tools would call that a pass. Restora added only the header and asked again: "
                      "the API still refused the negative amount. Now the test checks what it says.", 6400)
        elif got.get("result") == "PASSED":
            d.caption("The API refused the negative amount, exactly as the new test expects.", 4400)
        elif got.get("result") == "FAILED" and 200 <= (got.get("status") or 0) < 300:
            d.caption("The API accepted a negative amount. Restora reports it as a bug instead of a pass.", 5200)
        else:
            d.caption("The new test failed: " + (got.get("explanation") or "see the row details.")[:160], 6000)

    # ---- 5. what you take away
    href = d.page.locator("#summary a", has_text="Open full report").get_attribute("href")
    d.caption("Download the corrected sheet, or open one self-contained report to share.", 300)
    d.move(d.page.locator("#summary .actions"), 700)
    d.hold(2600)
    d.goto(d.studio.rstrip("/") + href)
    d.caption("The report: what was sent and received, how each row was healed, and why anything failed.", 300)
    d.hold(2500)
    d.page.mouse.wheel(0, 700)
    d.hold(2600)

    # ---- 6. power users: the command line, settings and plug-ins
    d.before_cli()
    d.terminal()
    d.caption("Power users get the same brain on the command line: here, the plan only, nothing sent.", 300)
    d.command(r"run.bat examples\demo_bank_messy.csv --plan", ["examples/demo_bank_messy.csv", "--plan"], slow=0.06)
    d.hold(3000)
    d.page.evaluate("() => term.clear()")
    d.caption("Rules-only mode for CI: same decisions every run, JUnit output, exit code 1 for the real bug.", 300)
    d.command(r"run.bat --demo --brain rules --junit results.xml",
              ["--demo", "--brain", "rules", "--junit", "results.xml"], slow=0.07, keep=pytest_output())
    d.hold(4200)
    d.code("restora.toml", "restora.toml", "toml", highlight=[r"^mode = "], first=4, last=23)
    d.caption("Every behaviour is a setting: what the brain may do, and whether healing is applied, only suggested, or off.", 5200)
    d.code("restora.toml", "restora.toml", "toml", highlight=[r"^(provider|url|model) = "], first=24, last=40)
    d.caption("The model and the server: Ollama, or any OpenAI-compatible server such as LM Studio or llama.cpp.", 4800)
    d.code("sign_requests.py", "examples/power_user/plugins/sign_requests.py", "python",
           highlight=[r"before_request", r"def sign_", r"/beneficiaries", r"X-Signature", r"body = "], first=6)
    d.caption("Plug-ins add what no AI could guess, like an HMAC signature a payment API demands. Six lines of Python.", 5200)
    d.terminal()
    d.command(r"run.bat examples\power_user\beneficiaries.csv --demo --config examples\power_user\restora.toml",
              ["examples/power_user/beneficiaries.csv", "--demo", "--config", "examples/power_user/restora.toml"], slow=0.07,
              keep=pytest_output())
    d.caption("Signed by the plug-in, the beneficiary is accepted. Suggest mode reports a fix without applying it.", 4600)
    d.page.evaluate("() => term.clear()")
    d.caption("And Restora tests itself: more than a hundred checks, in seconds.", 300)
    d.command("run.bat --selftest", ["--selftest"], slow=0.05)
    d.hold(3000)

    # ---- 7. close
    d.card(sub="Rules first, local AI second. Never hides a real bug.",
           lines=f"{repo}|Apache-2.0 licensed: free to use and change, with credit", by=author, nospec="1")
    d.hold(5200)


def _stop_demo(studio) -> None:
    """Studio's Demo Bank holds port 18099; the command-line scenes start their own (run.bat --demo)."""
    srv = getattr(studio, "demo", None)
    if srv is not None and srv != "external":
        srv.shutdown()
        srv.server_close()
    studio.demo = None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", default="msedge" if WINDOWS else "chromium", help="msedge | chrome | chromium")
    ap.add_argument("--headless", action="store_true", help="do not show the browser window while recording")
    ap.add_argument("--skip-ai", action="store_true", help="leave out the plain-English (AI) scene")
    ap.add_argument("--author", default="Built by Vedant Surve")
    ap.add_argument("--repo", default="github.com/VYADDISURVE07/restora")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default=str(ROOT / "demo_video_out"))
    ap.add_argument("--ai-url", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg is needed: pip install -r requirements-demo.txt")

    from restora import settings as settings_mod
    from restora.studio.server import start_in_background
    s = settings_mod.load(None, ROOT)
    s.brain.mode = "rules" if args.skip_ai else "ai"
    if args.ai_url:
        s.ai.url = args.ai_url
    server, url, studio = start_in_background(args.port, s)
    studio.start_demo()
    if not args.skip_ai:
        st = studio.check_ai()
        print(st["message"], flush=True)
        if not st["ready"]:
            sys.exit("The local AI model is not ready. Run ai_setup.bat first, or use --skip-ai.")
    model = s.ai.model

    out = Path(args.out)
    frames = out / "frames"
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir(parents=True)
    print(f"Recording to {out} ...", flush=True)
    with sync_playwright() as p:
        launch = {"headless": args.headless, "ignore_default_args": ["--enable-automation"],
                  "args": [f"--window-size={VW},{VH}", "--start-fullscreen", "--disable-infobars"] if not args.headless else []}
        if args.browser in ("msedge", "chrome"):
            launch["channel"] = args.browser
        browser = p.chromium.launch(**launch)
        context = browser.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=DSF, color_scheme="light")
        context.add_init_script(OVERLAY_JS)
        page = context.new_page()
        rec = Recorder(page, frames)
        d = Director(page, rec, url, args.skip_ai, before_cli=lambda: _stop_demo(studio), shots=out)
        try:
            page.goto("about:blank")
            rec.start()
            scenes(d, args.author, args.repo, model)
        finally:
            rec.stop()
            (out / "captions.json").write_text(json.dumps(d.log, indent=1), encoding="utf-8")
            browser.close()
    server.shutdown()
    mp4 = rec.encode(out / "restora_demo.mp4", ffmpeg)
    print(f"Done: {mp4}  ({len(rec.frames)} frames captured)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
