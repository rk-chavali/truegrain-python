"""A stub engine, so the client can be tested without a warehouse.

The client's job is transport, parsing and error mapping. Testing that against a
real engine would test the engine instead, and would make these tests need a
database. The stub records what it was sent, which is how the request-shaping
tests assert the client does not send fields the server would reject.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

import pytest

from truegrain import Client

# A canned response per "METHOD /path", or a callable taking the parsed body.
Route = Any


class StubEngine:
    """An HTTP server returning canned responses and recording requests."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def on(self, method: str, path: str, response: Route, status: int = 200) -> "StubEngine":
        self.routes[f"{method} {path}"] = (status, response)
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "StubEngine":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence the test output
                pass

            def _respond(self, method: str) -> None:
                path = self.path.split("?", 1)[0]
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                body: Any = None
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))

                stub.requests.append({
                    "method": method,
                    "path": path,
                    "query": query,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                    "user_agent": self.headers.get("User-Agent"),
                })

                route = stub.routes.get(f"{method} {path}")
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"code":"not_found","reason":"no stub route","retry":"never"}')
                    return

                status, payload = route
                if isinstance(payload, Callable):  # type: ignore[arg-type]
                    payload = payload(body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._respond("GET")

            def do_POST(self) -> None:
                self._respond("POST")

            def do_DELETE(self) -> None:
                self._respond("DELETE")

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def last_query(self) -> dict[str, str]:
        """The query string of the most recent request, parsed."""
        from urllib.parse import parse_qs

        return {k: v[0] for k, v in parse_qs(self.last_request()["query"]).items()}

    def last_request(self) -> dict[str, Any]:
        assert self.requests, "no request was made"
        return self.requests[-1]


@pytest.fixture
def stub() -> Any:
    engine = StubEngine().start()
    yield engine
    engine.stop()


@pytest.fixture
def client(stub: StubEngine) -> Client:
    return Client(stub.url, token="test-token", timeout=10)
