from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from my_agent.webui import audit_center


class FakeSessionManager:
    def read_session_file(self, _session_key: str) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": "检查服务，token=should-not-leak",
                    "timestamp": "2026-07-08T10:00:00+08:00",
                }
            ]
        }


def test_audit_trace_collects_timeline_and_redacts_secrets(monkeypatch: Any) -> None:
    lines = [
        {
            "turn_id": "trace-1",
            "turn_seq": 1,
            "event": "user",
            "text": "检查服务，token=should-not-leak",
        },
        {
            "turn_id": "trace-1",
            "turn_seq": 2,
            "event": "reasoning_delta",
            "text": "先确认服务状态。",
        },
        {
            "turn_id": "trace-1",
            "turn_seq": 3,
            "event": "tool",
            "tool_events": [
                {
                    "phase": "start",
                    "call_id": "call-1",
                    "name": "service_status",
                    "arguments": {"authorization": "Bearer private"},
                }
            ],
        },
        {
            "turn_id": "trace-1",
            "turn_seq": 4,
            "event": "tool",
            "tool_events": [
                {
                    "phase": "end",
                    "call_id": "call-1",
                    "name": "service_status",
                    "result": {"status": "healthy"},
                }
            ],
        },
        {
            "turn_id": "trace-1",
            "turn_seq": 5,
            "event": "prompt_guardrails_scan",
            "scans": [{"name": "injection", "allowed": True, "reason": "clean"}],
        },
        {
            "turn_id": "trace-1",
            "turn_seq": 6,
            "event": "delta",
            "text": "服务运行正常。",
        },
        {
            "turn_id": "trace-1",
            "turn_seq": 7,
            "event": "turn_end",
            "latency_ms": 1350,
        },
    ]
    monkeypatch.setattr(audit_center, "read_transcript_lines", lambda _key: lines)
    privileged = [
        {
            "id": "audit-1",
            "session_key": "websocket:demo",
            "requested_at": datetime(
                2026,
                7,
                8,
                2,
                0,
                1,
                tzinfo=timezone.utc,
            ).timestamp(),
            "action_name": "restart_service",
            "outcome": "approved",
            "parameters": {"password": "private"},
        }
    ]
    manager = FakeSessionManager()
    row = {
        "key": "websocket:demo",
        "title": "服务巡检",
        "actor_username": "operator01",
    }

    payload = audit_center.list_audit_traces(manager, [row], privileged)
    assert payload["summary"] == {
        "total": 1,
        "completed": 1,
        "high_risk": 1,
        "security_events": 2,
    }
    assert payload["traces"][0]["user_input"] == "检查服务，token=[REDACTED]"
    assert payload["traces"][0]["actor_username"] == "operator01"
    assert payload["traces"][0]["anomaly_count"] == 0

    detail = audit_center.get_audit_trace(manager, row, "trace-1", privileged)
    assert detail is not None
    assert detail["actor_username"] == "operator01"
    assert detail["integrity"] == "complete"
    assert [event["stage"] for event in detail["timeline"]] == [
        "received",
        "reasoning",
        "tool_call",
        "security_check",
        "result",
        "approval",
        "completed",
    ]
    tool_event = next(event for event in detail["timeline"] if event["stage"] == "tool_call")
    assert tool_event["details"]["arguments"]["authorization"] == "[REDACTED]"
    approval_event = next(event for event in detail["timeline"] if event["stage"] == "approval")
    assert approval_event["details"]["parameters"]["password"] == "[REDACTED]"


def test_audit_trace_locates_empty_config_as_first_anomaly(monkeypatch: Any) -> None:
    lines = [
        {
            "turn_id": "trace-missing-service",
            "turn_seq": 1,
            "event": "user",
            "text": "诊断不存在的服务",
        },
        {
            "turn_id": "trace-missing-service",
            "turn_seq": 2,
            "event": "tool",
            "tool_events": [
                {
                    "phase": "start",
                    "call_id": "read-unit",
                    "name": "read_config",
                    "arguments": {"path": "/usr/lib/systemd/system/missing.service"},
                }
            ],
        },
        {
            "turn_id": "trace-missing-service",
            "turn_seq": 3,
            "event": "tool",
            "tool_events": [
                {
                    "phase": "end",
                    "call_id": "read-unit",
                    "name": "read_config",
                    "result": {"size_bytes": 0, "content": "", "truncated": False},
                }
            ],
        },
        {
            "turn_id": "trace-missing-service",
            "turn_seq": 4,
            "event": "turn_end",
            "latency_ms": 420,
        },
    ]
    monkeypatch.setattr(audit_center, "read_transcript_lines", lambda _key: lines)
    manager = FakeSessionManager()
    row = {"key": "websocket:demo", "title": "服务诊断"}

    payload = audit_center.list_audit_traces(manager, [row], [])
    assert payload["traces"][0]["anomaly_count"] == 1
    assert payload["traces"][0]["risk_level"] == "medium"

    detail = audit_center.get_audit_trace(manager, row, "trace-missing-service", [])
    assert detail is not None
    assert detail["anomalies"][0]["title"] == "未读取到目标配置"
    assert detail["anomalies"][0]["event_id"] == (
        "trace-missing-service:tool:read-unit"
    )
    tool_event = next(event for event in detail["timeline"] if event["stage"] == "tool_call")
    assert tool_event["status"] == "warning"
