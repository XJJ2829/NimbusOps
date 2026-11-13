"""Invalid systemd unit with *several concurrent* errors in the sandbox.

A single malformed ``ExecStart`` is too easy: this unit fails the official
validator for three independent reasons, so fixing only one still leaves it
broken (the agent must diagnose and correct all of them):

1. an unknown/misplaced directive (``StartLimitIntervalSec`` in ``[Service]``
   instead of ``[Unit]``);
2. an ordering/dependency on a unit that does not exist
   (``Requires=hostops-demo-db.service``);
3. an ``ExecStart`` pointing at a binary that is not executable / absent.

The genuine fix is to read the unit, validate it (which enumerates every
error), rewrite it so all three are resolved, and re-validate until the official
validator accepts it -- through the dry-run + confirmation write pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Fault

VALID_UNIT = (
    "[Unit]\n"
    "Description=HostOpsBench demo worker\n"
    "StartLimitIntervalSec=10\n"
    "StartLimitBurst=3\n"
    "\n"
    "[Service]\n"
    "Type=simple\n"
    "ExecStart=/bin/true\n"
    "Restart=always\n"
)

_INVALID_UNIT = (
    "[Unit]\n"
    "Description=HostOpsBench broken demo worker\n"
    "Requires=hostops-demo-db.service\n"
    "After=hostops-demo-db.service\n"
    "\n"
    "[Service]\n"
    "Type=simple\n"
    "ExecStart=/opt/hostops/bin/demo-worker --serve\n"
    "Restart=always\n"
    "StartLimitIntervalSec=10\n"
    "StartLimitBurst=3\n"
)


class InvalidSystemdConfigFault(Fault):
    type = "invalid_systemd_config"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._path: Path | None = None

    def inject(self) -> dict[str, Any]:
        root = Path(self.ctx.sandbox.dir) / "systemd"
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / "hostops-demo.service"
        self._path.write_text(_INVALID_UNIT, encoding="utf-8")
        self.artifacts = {
            "config_path": str(self._path),
            "service_unit": "hostops-demo.service",
        }
        return self.artifacts

    def revert(self) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)
        self._path = None
