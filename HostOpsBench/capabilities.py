"""Preflight capability detection.

The MVP is designed to run in two very different places: a plain unprivileged
container (fast CI, reduced fidelity) and a real VM (root, loop mounts, PSI,
fio). Rather than assume, the controller probes what is actually available and
degrades gracefully, recording the degradation so results are never silently
over-claimed.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def _has_sudo() -> bool:
    try:
        return subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _psi_available() -> bool:
    return Path("/proc/pressure/io").exists()


@dataclass
class Capabilities:
    sudo: bool = False
    psi: bool = False
    fio: bool = False
    iostat: bool = False
    pidstat: bool = False
    ps: bool = False
    ss: bool = False
    stress_ng: bool = False
    systemd_analyze: bool = False
    loop_mount: bool = False
    diskstats: bool = False
    procfs: bool = False
    degradations: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        if msg not in self.degradations:
            self.degradations.append(msg)


def detect() -> Capabilities:
    caps = Capabilities()
    caps.sudo = _has_sudo()
    caps.psi = _psi_available()
    caps.fio = shutil.which("fio") is not None
    caps.iostat = shutil.which("iostat") is not None
    caps.pidstat = shutil.which("pidstat") is not None
    caps.ps = shutil.which("ps") is not None
    caps.ss = shutil.which("ss") is not None
    caps.stress_ng = shutil.which("stress-ng") is not None
    caps.systemd_analyze = shutil.which("systemd-analyze") is not None
    caps.diskstats = Path("/proc/diskstats").exists()
    caps.procfs = Path("/proc/self/fd").exists()
    caps.loop_mount = caps.sudo and shutil.which("mkfs.ext4") is not None

    if not caps.psi:
        caps.note("PSI (/proc/pressure) unavailable: IO/CPU pressure signals degraded")
    if not caps.fio and not caps.stress_ng:
        caps.note("neither fio nor stress-ng present: IO load uses a python fallback")
    if not caps.iostat or not caps.pidstat:
        caps.note("sysstat tools missing: device/process IO evidence is degraded")
    if not caps.ps or not caps.procfs:
        caps.note("ps or procfs missing: deep process diagnostics are degraded")
    if not caps.ss:
        caps.note("ss missing: TCP listener and connection evidence is degraded")
    if not caps.loop_mount:
        caps.note(
            "no root+mkfs.ext4: disk-pressure scenarios use a bounded directory, "
            "not a dedicated real filesystem"
        )
    return caps
