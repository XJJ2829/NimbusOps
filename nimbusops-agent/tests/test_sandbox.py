import pytest

from my_agent.agent.tools import sandbox


def test_auto_selects_macos_seatbelt(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")

    assert sandbox.resolve_backend("auto") == "sandbox-exec"


def test_auto_selects_linux_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")

    assert sandbox.resolve_backend("auto") == "bwrap"


def test_seatbelt_profile_restricts_user_files_and_allows_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_media_path", lambda: tmp_path / "media")
    profile = sandbox._seatbelt_profile(str(tmp_path))

    assert "(deny file-write*)" in profile
    assert "(deny file-read*" in profile
    assert str(tmp_path.resolve()) in profile
    assert "(allow file-write*" in profile


def test_sandbox_exec_wrapper_quotes_command(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_media_path", lambda: tmp_path / "media")
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None,
    )
    command = "printf '%s' \"hello; touch /tmp/not-a-second-command\""

    wrapped = sandbox.wrap_command(
        "seatbelt",
        command,
        str(tmp_path),
        str(tmp_path),
    )

    assert wrapped.startswith("/usr/bin/sandbox-exec -p ")
    assert "/bin/sh -c" in wrapped
    assert "touch /tmp/not-a-second-command" in wrapped


def test_unknown_backend_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown sandbox backend"):
        sandbox.wrap_command("unknown", "true", str(tmp_path), str(tmp_path))
