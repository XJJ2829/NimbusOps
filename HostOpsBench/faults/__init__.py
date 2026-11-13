"""Reversible, bounded, user-space fault injectors.

Every injector confines its effect to the scenario :class:`~benchmark.sandbox.Sandbox`
and exposes ``inject()`` / ``revert()``. Injectors are created by the controller
and are *never* handed to the agent under test.
"""
from __future__ import annotations

from .base import Fault, FaultContext
from .cpu_saturation import CpuSaturationFault
from .fd_leak import FdLeakFault
from .invalid_systemd_config import InvalidSystemdConfigFault
from .io_saturation import IoSaturationFault
from .log_flood import LogFloodFault
from .upstream_http_failure import UpstreamHttpFailureFault

_REGISTRY: dict[str, type[Fault]] = {
    "log_flood": LogFloodFault,
    "io_saturation": IoSaturationFault,
    "cpu_saturation": CpuSaturationFault,
    "fd_leak": FdLeakFault,
    "upstream_http_failure": UpstreamHttpFailureFault,
    "invalid_systemd_config": InvalidSystemdConfigFault,
}


def build(fault_type: str, ctx: FaultContext) -> Fault:
    try:
        cls = _REGISTRY[fault_type]
    except KeyError as exc:
        raise ValueError(f"unknown fault type: {fault_type}") from exc
    return cls(ctx)


__all__ = [
    "CpuSaturationFault",
    "Fault",
    "FaultContext",
    "FdLeakFault",
    "InvalidSystemdConfigFault",
    "IoSaturationFault",
    "LogFloodFault",
    "UpstreamHttpFailureFault",
    "build",
]
