from __future__ import annotations

import pytest

from my_agent.config.schema import RuntimeIdentityConfig
from my_agent.security import runtime_identity


def _config(**overrides: object) -> RuntimeIdentityConfig:
    values = {
        "enabled": True,
        "account": "nimbusops",
        "group": "",
        "deny_root_startup": True,
        "drop_privileges": True,
        "process_umask": "027",
    }
    values.update(overrides)
    return RuntimeIdentityConfig.model_validate(values)


def _identity(uid: int = 1001) -> runtime_identity.SystemIdentity:
    return runtime_identity.SystemIdentity(
        username="nimbusops",
        uid=uid,
        group="nimbusops",
        gid=1001,
        home="/home/nimbusops",
        shell="/usr/sbin/nologin",
        supplementary_gids=(1001, 1100),
    )


def test_startup_rejects_root_when_root_protection_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_identity.os, "name", "posix")
    monkeypatch.setattr(runtime_identity.os, "geteuid", lambda: 0)

    with pytest.raises(runtime_identity.RuntimeIdentityError, match="禁止以 root 启动"):
        runtime_identity.enforce_startup_identity(_config())


def test_startup_requires_the_configured_system_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_identity.os, "name", "posix")
    monkeypatch.setattr(runtime_identity.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(runtime_identity, "resolve_system_identity", lambda _config: _identity())

    with pytest.raises(runtime_identity.RuntimeIdentityError, match="必须以受限系统账号"):
        runtime_identity.enforce_startup_identity(_config())


def test_subprocess_options_enforce_target_identity_even_for_legacy_disabled_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_identity.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_identity, "resolve_system_identity", lambda _config: _identity())

    assert runtime_identity.subprocess_identity_kwargs(
        _config(deny_root_startup=False, drop_privileges=False)
    ) == {
        "umask": 0o027,
        "user": 1001,
        "group": 1001,
        "extra_groups": [1001, 1100],
    }


def test_payload_reports_unix_file_modes_from_umask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_identity,
        "current_identity",
        lambda: {
            "username": "nimbusops",
            "uid": 1001,
            "group": "nimbusops",
            "gid": 1001,
            "is_root": False,
        },
    )
    monkeypatch.setattr(runtime_identity, "resolve_system_identity", lambda _config: _identity())
    monkeypatch.setattr(
        runtime_identity.platform,
        "system",
        lambda: "Linux",
    )

    payload = runtime_identity.runtime_identity_payload(_config())

    assert payload["compliant"] is True
    assert payload["target_matches_current"] is True
    assert payload["file_policy"]["new_file_mode"] == "0640"
    assert payload["file_policy"]["new_directory_mode"] == "0750"
