"""Demo service + constant-rate HTTP workload for business-impact metrics."""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import httpx


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Sample:
    at: float
    latency_ms: float
    status: int


class DemoWorkload:
    def __init__(self, root: Path, request_rate: float, write_kb: int) -> None:
        self.root = root
        self.request_rate = request_rate
        self.write_kb = write_kb
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen[bytes] | None = None
        self.samples: deque[Sample] = deque(maxlen=20_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "benchmark.demo_service",
                "--port",
                str(self.port),
                "--work-file",
                str(self.root / "demo-service" / "work.dat"),
                "--write-kb",
                str(self.write_kb),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_healthy()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("demo service did not become healthy")

    def _run(self) -> None:
        interval = 1.0 / max(self.request_rate, 0.1)
        with httpx.Client(timeout=10) as client:
            while not self._stop.is_set():
                started = time.monotonic()
                status = 0
                try:
                    status = client.get(f"{self.base_url}/work").status_code
                except httpx.HTTPError:
                    status = 599
                finished = time.monotonic()
                self.samples.append(
                    Sample(at=finished, latency_ms=(finished - started) * 1000, status=status)
                )
                self._stop.wait(max(0.0, interval - (finished - started)))

    def snapshot(self, since: float) -> dict[str, float | int]:
        rows = [s for s in self.samples if s.at >= since]
        latencies = sorted(s.latency_ms for s in rows)
        errors = sum(1 for s in rows if not (200 <= s.status < 400))
        return {
            "requests": len(rows),
            "errors": errors,
            "error_rate": round(errors / len(rows), 4) if rows else 0.0,
            "p50_ms": round(_percentile(latencies, 0.50), 3),
            "p95_ms": round(_percentile(latencies, 0.95), 3),
            "p99_ms": round(_percentile(latencies, 0.99), 3),
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    index = min(int(round((len(values) - 1) * q)), len(values) - 1)
    return values[index]
