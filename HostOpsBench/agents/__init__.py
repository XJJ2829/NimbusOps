"""Reference agents used to validate the benchmark itself.

They are deterministic *baselines*, not the product Agent. A real LLM/Agent
adapter can implement the same ``run(mcp, context) -> AgentResult`` protocol and
be selected in a scenario without changing the controller or scorer.
"""
from __future__ import annotations

from .base import Agent, AgentContext
from .scripted_cpu_saturation import ScriptedCpuSaturationAgent
from .scripted_fd_leak import ScriptedFdLeakAgent
from .scripted_high_load import ScriptedHighLoadAgent
from .scripted_invalid_systemd import ScriptedInvalidSystemdAgent
from .scripted_log_flood import ScriptedLogFloodAgent
from .scripted_upstream_failure import ScriptedUpstreamFailureAgent

_REGISTRY: dict[str, type[Agent]] = {
    "scripted-high-load": ScriptedHighLoadAgent,
    "scripted-log-flood": ScriptedLogFloodAgent,
    "scripted-cpu-saturation": ScriptedCpuSaturationAgent,
    "scripted-fd-leak": ScriptedFdLeakAgent,
    "scripted-upstream-failure": ScriptedUpstreamFailureAgent,
    "scripted-invalid-systemd": ScriptedInvalidSystemdAgent,
}


def build(name: str) -> Agent:
    try:
        return _REGISTRY[name]()
    except KeyError as exc:
        raise ValueError(f"unknown agent adapter: {name}") from exc


__all__ = ["Agent", "AgentContext", "build"]
