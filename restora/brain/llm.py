"""Talking to a local language model: Ollama, or any OpenAI-compatible server (LM Studio, llama.cpp,
vLLM, Jan, LocalAI ...). Free and offline with a local model; nothing leaves the machine.

Answers are forced into a JSON schema (constrained decoding), so even a small model returns valid JSON.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

import requests


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, provider: str = "ollama", url: str = "http://localhost:11434", model: str = "qwen3:4b-instruct",
                 timeout: float = 120.0, temperature: float = 0.0, api_key: str | None = None):
        self.provider = provider
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.api_key = api_key
        self.calls = 0
        self.seconds = 0.0
        self._cache: dict = {}
        self._think_ok = True
        self._schema_mode = "json_schema"

    # ------------------------------------------------------------------ discovery
    def _openai_base(self) -> str:
        return self.url if re.search(r"/v\d+$", self.url) else self.url + "/v1"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def models(self) -> list:
        if self.provider == "ollama":
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
            return [m.get("name") or m.get("model") for m in r.json().get("models", [])]
        r = requests.get(f"{self._openai_base()}/models", headers=self._headers(), timeout=5)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]

    def status(self) -> tuple:
        """(ready, message, models)."""
        try:
            names = self.models()
        except requests.exceptions.ConnectionError:
            where = "Ollama" if self.provider == "ollama" else "the AI server"
            return False, f"No local AI found at {self.url} ({where} is not running) - using rules only.", []
        except Exception as e:  # noqa: BLE001
            return False, f"The AI server at {self.url} did not answer properly ({e}) - using rules only.", []
        if self.provider == "ollama":
            wanted = self.model if ":" in self.model else self.model + ":latest"
            if wanted not in names and self.model not in names:
                others = f" (installed: {', '.join(names[:4])})" if names else ""
                return False, (f"Ollama is running but the model '{self.model}' is not installed{others}. "
                               f"Run:  ollama pull {self.model}  - using rules only."), names
        elif names and self.model not in names:
            return False, f"The AI server does not offer the model '{self.model}' (it has: {', '.join(names[:5])}) - using rules only.", names
        return True, f"Local AI ready: {self.model} via {'Ollama' if self.provider == 'ollama' else self.url}", names

    # ------------------------------------------------------------------ asking
    def chat_json(self, system: str, user: str, schema: dict, max_tokens: int = 400):
        key = hashlib.sha256(json.dumps([self.model, system, user, schema], sort_keys=True).encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        started = time.perf_counter()
        try:
            text = self._ollama(system, user, schema, max_tokens) if self.provider == "ollama" else \
                self._openai(system, user, schema, max_tokens)
        finally:
            self.calls += 1
            self.seconds += time.perf_counter() - started
        value = parse_json_answer(text)
        self._cache[key] = value
        return value

    def _ollama(self, system, user, schema, max_tokens) -> str:
        payload = {"model": self.model, "stream": False, "format": schema, "keep_alive": "30m",
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                   "options": {"temperature": self.temperature, "num_predict": max_tokens, "seed": 7, "num_ctx": 8192}}
        if self._think_ok:
            payload["think"] = False
        r = requests.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
        if r.status_code == 400 and "think" in r.text.lower() and self._think_ok:
            self._think_ok = False
            payload.pop("think", None)
            r = requests.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
        if r.status_code >= 400:
            raise LLMError(f"Ollama answered {r.status_code}: {r.text[:200]}")
        message = r.json().get("message") or {}
        content = message.get("content") or ""
        if not content.strip() and message.get("thinking"):
            raise LLMError(f"'{self.model}' spent its whole answer thinking. Use a non-thinking model such as "
                           f"qwen3:4b-instruct (set model in restora.toml).")
        return content

    def _openai(self, system, user, schema, max_tokens) -> str:
        base = {"model": self.model, "temperature": self.temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        attempts = []
        if self._schema_mode == "json_schema":
            attempts.append(("json_schema", {**base, "response_format": {"type": "json_schema", "json_schema": {
                "name": "restora", "schema": schema, "strict": True}}}))
        if self._schema_mode in ("json_schema", "json_object"):
            attempts.append(("json_object", {**base, "response_format": {"type": "json_object"}}))
        attempts.append(("plain", base))
        last = None
        for mode, payload in attempts:
            r = requests.post(f"{self._openai_base()}/chat/completions", json=payload, headers=self._headers(),
                              timeout=self.timeout)
            if r.status_code < 400:
                self._schema_mode = mode
                choice = (r.json().get("choices") or [{}])[0]
                return (choice.get("message") or {}).get("content") or ""
            last = r
        raise LLMError(f"The AI server answered {last.status_code}: {last.text[:200]}")


def parse_json_answer(text: str):
    """JSON from a model answer, tolerating <think> blocks and ``` fences."""
    if text is None:
        raise LLMError("empty answer")
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in t:                       # thinking models whose template opens <think> in the prompt
        t = t.rsplit("</think>", 1)[1]
    elif "<think>" in t:                      # cut off while still thinking: there is no answer
        raise LLMError("the model was still thinking when its answer was cut off (use a non-thinking model)")
    t = t.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise LLMError(f"the model did not return JSON: {t[:120]}")
