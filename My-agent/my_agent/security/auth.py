"""NimbusOps local user authentication and permission helpers."""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from my_agent.config.paths import get_runtime_subdir

SESSION_COOKIE = "nimbusops_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 390_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")

ADMIN_PERMISSIONS = frozenset(
    {
        "chat.use",
        "sessions.read_all",
        "settings.manage",
        "runtime.manage",
        "tools.install",
        "skills.install",
        "ops_shield.view",
        "ops_shield.manage",
        "sandbox.view",
        "sandbox.manage",
        "sandbox.host_exec",
        "users.manage",
        "metrics.view",
        "privileged.approve",
        "privileged.audit",
    }
)
USER_PERMISSIONS = frozenset(
    {
        "chat.use",
        "ops_shield.view",
        "sandbox.view",
        "metrics.view",
    }
)
KNOWN_PERMISSIONS = tuple(sorted(ADMIN_PERMISSIONS))
AUTH_USER_METADATA_KEY = "nimbusops_user_id"


@dataclass(frozen=True, slots=True)
class AuthUser:
    id: str
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    enabled: bool = True
    workspace_root: str | None = None
    created_at: float = 0
    last_login_at: float | None = None

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "enabled": self.enabled,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


_current_user: contextvars.ContextVar[AuthUser | None] = contextvars.ContextVar(
    "nimbusops_current_user",
    default=None,
)


def current_user() -> AuthUser | None:
    return _current_user.get()


def bind_current_user(user: AuthUser) -> contextvars.Token[AuthUser | None]:
    return _current_user.set(user)


def reset_current_user(token: contextvars.Token[AuthUser | None]) -> None:
    _current_user.reset(token)


def effective_exec_sandbox(configured: str) -> str:
    """Force non-admin WebUI users into the platform sandbox."""
    user = current_user()
    if user is None or user.has("sandbox.host_exec"):
        return configured
    return configured or "auto"


def requires_restricted_workspace() -> bool:
    """Return whether the current WebUI identity must stay inside its workspace."""
    user = current_user()
    return user is not None and not user.has("sandbox.host_exec")


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        iterations = int(raw_iterations)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


def _validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名必须为 3-64 位，只能包含字母、数字、点、横线和下划线")
    return username


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    if len(password) > 256:
        raise ValueError("密码不能超过 256 个字符")


