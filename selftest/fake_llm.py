"""A stand-in for Ollama / an OpenAI-compatible server, so the AI paths can be tested without a model."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLM:
    def __init__(self, answer, models=("qwen3:4b-instruct",), port: int = 0):
        """answer(system, user, schema) -> dict  (what the 'model' replies)."""
        self.answer = answer
        self.models = list(models)
        self.requests: list = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._send(200, {"models": [{"name": m} for m in fake.models]})
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": m} for m in fake.models]})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append((self.path, body))
                msgs = body.get("messages", [])
                system = next((m["content"] for m in msgs if m["role"] == "system"), "")
                user = next((m["content"] for m in msgs if m["role"] == "user"), "")
                schema = body.get("format") or ((body.get("response_format") or {}).get("json_schema") or {}).get("schema")
                reply = json.dumps(fake.answer(system, user, schema))
                if self.path == "/api/chat":
                    return self._send(200, {"message": {"role": "assistant", "content": reply}, "done": True})
                if self.path == "/v1/chat/completions":
                    return self._send(200, {"choices": [{"message": {"role": "assistant", "content": reply}}]})
                self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
