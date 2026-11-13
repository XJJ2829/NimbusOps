from __future__ import annotations

import json
import plistlib
import struct
from pathlib import Path

import pytest

from my_agent.security.privileged_helper import (
    HelperPolicyError,
    PrivilegedHelperServer,
    _peer_credentials,
    build_argv,
    validate_policy,
)
from my_agent.security.privileged_setup import (
    HELPER_PATH,
    POLICY_PATH,
    SOCKET_PATH,
    _launchd_plist,
    _systemd_unit,
    build_default_policy,
)


def test_linux_default_policy_supports_managed_service_peer() -> None:
    policy = build_default_policy(
        "Linux",
        service_user="nimbusops",
        peer_mode="service",
    )

    assert policy["allowedUsers"] == ["nimbusops"]
    assert policy["peerVerification"] == {
        "mode": "systemd",
        "service": "nimbusops.service",
    }
    assert "service_restart" in policy["actions"]
    assert policy["actions"]["flush_dns_cache"]["executable"] == "/usr/bin/resolvectl"
    assert policy["actions"]["flush_dns_cache"]["arguments"] == ["flush-caches"]


def test_macos_default_policy_supports_manual_development_peer() -> None:
    policy = build_default_policy(
        "Darwin",
        service_user="alice",
        peer_mode="uid",
    )

    assert policy["allowedUsers"] == ["alice"]
    assert policy["peerVerification"] == {"mode": "uid"}
    assert policy["allowUidOnlyDevelopmentMode"] is True
    assert "flush_dns_cache" in policy["actions"]


def test_policy_rejects_undefined_argument_placeholder() -> None:
    policy = build_default_policy(
        "Darwin",
        service_user="alice",
        peer_mode="uid",
    )
    policy["actions"]["flush_dns_cache"]["arguments"].append("{target}")

    with pytest.raises(HelperPolicyError, match="undefined parameters"):
        validate_policy(policy, require_root_owned=False)


def test_build_argv_rejects_values_outside_enum() -> None:
    policy = build_default_policy(
        "Linux",
        service_user="nimbusops",
        peer_mode="service",
    )

    with pytest.raises(ValueError, match="allowlist"):
        build_argv(
            policy,
            "service_restart",
            {"service": "unapproved.service"},
        )


def test_systemd_unit_uses_sealed_helper_and_platform_paths() -> None:
    unit = _systemd_unit(Path("/usr/bin/python3"), "nimbusops")

    assert str(HELPER_PATH) in unit
    assert str(POLICY_PATH) in unit
    assert str(SOCKET_PATH) in unit
    assert "RuntimeDirectoryMode=0750" in unit
    assert "--socket-group\" \"nimbusops" in unit


def test_launchd_plist_uses_sealed_helper_and_platform_paths() -> None:
    payload = plistlib.loads(_launchd_plist(Path("/usr/bin/python3"), "staff"))
    arguments = payload["ProgramArguments"]

    assert payload["Label"] == "com.nimbusops.privileged"
    assert str(HELPER_PATH) in arguments
    assert str(POLICY_PATH) in arguments
    assert str(SOCKET_PATH) in arguments
    assert arguments[-1] == "staff"


def test_policy_templates_are_valid_json() -> None:
    for system in ("Linux", "Darwin"):
        policy = build_default_policy(
            system,
            service_user="nimbusops",
            peer_mode="service",
        )
        assert json.loads(json.dumps(policy))["version"] == 1


def test_darwin_peer_credentials_parse_xucred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_cred = bytearray(76)
    struct.pack_into("=IIh", raw_cred, 0, 0, 501, 1)
    struct.pack_into("=I", raw_cred, 12, 20)

    class FakeSocket:
        def getsockopt(self, level: int, option: int, size: int) -> bytes:
            if option == 1:
                return bytes(raw_cred[:size])
            if option == 2:
                return struct.pack("=i", 4321)
            raise OSError

    monkeypatch.delattr("socket.SO_PEERCRED", raising=False)
    monkeypatch.setattr("socket.LOCAL_PEERCRED", 1, raising=False)

    assert _peer_credentials(FakeSocket()) == (4321, 501, 20)


def test_add_action_requires_system_administrator(tmp_path: Path) -> None:
    policy = build_default_policy(
        "Darwin",
        service_user="alice",
        peer_mode="uid",
    )
    server = PrivilegedHelperServer(
        socket_path=tmp_path / "helper.sock",
        policy_path=tmp_path / "policy.json",
        audit_path=tmp_path / "audit.jsonl",
        socket_group="staff",
        require_root_owned=False,
    )

    with pytest.raises(PermissionError, match="system administrator"):
        server._add_action(
            policy,
            {
                "actor": {
                    "id": "user-1",
                    "username": "alice",
                    "role": "user",
                },
                "action_name": "new_action",
                "action": {
                    "executable": "/usr/bin/true",
                    "arguments": [],
                    "parameters": {},
                },
            },
        )


def test_add_action_validates_and_persists_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = build_default_policy(
        "Darwin",
        service_user="alice",
        peer_mode="uid",
    )
    server = PrivilegedHelperServer(
        socket_path=tmp_path / "helper.sock",
        policy_path=tmp_path / "policy.json",
        audit_path=tmp_path / "audit.jsonl",
        socket_group="staff",
        require_root_owned=False,
    )
    persisted: dict[str, object] = {}

    def fake_write_policy(path: Path, candidate: dict) -> None:
        persisted["path"] = path
        persisted["policy"] = candidate

    monkeypatch.setattr(
        "my_agent.security.privileged_helper._write_policy",
        fake_write_policy,
    )
    monkeypatch.setattr(
        "my_agent.security.privileged_helper.load_policy",
        lambda *_args, **_kwargs: persisted["policy"],
    )

    response = server._add_action(
        policy,
        {
            "actor": {
                "id": "admin-1",
                "username": "admin",
                "role": "system_admin",
            },
            "action_name": "check_helper",
            "action": {
                "label": "检查 helper",
                "executable": "/usr/bin/true",
                "arguments": [],
                "parameters": {},
                "timeout": 10,
            },
        },
    )

    assert persisted["path"] == server.policy_path
    assert response["action"]["name"] == "check_helper"
    assert "check_helper" in response["policy"]["actions"]
