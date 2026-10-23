# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""One run of Restora: settings + brain + plug-ins + the records it produced.

Shared by the command line (through pytest) and by Studio (the web app), so both behave exactly the same.
"""
from __future__ import annotations

import datetime
import json
import time
from collections import Counter
from pathlib import Path

from . import plugin, report
from .brain import Brain
from .brain import fixed_sheet
from .client import Settings as HttpSettings
from .runner import execute
from .settings import Settings


class Session:
    def __init__(self, settings: Settings, root: Path, on_event=None, check_ai: bool = True):
        self.settings = settings
        self.root = Path(root)
        self.http = HttpSettings(insecure=settings.run.insecure, timeout=settings.run.timeout)
        self.plugins = plugin.load(settings.plugins_folder, self.root) if settings.plugins_enabled else None
        self.ai = None
        mode = settings.brain.mode
        if mode == "ai" and check_ai:
            from .brain.ai import connect
            ai = connect(settings.ai, settings.ai_key)
            self.ai_message = ai.message
            self.ai = ai if ai.ready else None
        elif mode == "ai":
            self.ai_message = "AI check skipped - using rules only."
        elif mode == "rules":
            self.ai_message = "Brain: rules only (no AI model is used)."
        else:
            self.ai_message = "Brain is off: rows run exactly as typed (only explicit {{...}} references are filled)."
        self.brain = Brain(settings, ai=self.ai, plugins=self.plugins)
        self.records: list = []
        self.plan = None
        self.started = time.time()
        self.on_event = on_event

    # ------------------------------------------------------------------ running
    def prepare(self, rows: list) -> list:
        self.plan = self.brain.plan(rows)
        return self.plan.rows

    def run_row(self, row):
        rec = execute(row, self.http, self.brain, self.plugins, self.on_event)
        self.records.append(rec)
        return rec

    def run_all(self, rows: list) -> list:
        for row in self.prepare(rows):
            self.run_row(row)
        return self.records

    # ------------------------------------------------------------------ results
    def counts(self) -> Counter:
        return Counter(r.result for r in self.records)

    def brain_summary(self) -> dict:
        ins = self.plan.insights if self.plan else []
        kinds = Counter(i.kind for i in ins)
        return {
            "filled": sum(len(r.filled) for r in self.records),
            "healed": sum(1 for r in self.records if r.result == "HEALED"),
            "heal_attempts": sum(len(r.heal_attempts) for r in self.records),
            "moved": kinds.get("order", 0),
            "repaired": kinds.get("repair", 0) + kinds.get("url", 0) + kinds.get("method", 0),
            "dynamic": sum(len(r.dynamic) for r in self.records),
            "negative": kinds.get("negative", 0),
            "explained": sum(1 for r in self.records if r.explanation),
            "ai_calls": self.ai.client.calls if self.ai else 0,
            "ai_seconds": round(self.ai.client.seconds, 1) if self.ai else 0.0,
            "ai": self.ai_message,
            "mode": self.settings.brain.mode,
            "heal_mode": self.settings.heal.mode,
        }

    def exit_code(self) -> int:
        c = self.counts()
        if c.get("FAILED"):
            return 1
        if c.get("HEALED") and self.settings.heal.count_healed_as == "fail":
            return 1
        return 0

    def write(self, run_dir: Path, input_name: str, input_suffix: str = ".csv") -> dict:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = {"input": input_name,
                "started": datetime.datetime.fromtimestamp(self.started).strftime("%d %b %Y, %H:%M:%S"),
                "duration": time.time() - self.started,
                "brain": self.brain_summary(),
                "insights": [i.as_dict() for i in (self.plan.insights if self.plan else [])]}
        paths = {"html": report.write_html(self.records, run_dir / "report.html", meta),
                 "csv": report.write_csv(self.records, run_dir / "results.csv")}
        rows = fixed_sheet.build(self.records, capture=self.settings.brain.capture_expected)
        stem = Path(input_name).stem
        paths["fixed_csv"] = fixed_sheet.write_csv(rows, run_dir / f"{stem}_corrected.csv")
        if input_suffix.lower() in (".xlsx", ".xlsm"):
            x = fixed_sheet.write_xlsx(rows, run_dir / f"{stem}_corrected.xlsx")
            if x:
                paths["fixed_xlsx"] = x
        (run_dir / "brain.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        paths["brain"] = run_dir / "brain.json"
        return paths
