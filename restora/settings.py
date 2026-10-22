"""All of Restora's knobs in one place: defaults <- restora.toml <- command-line flags."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

DEFAULT_MODEL = "qwen3:4b-instruct"


@dataclass
class BrainSettings:
    mode: str = "ai"                 # ai | rules | off
    reorder: bool = True             # move rows when one clearly needs a value another returns
    smart_compare: bool = True       # accept ids / timestamps / tokens that change on every call
    capture_expected: bool = True    # blank ExpectedResponse -> captured into the corrected sheet
    seed: int = 0                    # 0: new test data every run; any other number: the same data every run
    base_url: str = ""               # used for rows whose URL starts with /


@dataclass
class HealSettings:
    mode: str = "auto"               # auto | suggest | off
    max_attempts: int = 3
    statuses: list = field(default_factory=lambda: [400, 401, 403, 405, 406, 409, 415, 422, 428, 429])
    count_healed_as: str = "pass"    # pass | fail  (decides the exit code in CI)
    check_negative: bool = True      # make sure a negative test is refused for its own reason (see brain/guard.py)


@dataclass
class AISettings:
    provider: str = "ollama"         # ollama | openai  (openai = any OpenAI-compatible server)
    url: str = "http://localhost:11434"
    model: str = DEFAULT_MODEL
    timeout: float = 120.0
    temperature: float = 0.0
    api_key_env: str = ""            # name of an environment variable that holds a key (cloud servers only)
    explain_failures: bool = True    # plain-English explanation for each failed row
    max_calls: int = 40              # per run, so a big sheet never turns into a long AI session


@dataclass
class RunSettings:
    timeout: float = 30.0
    insecure: bool = False


@dataclass
class Settings:
    brain: BrainSettings = field(default_factory=BrainSettings)
    heal: HealSettings = field(default_factory=HealSettings)
    ai: AISettings = field(default_factory=AISettings)
    run: RunSettings = field(default_factory=RunSettings)
    plugins_folder: str = "plugins"
    plugins_enabled: bool = True
    generators: dict = field(default_factory=dict)
    synonyms: dict = field(default_factory=dict)
    source: str = ""                 # the settings file that was read, if any
    warnings: list = field(default_factory=list)

    # handy flat views used across the code base
    @property
    def reorder(self) -> bool:
        return self.brain.reorder and self.brain.mode != "off"

    @property
    def base_url(self) -> str:
        return self.brain.base_url

    @property
    def ai_key(self) -> str | None:
        return os.environ.get(self.ai.api_key_env) if self.ai.api_key_env else None


def _toml_loads(text: str) -> dict:
    try:
        import tomllib                      # Python 3.11+
    except ModuleNotFoundError:             # pragma: no cover
        import tomli as tomllib             # type: ignore
    return tomllib.loads(text)


def _apply(obj, data: dict, where: str, warnings: list) -> None:
    names = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        k = key.replace("-", "_")
        if k not in names:
            warnings.append(f"{where}: unknown setting '{key}' (ignored)")
            continue
        current = getattr(obj, k)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, f"{where}.{key}", warnings)
        else:
            try:
                if isinstance(current, bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, int):
                    value = int(value)
            except (TypeError, ValueError):
                warnings.append(f"{where}.{key}: '{value}' is not a valid value (ignored)")
                continue
            setattr(obj, k, value)


def load(path: str | Path | None = None, root: Path | None = None) -> Settings:
    s = Settings()
    candidates = [Path(path)] if path else [Path.cwd() / "restora.toml"] + ([root / "restora.toml"] if root else [])
    for p in candidates:
        if p.exists():
            try:
                data = _toml_loads(p.read_text(encoding="utf-8-sig"))
            except ModuleNotFoundError:
                s.warnings.append("restora.toml was found but cannot be read on this Python (pip install tomli).")
                return s
            except Exception as e:  # noqa: BLE001 - a broken settings file should explain itself, not crash
                s.warnings.append(f"{p.name} could not be read ({e}); default settings are used.")
                return s
            sections = {"brain": s.brain, "heal": s.heal, "ai": s.ai, "run": s.run}
            for section, value in data.items():
                if section in sections and isinstance(value, dict):
                    _apply(sections[section], value, section, s.warnings)
                elif section == "plugins" and isinstance(value, dict):
                    s.plugins_folder = str(value.get("folder", s.plugins_folder))
                    s.plugins_enabled = bool(value.get("enabled", s.plugins_enabled))
                elif section == "generators" and isinstance(value, dict):
                    s.generators = dict(value)
                elif section == "synonyms" and isinstance(value, dict):
                    s.synonyms = dict(value)
                else:
                    s.warnings.append(f"{p.name}: unknown section [{section}] (ignored)")
            s.source = str(p)
            break
    _validate(s)
    return s


def _validate(s: Settings) -> None:
    if s.brain.mode not in ("ai", "rules", "off"):
        s.warnings.append(f"brain.mode '{s.brain.mode}' is not ai / rules / off; using 'ai'.")
        s.brain.mode = "ai"
    if s.heal.mode not in ("auto", "suggest", "off"):
        s.warnings.append(f"heal.mode '{s.heal.mode}' is not auto / suggest / off; using 'auto'.")
        s.heal.mode = "auto"
    if s.ai.provider not in ("ollama", "openai"):
        s.warnings.append(f"ai.provider '{s.ai.provider}' is not ollama / openai; using 'ollama'.")
        s.ai.provider = "ollama"
    if s.heal.count_healed_as not in ("pass", "fail"):
        s.heal.count_healed_as = "pass"
