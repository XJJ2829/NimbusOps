from __future__ import annotations

from pathlib import Path

from my_agent.config.schema import PrivilegedExecutionConfig
from my_agent.security.privileged import PrivilegedActionRuntime, PrivilegedAuditStore


def test_audit_payload_collapses_one_approval_into_one_record(tmp_path: Path) -> None:
    audit = PrivilegedAuditStore(tmp_path / "privileged.jsonl")
    runtime = PrivilegedActionRuntime(
        PrivilegedExecutionConfig(audit_limit=20),
        audit_store=audit,
    )
    actor = {
        "id": "admin-1",
        "username": "admin",
        "display_name": "Administrator",
        "role": "system_admin",
    }
    common = {
        "approval_id": "approval-1",
        "action_name": "flush_dns_cache",
        "parameters": {},
        "actor": actor,
        "session_key": "websocket:chat-1",
    }
    audit.append({
        **common,
        "event": "requested",
        "outcome": "pending",
        "reason": "刷新 DNS 缓存",
    })
    audit.append({
        **common,
        "event": "approval",
        "outcome": "approved",
    })
    audit.append({
        **common,
        "event": "execution",
        "outcome": "succeeded",
        "exit_code": 0,
    })

    records = runtime.audit_payload()["records"]

    assert len(records) == 1
    assert records[0]["id"] == "approval-1"
    assert records[0]["outcome"] == "succeeded"
    assert records[0]["actor"]["username"] == "admin"
    assert records[0]["parameters"] == {}
    assert [stage["event"] for stage in records[0]["stages"]] == [
        "requested",
        "approval",
        "execution",
    ]
