"""Cross-platform installer and service controller for the root helper."""

from __future__ import annotations

import argparse
import getpass
import grp
import json
import os
import platform
import plistlib
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

from my_agent.security.privileged_helper import load_policy, validate_policy

POLICY_PATH = Path("/etc/nimbusops/privileged-actions.json")
SOCKET_PATH = Path("/var/run/nimbusops/privileged.sock")
AUDIT_PATH = Path("/var/log/nimbusops/privileged-actions.jsonl")
HELPER_DIR = Path("/usr/local/libexec/nimbusops")
HELPER_PATH = HELPER_DIR / "privileged_helper.py"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/nimbusops-privileged.service")
LAUNCHD_PLIST_PATH = Path("/Library/LaunchDaemons/com.nimbusops.privileged.plist")
SYSTEMD_AGENT_SERVICE = "nimbusops.service"
LAUNCHD_AGENT_LABEL = "com.nimbusops.agent"
LAUNCHD_HELPER_LABEL = "com.nimbusops.privileged"


class PrivilegedSetupError(RuntimeError):
    """Raised when the helper cannot be installed or controlled."""


def _run(
    argv: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=check,
            capture_output=capture_output,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise PrivilegedSetupError(f"command failed: {' '.join(argv)}{suffix}") from exc


def _require_supported_platform() -> str:
    system = platform.system()
    if system not in {"Linux", "Darwin"}:
        raise PrivilegedSetupError("root helper currently supports only Linux and macOS")
    return system


def _invoking_user(explicit: str | None = None) -> str:
    candidate = (explicit or os.environ.get("SUDO_USER") or "").strip()
    if candidate and candidate != "root":
        return candidate
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return getpass.getuser()


def _reexec_with_sudo(argv: list[str]) -> None:
    sudo = shutil.which("sudo")
    if sudo is None:
        raise PrivilegedSetupError("sudo is required to install or control the root helper")
    os.execv(
        sudo,
        [
            sudo,
            "--",
            sys.executable,
            "-m",
            "my_agent.security.privileged_setup",
            *argv,
        ],
    )


def _managed_agent_running(system: str) -> bool:
    if system == "Linux":
        completed = _run(
            ["systemctl", "show", "--property", "MainPID", "--value", SYSTEMD_AGENT_SERVICE],
            check=False,
            capture_output=True,
        )
        return completed.returncode == 0 and completed.stdout.strip() not in {"", "0"}
    completed = _run(
        ["/bin/launchctl", "print", f"system/{LAUNCHD_AGENT_LABEL}"],
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def _resolve_peer_mode(system: str, requested: str) -> str:
    if requested != "auto":
        return requested
    return "service" if _managed_agent_running(system) else "uid"


def _template_name(system: str) -> str:
    return "linux.json" if system == "Linux" else "macos.json"


def build_default_policy(
    system: str,
    *,
    service_user: str,
    peer_mode: str,
) -> dict[str, Any]:
    resource = files("my_agent.security.policies").joinpath(_template_name(system))
    payload = json.loads(resource.read_text(encoding="utf-8"))
    payload["allowedUsers"] = [service_user]
    if peer_mode == "uid":
        payload["peerVerification"] = {"mode": "uid"}
        payload["allowUidOnlyDevelopmentMode"] = True
    elif system == "Linux":
        payload["peerVerification"] = {
            "mode": "systemd",
            "service": SYSTEMD_AGENT_SERVICE,
        }
        payload.pop("allowUidOnlyDevelopmentMode", None)
    else:
        payload["peerVerification"] = {
            "mode": "launchd",
            "label": LAUNCHD_AGENT_LABEL,
        }
        payload.pop("allowUidOnlyDevelopmentMode", None)
    return validate_policy(payload, require_root_owned=False)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary_path, 0, 0)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _install_runtime() -> None:
    source = Path(__file__).with_name("privileged_helper.py")
    HELPER_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(HELPER_DIR, 0, 0)
    os.chmod(HELPER_DIR, 0o755)
    _atomic_write(HELPER_PATH, source.read_bytes(), 0o755)


def _install_policy(
    system: str,
    *,
    service_user: str,
    peer_mode: str,
    policy_source: Path | None,
    force: bool,
) -> bool:
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chown(POLICY_PATH.parent, 0, 0)
    os.chmod(POLICY_PATH.parent, 0o755)
    if POLICY_PATH.exists() and not force:
        load_policy(POLICY_PATH, require_root_owned=True)
        return False
    if policy_source is not None:
        try:
            candidate = json.loads(policy_source.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrivilegedSetupError(f"cannot read policy source: {exc}") from exc
        payload = validate_policy(candidate, require_root_owned=False)
    else:
        payload = build_default_policy(
            system,
            service_user=service_user,
            peer_mode=peer_mode,
        )
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(POLICY_PATH, encoded, 0o600)
    load_policy(POLICY_PATH, require_root_owned=True)
    return True


def _python_interpreter() -> Path:
    candidates = [Path("/usr/bin/python3"), Path(sys.executable).resolve()]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        info = candidate.stat()
        if info.st_mode & stat.S_IWOTH:
            continue
        completed = _run(
            [str(candidate), "-c", "import sys; raise SystemExit(sys.version_info < (3, 9))"],
            check=False,
        )
        if completed.returncode == 0:
            return candidate
    raise PrivilegedSetupError("Python 3.9 or newer is required for the root helper")


def _systemd_quote(value: str | Path) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _systemd_unit(python: Path, socket_group: str) -> str:
    exec_start = " ".join(
        _systemd_quote(item)
        for item in [
            python,
            HELPER_PATH,
            "--socket",
            SOCKET_PATH,
            "--policy",
            POLICY_PATH,
            "--audit-log",
            AUDIT_PATH,
            "--socket-group",
            socket_group,
        ]
    )
    return f"""[Unit]
Description=NimbusOps controlled privileged action helper
After=local-fs.target

[Service]
Type=simple
User=root
Group=root
UMask=0077
RuntimeDirectory=nimbusops
RuntimeDirectoryMode=0750
RuntimeDirectoryPreserve=yes
LogsDirectory=nimbusops
LogsDirectoryMode=0700
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
PrivateTmp=true
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
"""


def _launchd_plist(python: Path, socket_group: str) -> bytes:
    payload = {
        "Label": LAUNCHD_HELPER_LABEL,
        "ProgramArguments": [
            str(python),
            str(HELPER_PATH),
            "--socket",
            str(SOCKET_PATH),
            "--policy",
            str(POLICY_PATH),
            "--audit-log",
            str(AUDIT_PATH),
            "--socket-group",
            socket_group,
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": "/var/log/nimbusops/privileged-helper.log",
        "StandardErrorPath": "/var/log/nimbusops/privileged-helper.log",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _install_service(system: str, socket_group: str) -> None:
    python = _python_interpreter()
    log_dir = Path("/var/log/nimbusops")
    log_dir.mkdir(parents=True, exist_ok=True)
    os.chown(log_dir, 0, 0)
    os.chmod(log_dir, 0o700)
    if system == "Linux":
        _atomic_write(
            SYSTEMD_UNIT_PATH,
            _systemd_unit(python, socket_group).encode("utf-8"),
            0o644,
        )
        _run(["systemctl", "daemon-reload"])
        return
    _atomic_write(
        LAUNCHD_PLIST_PATH,
        _launchd_plist(python, socket_group),
        0o644,
    )


def _start_service(system: str) -> None:
    if system == "Linux":
        _run(["systemctl", "enable", SYSTEMD_UNIT_PATH.name])
        _run(["systemctl", "restart", SYSTEMD_UNIT_PATH.name])
        return
    _run(
        ["/bin/launchctl", "bootout", "system", str(LAUNCHD_PLIST_PATH)],
        check=False,
        capture_output=True,
    )
    _run(["/bin/launchctl", "bootstrap", "system", str(LAUNCHD_PLIST_PATH)])
    _run(["/bin/launchctl", "enable", f"system/{LAUNCHD_HELPER_LABEL}"])
    _run(["/bin/launchctl", "kickstart", "-k", f"system/{LAUNCHD_HELPER_LABEL}"])


def _stop_service(system: str) -> None:
    if system == "Linux":
        _run(["systemctl", "disable", "--now", SYSTEMD_UNIT_PATH.name], check=False)
        return
    _run(
        ["/bin/launchctl", "bootout", "system", str(LAUNCHD_PLIST_PATH)],
        check=False,
        capture_output=True,
    )


def _service_status(system: str) -> tuple[bool, str]:
    if system == "Linux":
        completed = _run(
            ["systemctl", "is-active", SYSTEMD_UNIT_PATH.name],
            check=False,
            capture_output=True,
        )
        state = (completed.stdout or completed.stderr).strip() or "unknown"
        return completed.returncode == 0, state
    completed = _run(
        ["/bin/launchctl", "print", f"system/{LAUNCHD_HELPER_LABEL}"],
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0, "running" if completed.returncode == 0 else "not loaded"


def _print_status(system: str) -> bool:
    active, state = _service_status(system)
    policy_state = "missing"
    if POLICY_PATH.is_file():
        if not os.access(POLICY_PATH, os.R_OK):
            policy_state = "present and root-protected"
        else:
            try:
                policy = load_policy(POLICY_PATH, require_root_owned=True)
                policy_state = (
                    f"valid, {len(policy['actions'])} action(s), "
                    f"peer={policy['peerVerification']['mode']}"
                )
            except Exception as exc:
                policy_state = f"invalid: {exc}"
    socket_state = "present" if SOCKET_PATH.exists() else "missing"
    print(f"service: {'running' if active else state}")
    print(f"socket:  {SOCKET_PATH} ({socket_state})")
    print(f"policy:  {POLICY_PATH} ({policy_state})")
    return active and SOCKET_PATH.exists()


def _wait_until_ready(system: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active, _ = _service_status(system)
        if active and SOCKET_PATH.exists():
            return True
        time.sleep(0.1)
    return False


def _install(
    system: str,
    *,
    service_user: str,
    peer_mode: str,
    policy_source: Path | None,
    force_policy: bool,
) -> None:
    try:
        account = pwd.getpwnam(service_user)
    except KeyError as exc:
        raise PrivilegedSetupError(f"service user does not exist: {service_user}") from exc
    socket_group = grp.getgrgid(account.pw_gid).gr_name
    resolved_mode = _resolve_peer_mode(system, peer_mode)
    _install_runtime()
    policy_created = _install_policy(
        system,
        service_user=service_user,
        peer_mode=resolved_mode,
        policy_source=policy_source,
        force=force_policy,
    )
    _install_service(system, socket_group)
    print(f"helper runtime installed: {HELPER_PATH}")
    print(f"policy {'installed' if policy_created else 'preserved'}: {POLICY_PATH}")
    print(f"socket group: {socket_group}")
    print(f"peer verification: {resolved_mode}")
    if resolved_mode == "uid":
        print(
            "warning: UID-only verification is for a manually started development server; "
            "use --peer-mode service for a managed production deployment"
        )


def _remove(system: str, *, remove_policy: bool) -> None:
    _stop_service(system)
    if system == "Linux":
        SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
        _run(["systemctl", "daemon-reload"])
    else:
        LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
    HELPER_PATH.unlink(missing_ok=True)
    try:
        HELPER_DIR.rmdir()
    except OSError:
        pass
    if remove_policy:
        POLICY_PATH.unlink(missing_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(prog="nimbusops-helper")
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=["install", "start", "restart", "stop", "status", "uninstall"],
    )
    parser.add_argument("--service-user", default=None)
    parser.add_argument(
        "--peer-mode",
        choices=["auto", "service", "uid"],
        default="auto",
    )
    parser.add_argument("--policy-source", type=Path)
    parser.add_argument("--force-policy", action="store_true")
    parser.add_argument("--remove-policy", action="store_true")
    args = parser.parse_args(raw_argv)

    try:
        system = _require_supported_platform()
        mutating = args.command != "status"
        if mutating and os.geteuid() != 0:
            _reexec_with_sudo(raw_argv)
            return

        service_user = _invoking_user(args.service_user)
        if args.command == "status":
            raise SystemExit(0 if _print_status(system) else 1)
        if args.command == "stop":
            _stop_service(system)
            _print_status(system)
            return
        if args.command == "uninstall":
            _remove(system, remove_policy=args.remove_policy)
            print("root helper service removed")
            return
        _install(
            system,
            service_user=service_user,
            peer_mode=args.peer_mode,
            policy_source=args.policy_source,
            force_policy=args.force_policy,
        )
        if args.command in {"start", "restart"}:
            _start_service(system)
            _wait_until_ready(system)
            if not _print_status(system):
                raise PrivilegedSetupError("root helper did not reach the running state")
    except PrivilegedSetupError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
