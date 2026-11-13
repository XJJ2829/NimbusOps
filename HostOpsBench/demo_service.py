"""A tiny systemd-friendly demo API for the HostOpsBench MVP.

``GET /work`` performs a bounded write + fsync on the scenario filesystem, so
device saturation creates a real user-visible latency signal. ``/health`` and
``/metrics`` expose source-of-truth probes without requiring a heavyweight demo
microservice stack in the first iteration.
"""
from __future__ import annotations

import argparse
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_LOCK = threading.Lock()
_REQUESTS = 0
_ERRORS = 0
_LATENCY_SUM = 0.0


class Handler(BaseHTTPRequestHandler):
    work_file: Path
    write_kb: int

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        global _ERRORS, _LATENCY_SUM, _REQUESTS
        if self.path == "/health":
            self._reply(HTTPStatus.OK, b'{"status":"ok"}\n', "application/json")
            return
        if self.path == "/metrics":
            with _LOCK:
                body = (
                    f"demo_http_requests_total {_REQUESTS}\n"
                    f"demo_http_errors_total {_ERRORS}\n"
                    f"demo_http_request_duration_seconds_sum {_LATENCY_SUM:.6f}\n"
                ).encode()
            self._reply(HTTPStatus.OK, body, "text/plain; version=0.0.4")
            return
        if self.path != "/work":
            self._reply(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")
            return

        started = time.monotonic()
        status = HTTPStatus.OK
        body = b'{"ok":true}\n'
        try:
            payload = os.urandom(self.write_kb * 1024)
            with open(self.work_file, "ab", buffering=0) as fh:
                fh.write(payload)
                os.fsync(fh.fileno())
        except OSError as exc:
            status = HTTPStatus.INSUFFICIENT_STORAGE
            body = f'{{"ok":false,"error":"{type(exc).__name__}"}}\n'.encode()
        elapsed = time.monotonic() - started
        with _LOCK:
            _REQUESTS += 1
            _LATENCY_SUM += elapsed
            if status != HTTPStatus.OK:
                _ERRORS += 1
        self._reply(status, body, "application/json")

    def _reply(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--work-file", type=Path, required=True)
    parser.add_argument("--write-kb", type=int, default=128)
    args = parser.parse_args()
    args.work_file.parent.mkdir(parents=True, exist_ok=True)
    Handler.work_file = args.work_file
    Handler.write_kb = args.write_kb
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
