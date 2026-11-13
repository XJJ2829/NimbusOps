"""Root-owned Unix socket helper for narrowly scoped privileged actions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pwd
import re
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
_DARWIN_SOL_LOCAL = 0
_DARWIN_LOCAL_PEERCRED = 1
_DARWIN_LOCAL_PEERPID = 2
_DARWIN_XUCRED_SIZE = 76
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class HelperPolicyError(RuntimeError):
    """Raised when the root-owned helper policy is invalid."""


def _secure_root_owned_file(path: Path, *, label: str) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise HelperPolicyError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise HelperPolicyError(f"{label} must be a regular file: {path}")
    if info.st_uid != 0:
        raise HelperPolicyError(f"{label} must be owned by root: {path}")
    if info.st_mode & 0o022:
        raise HelperPolicyError(f"{label} must not be group/world writable: {path}")


def load_policy(path: Path, *, require_root_owned: bool = True) -> dict[str, Any]:
    if require_root_owned:
        _secure_root_owned_file(path, label="policy")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelperPolicyError(f"cannot read policy: {exc}") from exc
    return validate_policy(payload, require_root_owned=require_root_owned)


def validate_policy(
    payload: Any,
    *,
    require_root_owned: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise HelperPolicyError("policy version must be 1")
    actions = payload.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise HelperPolicyError("policy actions must be a non-empty object")
    verification = payload.get("peerVerification")
    if not isinstance(verification, dict):
        raise HelperPolicyError("peerVerification must be configured")
    mode = verification.get("mode")
    if mode not in {"systemd", "launchd", "uid"}:
        raise HelperPolicyError("peerVerification.mode must be systemd, launchd, or uid")
    if mode == "systemd" and not str(verification.get("service") or "").strip():
        raise HelperPolicyError("peerVerification.service is required")
    if mode == "launchd" and not str(verification.get("label") or "").strip():
        raise HelperPolicyError("peerVerification.label is required")
    allowed_users = payload.get("allowedUsers")
    if not isinstance(allowed_users, list) or not allowed_users:
        raise HelperPolicyError("allowedUsers must contain the NimbusOps service account")
    for name, raw in actions.items():
        _validate_action_definition(str(name), raw, require_root_owned=require_root_owned)
    return payload


def _validate_action_definition(
    name: str,
    raw: Any,
    *,
    require_root_owned: bool,
) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name):
        raise HelperPolicyError(f"invalid action name: {name!r}")
    if not isinstance(raw, dict):
        raise HelperPolicyError(f"action {name!r} must be an object")
    executable = Path(str(raw.get("executable") or ""))
    if not executable.is_absolute():
        raise HelperPolicyError(f"action {name!r} executable must be absolute")
    if require_root_owned:
        _secure_root_owned_file(executable, label=f"action {name!r} executable")
    arguments = raw.get("arguments")
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise HelperPolicyError(f"action {name!r} arguments must be a string array")
    parameters = raw.get("parameters", {})
    if not isinstance(parameters, dict):
        raise HelperPolicyError(f"action {name!r} parameters must be an object")
    placeholders = {
        match
        for argument in arguments
        for match in _PLACEHOLDER_RE.findall(argument)
    }
    unknown = placeholders - set(parameters)
    if unknown:
        raise HelperPolicyError(
            f"action {name!r} uses undefined parameters: {', '.join(sorted(unknown))}"
        )
    for parameter_name, schema in parameters.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(parameter_name)):
            raise HelperPolicyError(f"action {name!r} has invalid parameter name")
        if not isinstance(schema, dict) or schema.get("type", "string") not in {
            "string",
            "integer",
            "boolean",
        }:
            raise HelperPolicyError(
                f"action {name!r} parameter {parameter_name!r} has unsupported type"
            )


def public_actions(policy: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, raw in policy["actions"].items():
        result.append(
            {
                "name": name,
                "label": str(raw.get("label") or name),
                "description": str(raw.get("description") or ""),
                "category": str(raw.get("category") or "system"),
                "impact": str(raw.get("impact") or "需要 root 权限"),
                "parameters": raw.get("parameters", {}),
                "timeout": int(raw.get("timeout") or 60),
            }
        )
    return result


def _write_policy(path: Path, policy: dict[str, Any]) -> None:
    encoded = (json.dumps(policy, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), 0, 0)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chown(path, 0, 0)
        os.chmod(path, 0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


def _peer_credentials(sock: socket.socket) -> tuple[int | None, int | None, int | None]:
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)
    uid: int | None = None
    gid: int | None = None
    getpeereid = getattr(sock, "getpeereid", None)
    if callable(getpeereid):
        uid, gid = getpeereid()
    elif hasattr(socket, "LOCAL_PEERCRED"):
        raw_cred = sock.getsockopt(
            getattr(socket, "SOL_LOCAL", _DARWIN_SOL_LOCAL),
            socket.LOCAL_PEERCRED,
            _DARWIN_XUCRED_SIZE,
        )
        if len(raw_cred) < 12:
            raise PermissionError("peer credentials response is truncated")
        version, uid = struct.unpack_from("=II", raw_cred, 0)
        group_count = struct.unpack_from("=h", raw_cred, 8)[0]
        if version != 0:
            raise PermissionError(f"unsupported peer credentials version {version}")
        if group_count > 0 and len(raw_cred) >= 16:
            gid = struct.unpack_from("=I", raw_cred, 12)[0]
    pid: int | None = None
    try:
        raw_pid = sock.getsockopt(
            getattr(socket, "SOL_LOCAL", _DARWIN_SOL_LOCAL),
            getattr(socket, "LOCAL_PEERPID", _DARWIN_LOCAL_PEERPID),
            struct.calcsize("i"),
        )
        pid = struct.unpack("i", raw_pid)[0]
    except OSError:
        pass
    return pid, uid, gid


def _systemd_main_pid(service: str) -> int:
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    completed = subprocess.run(
        [systemctl, "show", "--property", "MainPID", "--value", service],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise PermissionError(f"cannot resolve systemd MainPID for {service}")
    try:
        return int(completed.stdout.strip())
    except ValueError as exc:
        raise PermissionError(f"invalid systemd MainPID for {service}") from exc


def _launchd_main_pid(label: str) -> int:
    completed = subprocess.run(
        ["/bin/launchctl", "print", f"system/{label}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise PermissionError(f"cannot resolve launchd PID for {label}")
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", completed.stdout, re.MULTILINE)
    if not match:
        raise PermissionError(f"launchd service {label} has no active PID")
    return int(match.group(1))


def verify_peer(
    policy: dict[str, Any],
    *,
    pid: int | None,
    uid: int | None,
) -> str:
    if uid is None:
        raise PermissionError("peer UID is unavailable")
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError as exc:
        raise PermissionError(f"unknown peer UID {uid}") from exc
    if username not in policy["allowedUsers"]:
        raise PermissionError(f"peer account {username!r} is not allowed")
    verification = policy["peerVerification"]
    mode = verification["mode"]
    if mode == "uid":
        if not policy.get("allowUidOnlyDevelopmentMode", False):
            raise PermissionError("uid-only peer verification is disabled")
        return username
    if pid is None:
        raise PermissionError("peer PID is unavailable")
    if mode == "systemd":
        expected = _systemd_main_pid(str(verification["service"]))
    else:
        expected = _launchd_main_pid(str(verification["label"]))
    if expected <= 0 or pid != expected:
        raise PermissionError(
            f"peer PID {pid} is not the configured NimbusOps service MainPID {expected}"
        )
    return username


def _validate_parameter(name: str, value: Any, schema: dict[str, Any]) -> Any:
    parameter_type = schema.get("type", "string")
    if parameter_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"parameter {name!r} must be a string")
        normalized: Any = value
        if "\0" in value or "\n" in value or "\r" in value:
            raise ValueError(f"parameter {name!r} contains invalid characters")
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ValueError(f"parameter {name!r} is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ValueError(f"parameter {name!r} is too long")
        if schema.get("pattern") and not re.fullmatch(str(schema["pattern"]), value):
            raise ValueError(f"parameter {name!r} does not match the allowed pattern")
    elif parameter_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"parameter {name!r} must be an integer")
        normalized = value
        if "minimum" in schema and value < int(schema["minimum"]):
            raise ValueError(f"parameter {name!r} is below the minimum")
        if "maximum" in schema and value > int(schema["maximum"]):
            raise ValueError(f"parameter {name!r} exceeds the maximum")
    else:
        if not isinstance(value, bool):
            raise ValueError(f"parameter {name!r} must be a boolean")
        normalized = value
    if "enum" in schema and normalized not in schema["enum"]:
        raise ValueError(f"parameter {name!r} is not in the allowlist")
    return normalized


def build_argv(
    policy: dict[str, Any],
    action_name: str,
    parameters: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    raw = policy["actions"].get(action_name)
    if not isinstance(raw, dict):
        raise ValueError(f"unknown privileged action: {action_name}")
    schemas = raw.get("parameters", {})
    if set(parameters) - set(schemas):
        raise ValueError("request contains parameters not defined by the action")
    normalized: dict[str, Any] = {}
    for name, schema in schemas.items():
        if name not in parameters:
            if schema.get("required", True):
                raise ValueError(f"missing required parameter: {name}")
            if "default" in schema:
                normalized[name] = schema["default"]
            continue
        normalized[name] = _validate_parameter(name, parameters[name], schema)

    def replace(template: str) -> str:
        def value(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in normalized:
                raise ValueError(f"missing value for argument placeholder: {key}")
            raw_value = normalized[key]
            if isinstance(raw_value, bool):
                return "true" if raw_value else "false"
            return str(raw_value)

        return _PLACEHOLDER_RE.sub(value, template)

    executable = str(raw["executable"])
    argv = [executable, *(replace(item) for item in raw["arguments"])]
    return argv, raw


def _append_audit(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


class PrivilegedHelperServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        policy_path: Path,
        audit_path: Path,
        socket_group: str,
        require_root_owned: bool = True,
    ) -> None:
        self.socket_path = socket_path
        self.policy_path = policy_path
        self.audit_path = audit_path
        self.socket_group = socket_group
        self.require_root_owned = require_root_owned

    async def serve(self) -> None:
        if os.geteuid() != 0:
            raise SystemExit("nimbusops-privileged-helper must run as root")
        policy = load_policy(self.policy_path, require_root_owned=self.require_root_owned)
        import grp

        gid = grp.getgrnam(self.socket_group).gr_gid
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chown(self.socket_path.parent, 0, gid)
        os.chmod(self.socket_path.parent, 0o750)
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        os.chown(self.socket_path, 0, gid)
        os.chmod(self.socket_path, 0o660)
        async with server:
            await server.serve_forever()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        started = time.monotonic()
        request: dict[str, Any] = {}
        peer_pid: int | None = None
        peer_uid: int | None = None
        username = "unknown"
        try:
            transport_socket = writer.get_extra_info("socket")
            if transport_socket is None:
                raise PermissionError("peer socket is unavailable")
            peer_pid, peer_uid, _ = _peer_credentials(transport_socket)
            policy = load_policy(
                self.policy_path,
                require_root_owned=self.require_root_owned,
            )
            username = verify_peer(policy, pid=peer_pid, uid=peer_uid)
            raw = await reader.readline()
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request is too large")
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("protocol") != PROTOCOL_VERSION:
                raise ValueError("unsupported protocol")
            operation = request.get("operation")
            if operation == "status":
                response = {
                    "ok": True,
                    "helper_uid": os.geteuid(),
                    "peer_verified": True,
                    "peer_mode": policy["peerVerification"]["mode"],
                    "policy_path": str(self.policy_path),
                    "action_count": len(policy["actions"]),
                    "capabilities": [
                        "get_policy",
                        "validate_policy",
                        "add_action",
                    ],
                }
            elif operation == "list_actions":
                response = {"ok": True, "actions": public_actions(policy)}
            elif operation == "get_policy":
                response = {"ok": True, "policy": policy}
            elif operation == "validate_policy":
                candidate = validate_policy(
                    request.get("policy"),
                    require_root_owned=self.require_root_owned,
                )
                response = {
                    "ok": True,
                    "policy": candidate,
                    "actions": public_actions(candidate),
                }
            elif operation == "add_action":
                response = self._add_action(policy, request)
            elif operation == "execute":
                response = await self._execute(policy, request)
            else:
                raise ValueError("unknown operation")
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        response["request_id"] = request.get("request_id")
        writer.write(
            (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
        if request.get("operation") in {"execute", "add_action"}:
            _append_audit(
                self.audit_path,
                {
                    "timestamp": time.time(),
                    "request_id": request.get("request_id"),
                    "operation": request.get("operation"),
                    "approval": request.get("approval"),
                    "actor": request.get("actor"),
                    "action_name": request.get("action_name"),
                    "action": request.get("action"),
                    "parameters": request.get("parameters"),
                    "peer_pid": peer_pid,
                    "peer_uid": peer_uid,
                    "peer_user": username,
                    "ok": response.get("ok"),
                    "exit_code": response.get("exit_code"),
                    "error": response.get("error"),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )

    def _add_action(
        self,
        policy: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        actor = request.get("actor")
        if not isinstance(actor, dict) or actor.get("role") != "system_admin":
            raise PermissionError("only a system administrator may add privileged actions")
        if not actor.get("id") or not actor.get("username"):
            raise PermissionError("administrator identity is incomplete")
        action_name = str(request.get("action_name") or "").strip()
        action = request.get("action")
        if not isinstance(action, dict):
            raise ValueError("action must be an object")
        if action_name in policy["actions"]:
            raise ValueError(f"privileged action {action_name!r} already exists")

        candidate = json.loads(json.dumps(policy))
        candidate["actions"][action_name] = action
        validated = validate_policy(
            candidate,
            require_root_owned=self.require_root_owned,
        )
        _write_policy(self.policy_path, validated)
        reloaded = load_policy(
            self.policy_path,
            require_root_owned=self.require_root_owned,
        )
        return {
            "ok": True,
            "action": next(
                item for item in public_actions(reloaded)
                if item.get("name") == action_name
            ),
            "actions": public_actions(reloaded),
            "policy": reloaded,
        }

    async def _execute(
        self,
        policy: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        approval = request.get("approval")
        if not isinstance(approval, dict) or not approval.get("id"):
            raise PermissionError("an administrator approval record is required")
        if approval.get("actor_role") != "system_admin":
            raise PermissionError("approval actor must be a system administrator")
        action_name = str(request.get("action_name") or "")
        parameters = request.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be an object")
        argv, action = build_argv(policy, action_name, parameters)
        timeout = max(1, min(int(action.get("timeout") or 60), 1800))
        env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/root",
        }
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/",
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"privileged action timed out after {timeout} seconds")
        return {
            "ok": True,
            "action_name": action_name,
            "exit_code": process.returncode,
            "stdout": stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "stderr": stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nimbusops-privileged-helper")
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path("/var/run/nimbusops/privileged.sock"),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("/etc/nimbusops/privileged-actions.json"),
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path("/var/log/nimbusops/privileged-actions.jsonl"),
    )
    parser.add_argument("--socket-group", default="nimbusops")
    args = parser.parse_args(argv)
    server = PrivilegedHelperServer(
        socket_path=args.socket,
        policy_path=args.policy,
        audit_path=args.audit_log,
        socket_group=args.socket_group,
    )
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
