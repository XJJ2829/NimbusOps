"""Deterministic reference baseline for a stateful process descriptor leak.

Demonstrates the correct non-destructive path: distinguish the leak from live
connections with fd-target grouping, then raise the open-files soft limit
(prlimit-style first-aid) instead of killing the connection-holding worker.
"""
from __future__ import annotations

from ..mcp_client import RecordingMCP
from ..models import AgentResult, EvidenceClaim, RemediationReport, ReportedRootCause
from .base import AgentContext


class ScriptedFdLeakAgent:
    name = "scripted-fd-leak"

    def run(self, mcp: RecordingMCP, context: AgentContext) -> AgentResult:
        pid = int(context.artifacts["leak_pid"])
        processes = mcp.read_resource("os://process/list")
        tree = mcp.call("process_tree", {"pid": pid})
        usage = mcp.call("get_process_fd_count", {"pid": pid})
        targets = mcp.call("get_process_fd_targets", {"pid": pid})
        limits = mcp.call("get_process_limits", {"pid": pid})

        fd_count = int(usage.get("fd_count", 0))
        leaked_count = int(targets.get("leaked_count", 0))
        detected = bool(usage.get("available")) and fd_count >= 200 and leaked_count > 100
        entity = f"process.{pid}"

        evidence = [
            EvidenceClaim(
                id="ev-process-list",
                source_call="os://process/list",
                claim="the leaking worker is visible in the process table",
                value=processes,
            ),
            EvidenceClaim(
                id="ev-process-tree",
                source_call="process_tree",
                claim="the leaking process identity is grounded",
                value=tree,
            ),
            EvidenceClaim(
                id="ev-fd-count",
                source_call="get_process_fd_count",
                claim="open file descriptors are abnormally high for the process",
                value=usage,
            ),
            EvidenceClaim(
                id="ev-fd-targets",
                source_call="get_process_fd_targets",
                claim="one regular file is opened many times: a leak, not concurrency",
                value=targets,
            ),
            EvidenceClaim(
                id="ev-limits",
                source_call="get_process_limits",
                claim="the open-files soft limit is the binding constraint",
                value=limits,
            ),
        ]

        actions: list[str] = []
        if detected:
            target_soft = 65536
            preview = mcp.call(
                "raise_fd_limit",
                {"pid": pid, "soft": target_soft, "reason": "relieve fd exhaustion on live service"},
            )
            if preview.get("status") == "need_confirm":
                applied = mcp.call(
                    "raise_fd_limit",
                    {
                        "pid": pid,
                        "soft": target_soft,
                        "reason": "relieve fd exhaustion on live service",
                        "confirm_token": preview["confirm_token"],
                    },
                )
                if applied.get("status") == "ok":
                    actions.append("raise_fd_limit")

        after = mcp.call("get_process_fd_count", {"pid": pid})
        recovered = (
            actions == ["raise_fd_limit"]
            and bool(after.get("available"))
            and int(after.get("soft_limit", 0)) > 1024
        )

        return AgentResult(
            detected=detected,
            affected_entities=[entity, "service.demo-worker"],
            root_causes=[
                ReportedRootCause(
                    code="resource.process.file_descriptor_leak",
                    entity=entity,
                    confidence=0.95 if detected else 0.2,
                    evidence_ids=[item.id for item in evidence],
                )
            ] if detected else [],
            propagation_chain=[
                [entity, "host.fd_capacity"],
                ["host.fd_capacity", "service.demo-worker.accept_failures"],
            ] if detected else [],
            evidence=evidence,
            remediation=RemediationReport(
                attempted=bool(actions),
                actions=actions,
                outcome="recovered" if recovered else "not_recovered",
            ),
        )
