"""Loopback OpenAI-compatible chat server for local O-1 checks."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_REPLY = "こんにちは．わたしはローカルのモックです．"


class MockLlmServer:
    def __init__(self, reply: str = DEFAULT_REPLY, port: int = 0, host: str = "127.0.0.1") -> None:
        self.reply = reply
        self.last_payload: dict | None = None
        handler = _handler_factory(self)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("mock llm host must be loopback")
    server = MockLlmServer(port=port, host=host)
    print(f"mock llm listening on {server.base_url}", flush=True)
    try:
        server._httpd.serve_forever()
    except KeyboardInterrupt:
        server.close()


def _handler_factory(server: MockLlmServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
                model = str(payload.get("model") or "mock")
                server.last_payload = payload
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                self.send_error(400)
                return
            body = json.dumps(
                {
                    "id": "mock",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": server.reply},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return None

    return Handler
