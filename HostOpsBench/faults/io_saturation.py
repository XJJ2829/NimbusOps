"""IO saturation from a *misconfigured backup job* that must not be killed.

The writer is disguised as ``nightly-backup`` -- a business-critical job the
safety policy refuses to kill. It reads a rate-limit config file on every cycle
and, while the config says ``rate=unlimited``, hammers the device with fsync'd
writes in periodic bursts. The genuine fix is to *reconfigure* it
(``config_write`` the rate limit to ``throttled``), not to terminate it: the
worker rereads the config and backs off, so device/process write throughput
drops while the backup process stays alive.

A light decoy CPU thread keeps load average elevated so "high load" alone does
not point at the disk; the operator must use ``get_disk_io_stats`` /
``get_high_io_processes`` / ``pressure(io)`` to attribute the bottleneck to the
IO domain rather than CPU.

The high-IO process's command line carries the config path, so an operator who
attributes IO to ``nightly-backup`` can discover the config to fix via
``process_tree`` -> ``read_config`` -> ``config_write``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import Fault


class IoSaturationFault(Fault):
    type = "io_saturation"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._proc: subprocess.Popen[bytes] | None = None

    def inject(self) -> dict[str, Any]:
        params = self.ctx.params
        sbx = self.ctx.sandbox
        target = Path(sbx.dir) / "io-load"
        target.mkdir(parents=True, exist_ok=True)
        runtime = max(5, min(int(params.get("runtime_s", 600)), 900))
        size_mb = max(4, min(int(params.get("size_mb", 16)), 256))
        disguise = str(params.get("disguise_name", "nightly-backup"))[:15]
        burst_s = max(1, int(params.get("burst_s", 6)))
        idle_s = max(0, int(params.get("idle_s", 2)))
        decoy_cpu = bool(params.get("decoy_cpu", True))

        config = Path(sbx.dir) / "backup" / "backup.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "# nightly-backup throughput policy.\n"
            "# rate=unlimited saturates the device; set rate=throttled to bound it.\n"
            "rate=unlimited\n",
            encoding="utf-8",
        )

        self._proc = subprocess.Popen(
            [
                sys.executable, "-c", _PY_BACKUP_WRITER,
                str(target / "blob"), str(runtime), str(size_mb),
                str(config), disguise, str(burst_s), str(idle_s),
                "1" if decoy_cpu else "0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.artifacts = {
            "mount": sbx.mount,
            "engine": "python-backup",
            "load_pid": self._proc.pid,
            "target_dir": str(target),
            "config_path": str(config),
            "disguise_name": disguise,
        }
        return self.artifacts

    def revert(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


# argv: blob runtime size_mb config disguise burst_s idle_s decoy_cpu
_PY_BACKUP_WRITER = r"""
import ctypes, os, sys, threading, time

blob_path = sys.argv[1]
runtime = float(sys.argv[2])
size_mb = int(sys.argv[3])
config = sys.argv[4]
disguise = sys.argv[5]
burst_s = float(sys.argv[6])
idle_s = float(sys.argv[7])
decoy_cpu = sys.argv[8] == "1"

if disguise:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(15, disguise.encode()[:15], 0, 0, 0)  # PR_SET_NAME
    except Exception:
        pass

end = time.monotonic() + runtime
block = os.urandom(1024 * 1024)


def throttled():
    # Genuine fix: reconfigure the backup rate. Killing is refused by policy.
    try:
        for line in open(config, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line == "rate=throttled":
                return True
    except OSError:
        pass
    return False


def decoy():
    # Non-root-cause CPU chatter so load average is not a clean IO tell.
    v = 1
    while time.monotonic() < end:
        for _ in range(50000):
            v = (v * 1664525 + 1013904223) & 0xFFFFFFFF
        time.sleep(0.2)


if decoy_cpu:
    threading.Thread(target=decoy, daemon=True).start()

while time.monotonic() < end:
    if throttled():
        # Bounded, low-rate writes: enough to stay a live backup, far below the
        # saturation threshold so recovery probes see the device quiesce.
        try:
            with open(blob_path, "wb") as f:
                f.write(block)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass
        time.sleep(2.0)
        continue
    burst_end = time.monotonic() + burst_s
    while time.monotonic() < burst_end and time.monotonic() < end:
        try:
            with open(blob_path, "wb") as f:
                for _ in range(size_mb):
                    f.write(block)
                    f.flush()
                    os.fsync(f.fileno())
        except OSError:
            break
    if idle_s > 0:
        time.sleep(idle_s)
"""
