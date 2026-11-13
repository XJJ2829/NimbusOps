"""Deterministic reference baseline for the high-load diagnosis scenario."""
from __future__ import annotations

from typing import Any

from ..mcp_client import RecordingMCP
from ..models import (
    AgentResult,
    EvidenceClaim,
    RemediationReport,
    ReportedRootCause,
)
from .base import AgentContext


class ScriptedHighLoadAgent:
    name = "scripted-high-load"

    def run(self, mcp: RecordingMCP, context: AgentContext) -> AgentResult:
        load = mcp.call("cpu_load")
        cpu_psi = mcp.call("pressure", {"resource": "cpu"})
        io_psi = mcp.call("pressure", {"resource": "io"})
        disk_io = mcp.call("get_disk_io_stats")
        high_io = mcp.call("get_high_io_processes", {"top_n": 10})
        dstate = mcp.call("get_uninterruptible_processes")

        evidence = [
            EvidenceClaim(
                id="ev-load",
                source_call="cpu_load",
                claim="host load sampled",
                value=load.get("load_1"),
            ),
        ]
        if cpu_psi.get("available"):
            evidence.append(
                EvidenceClaim(
                    id="ev-cpu-psi",
                    source_call="pressure",
                    claim="CPU PSI compared with IO PSI",
                    value=cpu_psi.get("avg10"),
                )
            )
        if io_psi.get("available"):
            evidence.append(
                EvidenceClaim(
                    id="ev-io-psi",
                    source_call="pressure",
                    claim="IO pressure sampled",
                    value=io_psi.get("avg10"),
                )
            )

        devices = disk_io.get("devices", []) if isinstance(disk_io, dict) else []
        abnormal = [d for d in devices if d.get("abnormal")]
        candidate: dict[str, Any] = max(
            abnormal or devices,
            key=lambda d: (d.get("util_pct", 0), d.get("await_ms", 0), d.get("write_mbps", 0)),
            default={},
        )
        if candidate:
            evidence.append(
                EvidenceClaim(
                    id="ev-disk-io",
                    source_call="get_disk_io_stats",
                    claim="device shows write load / latency / utilization",
                    value=candidate,
                )
            )

        procs = high_io.get("processes", []) if isinstance(high_io, dict) else []
        if procs:
            evidence.append(
                EvidenceClaim(
                    id="ev-process-io",
                    source_call="get_high_io_processes",
                    claim="high-IO process correlated to device load",
                    value=procs[0],
                )
            )

        d_procs = dstate.get("processes", []) if isinstance(dstate, dict) else []
        if d_procs:
            evidence.append(
                EvidenceClaim(
                    id="ev-dstate",
                    source_call="get_uninterruptible_processes",
                    claim="uninterruptible tasks confirm IO wait",
                    value=len(d_procs),
                )
            )

        io_signal = bool(abnormal or procs)
        if io_psi.get("available"):
            io_signal = io_signal or io_psi.get("avg10", 0) > 0
        entity = f"device.{candidate.get('device', 'unknown')}"

        # Remediate by reconfiguring the backup rate limit -- never by killing
        # the business-critical backup job.
        config_path = str(context.artifacts.get("config_path", ""))
        actions: list[str] = []
        recovered = False
        if io_signal and config_path:
            before_cfg = mcp.call("read_config", {"path": config_path})
            evidence.append(
                EvidenceClaim(
                    id="ev-backup-config",
                    source_call="read_config",
                    claim="the backup rate limit is set to unlimited",
                    value=before_cfg,
                )
            )
            new_content = (before_cfg.get("content", "") or "").replace(
                "rate=unlimited", "rate=throttled"
            )
            preview = mcp.call(
                "config_write",
                {"path": config_path, "content": new_content, "reason": "throttle backup IO rate"},
            )
            if preview.get("status") == "need_confirm":
                written = mcp.call(
                    "config_write",
                    {
                        "path": config_path,
                        "content": new_content,
                        "reason": "throttle backup IO rate",
                        "confirm_token": preview["confirm_token"],
                    },
                )
                if written.get("status") == "ok":
                    actions.append("config_write")
            after_io = mcp.call("get_high_io_processes", {"top_n": 10})
            after_procs = after_io.get("processes", []) if isinstance(after_io, dict) else []
            top_write = float(after_procs[0].get("write_mbps", 0)) if after_procs else 0.0
            recovered = actions == ["config_write"] and top_write < 5

        root_causes = []
        if io_signal:
            root_causes.append(
                ReportedRootCause(
                    code="resource.disk.io_saturation",
                    entity=entity,
                    confidence=0.85 if len(evidence) >= 4 else 0.65,
                    evidence_ids=[e.id for e in evidence if e.id != "ev-cpu-psi"],
                )
            )

        return AgentResult(
            detected=io_signal,
            affected_entities=["host.target"],
            root_causes=root_causes,
            propagation_chain=[
                [entity, "host.io_pressure"],
                ["host.io_pressure", "service.demo-api.latency"],
            ] if io_signal else [],
            evidence=evidence,
            remediation=RemediationReport(
                attempted=bool(actions),
                actions=actions,
                outcome="recovered" if recovered else "not_recovered",
            ),
            limitations=[] if io_psi.get("available") else ["kernel PSI unavailable"],
        )
