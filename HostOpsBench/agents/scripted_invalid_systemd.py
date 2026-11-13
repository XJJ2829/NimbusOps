"""Deterministic closed-loop baseline for an invalid systemd unit file."""
from __future__ import annotations

from ..faults.invalid_systemd_config import VALID_UNIT
from ..mcp_client import RecordingMCP
from ..models import (
    AgentResult,
    EvidenceClaim,
    RemediationReport,
    ReportedRootCause,
)
from .base import AgentContext


class ScriptedInvalidSystemdAgent:
    name = "scripted-invalid-systemd"

    def run(self, mcp: RecordingMCP, context: AgentContext) -> AgentResult:
        path = str(context.artifacts["config_path"])
        before = mcp.call("read_config", {"path": path})
        invalid = mcp.call("validate_config_syntax", {"path": path, "kind": "systemd"})
        evidence = [
            EvidenceClaim(
                id="ev-config",
                source_call="read_config",
                claim="the unit file has a misplaced directive, a missing dependency, and a bad ExecStart",
                value=before,
            ),
            EvidenceClaim(
                id="ev-validation",
                source_call="validate_config_syntax",
                claim="the official systemd validator rejects the unit",
                value=invalid,
            ),
        ]

        actions: list[str] = []
        preview = mcp.call(
            "config_write",
            {
                "path": path,
                "content": VALID_UNIT,
                "reason": "repair invalid systemd unit syntax",
            },
        )
        if preview.get("status") == "need_confirm":
            written = mcp.call(
                "config_write",
                {
                    "path": path,
                    "content": VALID_UNIT,
                    "reason": "repair invalid systemd unit syntax",
                    "confirm_token": preview["confirm_token"],
                },
            )
            if written.get("status") == "ok":
                actions.append("config_write")

        after = mcp.call("read_config", {"path": path})
        valid = mcp.call("validate_config_syntax", {"path": path, "kind": "systemd"})
        recovered = (
            actions == ["config_write"]
            and bool(valid.get("ok"))
            and "ExecStart=/bin/true" in after.get("content", "")
        )
        entity = f"config.{path}"

        return AgentResult(
            detected=not bool(invalid.get("ok")) and bool(invalid.get("available")),
            affected_entities=["service.hostops-demo"],
            root_causes=[
                ReportedRootCause(
                    code="config.systemd.unit_syntax_error",
                    entity=entity,
                    confidence=0.98,
                    evidence_ids=[item.id for item in evidence],
                )
            ],
            propagation_chain=[
                [entity, "service.hostops-demo"],
                ["service.hostops-demo", "service.hostops-demo.start_failure"],
            ],
            evidence=evidence,
            remediation=RemediationReport(
                attempted=True,
                actions=actions,
                outcome="recovered" if recovered else "not_recovered",
            ),
        )
