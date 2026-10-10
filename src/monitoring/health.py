"""Minimal HTTP health endpoint for k8s probes.

/health  — 메인 루프가 max_age 초 안에 돌았으면 200 (liveness)
/ready   — 재시작 복구(손절 점검 포함)가 끝났으면 200 (readiness)
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthState:
    def __init__(self, max_age: float = 90.0):
        self.max_age = max_age
        self.last_tick = time.monotonic()
        self.ready = False
        self.info: dict = {}

    def beat(self, **info) -> None:
        self.last_tick = time.monotonic()
        self.info.update(info)

    def alive(self) -> bool:
        return time.monotonic() - self.last_tick < self.max_age


def serve(state: HealthState, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/health"):
                ok = state.alive()
            elif self.path.startswith("/ready"):
                ok = state.ready and state.alive()
            else:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"ok": ok, **state.info}, default=str).encode()
            self.send_response(200 if ok else 503)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
