"""Deterministic reference baseline for CPU saturation diagnosis."""
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


class ScriptedCpuSaturationAgent:
    name = "scripted-cpu-saturation"

    def run(self, mcp: RecordingMCP, context: AgentContext) -> AgentResult:
        pid = int(context.artifacts["load_pid"])
        load = mcp.call("cpu_load")
        cpu_psi = mcp.call("pressure", {"resource": "cpu"})
        processes = mcp.read_resource("os://process/list")
        tree = mcp.call("process_tree", {"pid": pid})
        threads = mcp.call("get_process_threads", {"pid": pid})
        proc_cpu = mcp.call("get_process_cpu", {"pid": pid})

        top_thread: dict[str, Any] = max(
            threads.get("threads", []),
            key=lambda item: item.get("cpu_percent", 0),
            default={},
        )
        cpu_percent = float(top_thread.get("cpu_percent", 0))
        process_visible = any(
            item.get("pid") == pid for item in processes.get("processes", [])
        )
        detected = cpu_percent > 10 and process_visible
        entity = f"process.{pid}"

        evidence = [
            EvidenceClaim(
                id="ev-load",
                source_call="cpu_load",
                claim="host load and CPU topology sampled",
                value=load,
            ),
            EvidenceClaim(
                id="ev-process-list",
                source_call="os://process/list",
                claim="the CPU worker is present in the host process table",
                value=processes,
            ),
            EvidenceClaim(
                id="ev-process-tree",
                source_call="process_tree",
                claim="the runaway process identity and command line are grounded",
                value=tree,
            ),
            EvidenceClaim(
                id="ev-thread-cpu",
                source_call="get_process_threads",
                claim="a single thread in the process is the CPU hotspot",
                value=threads,
            ),
            EvidenceClaim(
                id="ev-proc-cpu",
                source_call="get_process_cpu",
                claim="the workload's instantaneous whole-process CPU is elevated",
                value=proc_cpu,
            ),
        ]
        if cpu_psi.get("available"):
            evidence.append(
                EvidenceClaim(
                    id="ev-cpu-psi",
                    source_call="pressure",
                    claim="CPU pressure was sampled",
                    value=cpu_psi,
                )
            )

        actions: list[str] = []
        if detected:
            target_nice = 19
            preview = mcp.call(
                "set_process_priority",
                {"pid": pid, "nice": target_nice, "reason": "deprioritise runaway CPU workload"},
            )
            if preview.get("status") == "need_confirm":
                applied = mcp.call(
                    "set_process_priority",
                    {
                        "pid": pid,
                        "nice": target_nice,
                        "reason": "deprioritise runaway CPU workload",
                        "confirm_token": preview["confirm_token"],
                    },
                )
                if applied.get("status") == "ok":
                    actions.append("set_process_priority")

        after_cpu = mcp.call("get_process_cpu", {"pid": pid})
        recovered = (
            actions == ["set_process_priority"]
            and bool(after_cpu.get("available"))
            and float(after_cpu.get("cpu_percent", 100)) < 30
        )

        return AgentResult(
            detected=detected,
            affected_entities=["host.target", "service.demo-api"],
            root_causes=[
                ReportedRootCause(
                    code="resource.cpu.runaway_process",
                    entity=entity,
                    confidence=0.95 if detected else 0.2,
                    evidence_ids=[item.id for item in evidence],
                )
            ] if detected else [],
            propagation_chain=[
                [entity, "host.cpu_pressure"],
                ["host.cpu_pressure", "service.demo-api.latency"],
            ] if detected else [],
            evidence=evidence,
            remediation=RemediationReport(
                attempted=bool(actions),
                actions=actions,
                outcome="recovered" if recovered else "not_recovered",
            ),
            limitations=[] if cpu_psi.get("available") else ["kernel PSI unavailable"],
        )
