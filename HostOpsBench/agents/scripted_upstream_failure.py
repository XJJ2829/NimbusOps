"""Deterministic reference baseline for a two-hop HTTP upstream failure.

Shows the non-obvious fix: the gateway config is a red herring; the root cause
is the backend health config, and correcting it is not enough until the backend
is *reloaded* (SIGHUP) so it re-reads. Neither hop is killed.
"""
from __future__ import annotations

from ..mcp_client import RecordingMCP
from ..models import AgentResult, EvidenceClaim, RemediationReport, ReportedRootCause
from .base import AgentContext

_SERVING = "# backend health switch: state=serving | maintenance\nstate=serving\n"


class ScriptedUpstreamFailureAgent:
    name = "scripted-upstream-failure"

    def run(self, mcp: RecordingMCP, context: AgentContext) -> AgentResult:
        a = context.artifacts
        gateway_url = str(a["gateway_url"])
        backend_pid = int(a["backend_pid"])
        backend_config = str(a["backend_config_path"])
        gateway_config = str(a["gateway_config_path"])

        dns = mcp.call("dns_lookup", {"name": "localhost"})
        listeners = mcp.call("list_listening_ports")
        health = mcp.call("http_health_check", {"url": gateway_url, "timeout": 3})
        gw_cfg = mcp.call("read_config", {"path": gateway_config})
        be_cfg = mcp.call("read_config", {"path": backend_config})

        detected = int(health.get("status", 0)) == 503 and "state=maintenance" in be_cfg.get("content", "")
        entity = f"config.{backend_config}"
        evidence = [
            EvidenceClaim(id="ev-dns", source_call="dns_lookup",
                          claim="local DNS resolves", value=dns),
            EvidenceClaim(id="ev-listener", source_call="list_listening_ports",
                          claim="both hops expose listening TCP sockets", value=listeners),
            EvidenceClaim(id="ev-http", source_call="http_health_check",
                          claim="the end-to-end call returns HTTP 503", value=health),
            EvidenceClaim(id="ev-gateway-config", source_call="read_config",
                          claim="the gateway points at the correct backend (red herring)",
                          value=gw_cfg.get("content", "")),
            EvidenceClaim(id="ev-backend-config", source_call="read_config",
                          claim="the backend health config is in maintenance mode",
                          value=be_cfg.get("content", "")),
        ]

        actions: list[str] = []
        if detected:
            preview = mcp.call(
                "config_write",
                {"path": backend_config, "content": _SERVING, "reason": "take backend out of maintenance"},
            )
            if preview.get("status") == "need_confirm":
                written = mcp.call(
                    "config_write",
                    {"path": backend_config, "content": _SERVING,
                     "reason": "take backend out of maintenance",
                     "confirm_token": preview["confirm_token"]},
                )
                if written.get("status") == "ok":
                    actions.append("config_write")
            reload_preview = mcp.call(
                "reload_service",
                {"pid": backend_pid, "reason": "reload backend to pick up health config"},
            )
            if reload_preview.get("status") == "need_confirm":
                reloaded = mcp.call(
                    "reload_service",
                    {"pid": backend_pid, "reason": "reload backend to pick up health config",
                     "confirm_token": reload_preview["confirm_token"]},
                )
                if reloaded.get("status") == "ok":
                    actions.append("reload_service")

        after = mcp.call("http_health_check", {"url": gateway_url, "timeout": 3})
        recovered = actions == ["config_write", "reload_service"] and int(after.get("status", 0)) == 200

        return AgentResult(
            detected=detected,
            affected_entities=["service.demo-api", "endpoint.gateway", "endpoint.backend"],
            root_causes=[
                ReportedRootCause(
                    code="dependency.http.backend_maintenance_mode",
                    entity=entity,
                    confidence=0.95 if detected else 0.2,
                    evidence_ids=[item.id for item in evidence],
                )
            ] if detected else [],
            propagation_chain=[
                [entity, "endpoint.backend"],
                ["endpoint.backend", "endpoint.gateway"],
                ["endpoint.gateway", "service.demo-api.errors"],
            ] if detected else [],
            evidence=evidence,
            remediation=RemediationReport(
                attempted=bool(actions),
                actions=actions,
                outcome="recovered" if recovered else "not_recovered",
            ),
        )
