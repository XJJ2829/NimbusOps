"""Operating-system account enforcement for NimbusOps processes."""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from typing import Any

from my_agent.config.schema import RuntimeIdentityConfig

_ACCOUNT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


class RuntimeIdentityError(RuntimeError):
    """Raised when the configured system identity cannot be enforced."""


@dataclass(frozen=True, slots=True)
class SystemIdentity:
    username: str
    uid: int
    group: str
    gid: int
    home: str
    shell: str
    supplementary_gids: tuple[int, ...]


def validate_runtime_identity(config: RuntimeIdentityConfig) -> None:
    if not _ACCOUNT_RE.fullmatch(config.account.strip()):
        raise ValueError("系统账号名称格式不正确")
    if config.group.strip() and not _ACCOUNT_RE.fullmatch(config.group.strip()):
        raise ValueError("系统用户组名称格式不正确")
    _parse_umask(config.process_umask)


def _parse_umask(value: str) -> int:
    try:
        parsed = int(value, 8)
    except ValueError as exc:
        raise ValueError("进程 umask 必须是 3-4 位八进制数") from exc
    if parsed < 0 or parsed > 0o777:
        raise ValueError("进程 umask 必须位于 000-777 之间")
    return parsed


def resolve_system_identity(config: RuntimeIdentityConfig) -> SystemIdentity:
    if os.name != "posix":
        raise RuntimeIdentityError("运行身份隔离目前仅支持 Linux 和 macOS")

    import grp
    import pwd

    account = config.account.strip()
    try:
        user_record = pwd.getpwnam(account)
    except KeyError as exc:
        raise RuntimeIdentityError(f"系统账号 {account!r} 不存在") from exc

    group_name = config.group.strip()
    if group_name:
        try:
            group_record = grp.getgrnam(group_name)
        except KeyError as exc:
            raise RuntimeIdentityError(f"系统用户组 {group_name!r} 不存在") from exc
    else:
        group_record = grp.getgrgid(user_record.pw_gid)

    try:
        supplementary = tuple(sorted(set(os.getgrouplist(account, group_record.gr_gid))))
    except OSError:
        supplementary = (group_record.gr_gid,)

    return SystemIdentity(
        username=account,
        uid=user_record.pw_uid,
        group=group_record.gr_name,
        gid=group_record.gr_gid,
        home=user_record.pw_dir,
        shell=user_record.pw_shell,
        supplementary_gids=supplementary,
    )


def current_identity() -> dict[str, Any]:
    if os.name != "posix":
        return {
            "username": os.environ.get("USERNAME") or "unknown",
            "uid": None,
            "gid": None,
            "is_root": False,
        }

    import grp
    import pwd

    uid = os.geteuid()
    gid = os.getegid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = str(gid)
    return {
        "username": username,
        "uid": uid,
        "group": group,
        "gid": gid,
        "is_root": uid == 0,
    }


def enforce_startup_identity(config: RuntimeIdentityConfig) -> None:
    """Reject unsafe service identities before NimbusOps starts accepting work."""
    validate_runtime_identity(config)
    if os.name != "posix":
        if config.enabled:
            raise RuntimeIdentityError("运行身份隔离目前仅支持 Linux 和 macOS")
        return

    uid = os.geteuid()
    if config.deny_root_startup and uid == 0:
        raise RuntimeIdentityError(
            "NimbusOps 已禁止以 root 启动。请使用受限系统账号启动服务。"
        )
    if not config.enabled:
        return

    target = resolve_system_identity(config)
    if uid != target.uid:
        raise RuntimeIdentityError(
            f"NimbusOps 必须以受限系统账号 {target.username!r} 启动；"
            f"当前有效 UID 为 {uid}。"
        )


def subprocess_identity_kwargs(config: RuntimeIdentityConfig | None) -> dict[str, Any]:
    """Return safe subprocess options for UID/GID enforcement and file modes."""
    if config is None or not config.enabled:
        return {}
    validate_runtime_identity(config)
    target = resolve_system_identity(config)
    current_uid = os.geteuid()

    kwargs: dict[str, Any] = {"umask": _parse_umask(config.process_umask)}
    if current_uid == target.uid:
        return kwargs
    if current_uid != 0:
        raise RuntimeIdentityError(
            f"无法从当前 UID {current_uid} 切换到系统账号 {target.username!r}；"
            "请直接使用该账号启动 NimbusOps。"
        )
    kwargs.update(
        {
            "user": target.uid,
            "group": target.gid,
            "extra_groups": list(target.supplementary_gids),
        }
    )
    return kwargs


def apply_identity_environment(
    env: dict[str, str],
    config: RuntimeIdentityConfig | None,
) -> dict[str, str]:
    if config is None or not config.enabled:
        return env
    target = resolve_system_identity(config)
    next_env = dict(env)
    next_env["HOME"] = target.home
    next_env["USER"] = target.username
    next_env["LOGNAME"] = target.username
    return next_env


def runtime_identity_payload(config: RuntimeIdentityConfig) -> dict[str, Any]:
    current = current_identity()
    target: SystemIdentity | None = None
    error: str | None = None
    try:
        validate_runtime_identity(config)
        target = resolve_system_identity(config)
    except (ValueError, RuntimeIdentityError) as exc:
        if config.enabled:
            error = str(exc)

    target_matches = target is not None and current.get("uid") == target.uid
    root_blocked = not current.get("is_root") or not config.deny_root_startup
    supported = os.name == "posix"
    compliant = bool(
        supported
        and root_blocked
        and (
            not config.enabled
            or (
                target_matches
                and target is not None
            )
        )
    )
    setup_command = ""
    if platform.system() == "Linux":
        setup_command = (
            f"sudo useradd --system --create-home --shell /usr/sbin/nologin "
            f"{config.account.strip() or 'nimbusops'}"
        )
    elif platform.system() == "Darwin":
        setup_command = (
            "请在“系统设置 > 用户与群组”中创建标准用户，"
            "并使用该账号启动 NimbusOps。"
        )
    process_umask = _parse_umask(config.process_umask)
    file_mode = format(0o666 & ~process_umask, "04o")
    directory_mode = format(0o777 & ~process_umask, "04o")

    return {
        "platform": platform.system(),
        "supported": supported,
        "configured": {
            "enabled": config.enabled,
            "account": config.account,
            "group": config.group,
            "deny_root_startup": config.deny_root_startup,
            "process_umask": config.process_umask,
        },
        "current": current,
        "target": (
            {
                "exists": True,
                "username": target.username,
                "uid": target.uid,
                "group": target.group,
                "gid": target.gid,
                "home": target.home,
                "shell": target.shell,
            }
            if target is not None
            else {
                "exists": False,
                "username": config.account,
                "group": config.group,
            }
        ),
        "target_matches_current": target_matches,
        "root_startup_blocked": config.deny_root_startup,
        "file_policy": {
            "umask": config.process_umask,
            "new_file_mode": file_mode,
            "new_directory_mode": directory_mode,
            "description": "由目标系统账号、用户组和进程 umask 共同执行文件权限隔离。",
        },
        "compliant": compliant,
        "requires_restart": config.enabled and not target_matches,
        "error": error,
        "setup_command": setup_command,
    }
