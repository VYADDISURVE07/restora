"""Plug-in hooks for power users: change anything Restora does with a few lines of Python.

Put a .py file in the plugins/ folder (see restora.toml [plugins]). Example:

    from restora import plugin

    @plugin.generator("pan", "panNumber")          # your own test data for a field name
    def my_pan(field, hint):
        return "ABCPE1234F"

    @plugin.before_request                         # change every request just before it is sent
    def sign(request, row):
        request.headers["X-Signature"] = my_hmac(request.body)

    @plugin.after_response                         # look at every response
    def log(response, row):
        print(row.name, response.status)

    @plugin.healer                                 # your own healing rule, tried before the built-in ones
    def needs_channel(attempt):
        if attempt.status == 400 and "channel" in attempt.text.lower():
            return attempt.patch(header={"X-Channel": "MOBILE"}, reason="this API needs a channel header")

    @plugin.dynamic                                # mark more response fields as 'changes every call'
    def ignore_nonce(path, expected, actual):
        return "nonce changes every call" if path.endswith(".nonce") else None
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Registry:
    generators: dict = field(default_factory=dict)      # normalised field name -> fn(field, hint)
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    healers: list = field(default_factory=list)
    dynamics: list = field(default_factory=list)
    loaded: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def clear(self) -> None:
        self.__init__()


REGISTRY = Registry()


def generator(*field_names: str) -> Callable:
    from .brain.names import norm

    def deco(fn):
        for name in field_names or (fn.__name__,):
            REGISTRY.generators[norm(name)] = fn
        return fn
    return deco


def before_request(fn: Callable) -> Callable:
    REGISTRY.before.append(fn)
    return fn


def after_response(fn: Callable) -> Callable:
    REGISTRY.after.append(fn)
    return fn


def healer(fn: Callable) -> Callable:
    REGISTRY.healers.append(fn)
    return fn


def dynamic(fn: Callable) -> Callable:
    REGISTRY.dynamics.append(fn)
    return fn


@dataclass
class Attempt:
    """What a @healer function receives."""
    request: Any            # .method .url .headers .body
    response: Any           # .status .headers .text .json_value
    row: Any
    memory: Any

    @property
    def status(self):
        return self.response.status

    @property
    def text(self) -> str:
        return self.response.text or ""

    def patch(self, *, header: dict | None = None, body: dict | None = None, query: dict | None = None,
              method: str | None = None, remove_body: list | None = None, reason: str = "a plug-in fixed it"):
        from .brain.heal import Change, Patch
        changes = []
        for k, v in (header or {}).items():
            changes.append(Change("header", "set", k, v, f"set header {k}"))
        for k, v in (body or {}).items():
            changes.append(Change("body", "set", k, v, f"set {k}"))
        for k in (remove_body or []):
            changes.append(Change("body", "remove", k, None, f"removed {k}"))
        for k, v in (query or {}).items():
            changes.append(Change("query", "set", k, v, f"set query {k}"))
        if method:
            changes.append(Change("method", "set", "", method.upper(), f"used {method.upper()}"))
        return Patch(changes, reason, source="plugin", confidence=1.0)


def load(folder: str | Path, root: Path | None = None) -> Registry:
    """Import every .py file in the plug-ins folder (once per run)."""
    REGISTRY.clear()
    path = Path(folder)
    if not path.is_absolute() and root is not None and not path.exists():
        path = root / path
    if not path.is_dir():
        return REGISTRY
    for f in sorted(path.glob("*.py")):
        if f.name.startswith("_"):
            continue
        name = f"restora_plugin_{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, f)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            REGISTRY.loaded.append(f.name)
        except Exception:  # noqa: BLE001 - a broken plug-in must not stop the run
            REGISTRY.errors.append(f"{f.name}: {traceback.format_exc(limit=2).strip().splitlines()[-1]}")
    return REGISTRY