def _normalize_workspace_root(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "\0" in text:
        raise ValueError("工作区路径包含非法字符")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("工作区路径必须是绝对路径")
    return str(path.resolve(strict=False))


def _permissions_for(role: str, explicit: Any = None) -> frozenset[str]:
    if role == "system_admin":
        return ADMIN_PERMISSIONS
    if explicit is None:
        return USER_PERMISSIONS
    if not isinstance(explicit, list):
        raise ValueError("permissions 必须是数组")
    values = {str(item) for item in explicit if str(item) in ADMIN_PERMISSIONS}
    values.update(USER_PERMISSIONS)
    values.discard("users.manage")
    values.discard("settings.manage")
    values.discard("runtime.manage")
    values.discard("sandbox.host_exec")
    return frozenset(values)


class UserStore:
    """Small JSON-backed user directory for local NimbusOps deployments."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def _path(self) -> Path:
        return self.path or (get_runtime_subdir("auth") / "users.json")

    def _read(self) -> dict[str, Any]:
        path = self._path()
        if not path.is_file():
            return {"version": 1, "users": []}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "users": []}
        if not isinstance(raw, dict) or not isinstance(raw.get("users"), list):
            return {"version": 1, "users": []}
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temp.write_text(encoded, encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)

    def setup_required(self) -> bool:
        return not self._read()["users"]

    def _to_user(self, row: dict[str, Any]) -> AuthUser:
        role = "system_admin" if row.get("role") == "system_admin" else "user"
        return AuthUser(
            id=str(row.get("id") or ""),
            username=str(row.get("username") or ""),
            display_name=str(row.get("display_name") or row.get("username") or ""),
            role=role,
            permissions=_permissions_for(role, row.get("permissions")),
            enabled=row.get("enabled") is not False,
            workspace_root=(
                _normalize_workspace_root(row.get("workspace_root"))
                if role != "system_admin"
                else None
            ),
            created_at=float(row.get("created_at") or 0),
            last_login_at=(
                float(row["last_login_at"])
                if isinstance(row.get("last_login_at"), (int, float))
                else None
            ),
        )

    def list_users(self) -> list[AuthUser]:
        return [self._to_user(row) for row in self._read()["users"] if isinstance(row, dict)]

    def find_by_id(self, user_id: str) -> AuthUser | None:
        return next((user for user in self.list_users() if user.id == user_id), None)

    def find_by_username(self, username: str) -> AuthUser | None:
        normalized = username.strip().casefold()
        return next(
            (user for user in self.list_users() if user.username.casefold() == normalized),
            None,
        )

    def create(
        self,
        *,
        username: str,
        password: str,
        display_name: str = "",
        role: str = "user",
        permissions: Any = None,
        workspace_root: Any = None,
        initial_setup: bool = False,
    ) -> AuthUser:
        username = _validate_username(username)
        _validate_password(password)
        data = self._read()
        if initial_setup:
            if data["users"]:
                raise ValueError("管理员已经初始化")
            role = "system_admin"
        if role not in {"system_admin", "user"}:
            raise ValueError("无效的用户角色")
        if any(
            isinstance(row, dict)
            and str(row.get("username") or "").casefold() == username.casefold()
            for row in data["users"]
        ):
            raise ValueError("用户名已存在")
        now = time.time()
        row = {
            "id": uuid.uuid4().hex,
            "username": username,
            "display_name": display_name.strip() or username,
            "role": role,
            "permissions": sorted(_permissions_for(role, permissions)),
            "enabled": True,
            "workspace_root": (
                _normalize_workspace_root(workspace_root)
                if role != "system_admin"
                else None
            ),
            "password_hash": _password_hash(password),
            "created_at": now,
            "last_login_at": None,
        }
        data["users"].append(row)
        self._write(data)
        return self._to_user(row)

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        data = self._read()
        normalized = username.strip().casefold()
        for row in data["users"]:
            if not isinstance(row, dict):
                continue
            if str(row.get("username") or "").casefold() != normalized:
                continue
            if row.get("enabled") is False:
                return None
            if not _verify_password(password, str(row.get("password_hash") or "")):
                return None
            row["last_login_at"] = time.time()
            self._write(data)
            return self._to_user(row)
        return None

    def update(self, user_id: str, changes: dict[str, Any]) -> AuthUser:
        data = self._read()
        target: dict[str, Any] | None = None
        for row in data["users"]:
            if isinstance(row, dict) and row.get("id") == user_id:
                target = row
                break
        if target is None:
            raise KeyError("用户不存在")
        if "display_name" in changes:
            target["display_name"] = str(changes["display_name"]).strip() or target["username"]
        if "role" in changes:
            role = str(changes["role"])
            if role not in {"system_admin", "user"}:
                raise ValueError("无效的用户角色")
            if target.get("role") == "system_admin" and role != "system_admin":
                admins = [
                    row for row in data["users"]
                    if isinstance(row, dict)
                    and row.get("role") == "system_admin"
                    and row.get("enabled") is not False
                ]
                if len(admins) <= 1:
                    raise ValueError("至少需要保留一个启用的系统管理员")
            target["role"] = role
        role = "system_admin" if target.get("role") == "system_admin" else "user"
        if "permissions" in changes or "role" in changes:
            target["permissions"] = sorted(_permissions_for(role, changes.get("permissions")))
        if role == "system_admin":
            target["workspace_root"] = None
        elif "workspace_root" in changes:
            workspace_root = _normalize_workspace_root(changes.get("workspace_root"))
            if workspace_root is None:
                raise ValueError("普通用户必须配置工作区根路径")
            target["workspace_root"] = workspace_root
        if "enabled" in changes:
            enabled = bool(changes["enabled"])
            if not enabled and role == "system_admin":
                admins = [
                    row for row in data["users"]
                    if isinstance(row, dict)
                    and row.get("role") == "system_admin"
                    and row.get("enabled") is not False
                    and row.get("id") != user_id
                ]
                if not admins:
                    raise ValueError("至少需要保留一个启用的系统管理员")
            target["enabled"] = enabled
        if "password" in changes and str(changes["password"]):
            password = str(changes["password"])
            _validate_password(password)
            target["password_hash"] = _password_hash(password)
        self._write(data)
        return self._to_user(target)

    def delete(self, user_id: str, *, actor_id: str) -> bool:
        if user_id == actor_id:
            raise ValueError("不能删除当前登录用户")
        data = self._read()
        before = len(data["users"])
        data["users"] = [
            row for row in data["users"]
            if not (isinstance(row, dict) and row.get("id") == user_id)
        ]
        if len(data["users"]) == before:
            return False
        if not any(
            isinstance(row, dict)
            and row.get("role") == "system_admin"
            and row.get("enabled") is not False
            for row in data["users"]
        ):
            raise ValueError("至少需要保留一个启用的系统管理员")
        self._write(data)
        return True


class LoginSessionStore:
    """In-memory opaque browser sessions backed by the persistent user directory."""

    def __init__(self, users: UserStore) -> None:
        self.users = users
        self.sessions: dict[str, tuple[str, float]] = {}

    def issue(self, user: AuthUser, ttl: int = SESSION_TTL_SECONDS) -> str:
        token = secrets.token_urlsafe(40)
        self.sessions[token] = (user.id, time.monotonic() + ttl)
        return token

    def revoke(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    def resolve(self, token: str | None) -> AuthUser | None:
        if not token:
            return None
        entry = self.sessions.get(token)
        if entry is None:
            return None
        user_id, expiry = entry
        if time.monotonic() > expiry:
            self.sessions.pop(token, None)
            return None
        user = self.users.find_by_id(user_id)
        if user is None or not user.enabled:
            self.sessions.pop(token, None)
            return None
        return user
