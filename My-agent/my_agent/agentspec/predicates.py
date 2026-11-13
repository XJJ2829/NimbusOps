"""Predicate registration for OpsShield rules."""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePath
from typing import Any

from my_agent.agentspec.action_ast import (
    ActionAst,
    ShellCommand,
    ShellRedirection,
    ShellScript,
    action_ast_is_destructive,
    parse_action_ast,
    parse_shell_source,
)
from my_agent.security.workspace_access import current_workspace_scope

Predicate = Callable[[Any, Any, list[Any]], bool]
predicate_table: dict[str, Predicate] = {}
_predicate_metadata: dict[str, dict[str, str | None]] = {}
_current_action_ast: ContextVar[ActionAst | None] = ContextVar(
    "ops_shield_current_action_ast",
    default=None,
)

_LARGE_REPLACEMENT_CHARS = 200
_LARGE_REPLACEMENT_LINES = 10
_SHELL_WRITE_REDIRECTS = frozenset({">", ">>", "<>", ">&", "&>", "&>>"})
_SHELL_INTERPRETERS = frozenset({"ash", "bash", "dash", "ksh", "sh", "zsh"})
_SHELL_WRAPPERS = frozenset(
    {
        "builtin",
        "chroot",
        "command",
        "doas",
        "env",
        "exec",
        "nice",
        "nohup",
        "nsenter",
        "pkexec",
        "setsid",
        "sudo",
        "systemd-run",
        "timeout",
        "unshare",
    }
)
_WRAPPER_OPTIONS_WITH_VALUE = frozenset(
    {
        "--argv0",
        "--chdir",
        "--default-signal",
        "--gid",
        "--group",
        "--host",
        "--ignore-signal",
        "--kill-after",
        "--machine",
        "--nice",
        "--preserve-status",
        "--property",
        "--same-dir",
        "--scope",
        "--setenv",
        "--signal",
        "--uid",
        "--unit",
        "--unset",
        "--user",
        "--working-directory",
        "-a",
        "-C",
        "-D",
        "-g",
        "-G",
        "-H",
        "-k",
        "-M",
        "-n",
        "-p",
        "-s",
        "-S",
        "-t",
        "-u",
        "-U",
    }
)
_SERVICE_MUTATING_VERBS = frozenset(
    {
        "daemon-reexec",
        "daemon-reload",
        "disable",
        "edit",
        "enable",
        "isolate",
        "kill",
        "link",
        "mask",
        "preset",
        "preset-all",
        "reload",
        "reload-or-restart",
        "reenable",
        "reset-failed",
        "restart",
        "revert",
        "set-default",
        "set-property",
        "start",
        "stop",
        "switch-root",
        "try-reload-or-restart",
        "try-restart",
        "unmask",
    }
)
_PROTECTED_SERVICE_VERBS = frozenset({"disable", "isolate", "kill", "mask", "stop"})
_PROTECTED_SERVICES = frozenset(
    {
        "auditd",
        "dbus",
        "firewalld",
        "iptables",
        "kylin-agent",
        "kysec",
        "network",
        "networkmanager",
        "nftables",
        "nimbusops",
        "nimbusops-privileged",
        "polkit",
        "rsyslog",
        "sshd",
        "ssh",
        "systemd-journald",
        "systemd-logind",
    }
)
_PROTECTED_TARGETS = frozenset({"default", "graphical", "multi-user", "rescue", "emergency"})
_PACKAGE_MANAGERS = frozenset(
    {"apt", "apt-get", "dnf", "dpkg", "rpm", "yum", "zypper"}
)
_PACKAGE_MANAGER_VERBS = frozenset(
    {
        "autoremove",
        "dist-upgrade",
        "downgrade",
        "erase",
        "full-upgrade",
        "history",
        "install",
        "mark",
        "reinstall",
        "remove",
        "rollback",
        "swap",
        "update",
        "upgrade",
    }
)
_RPM_MUTATING_LONG_OPTIONS = frozenset(
    {
        "--erase",
        "--freshen",
        "--import",
        "--install",
        "--rebuilddb",
        "--setperms",
        "--setugids",
        "--upgrade",
    }
)
_IDENTITY_AUTH_PROGRAMS = frozenset(
    {
        "authconfig",
        "authselect",
        "chage",
        "chpasswd",
        "faillock",
        "gpasswd",
        "groupadd",
        "groupdel",
        "groupmod",
        "newusers",
        "pam-auth-update",
        "passwd",
        "usermod",
        "useradd",
        "userdel",
        "vigr",
        "vipw",
        "visudo",
    }
)
_SECURITY_CONTROL_PROGRAMS = frozenset(
    {
        "auditctl",
        "chcon",
        "firewall-cmd",
        "iptables",
        "iptables-restore",
        "ip6tables",
        "ip6tables-restore",
        "kysec",
        "kysec_set",
        "nft",
        "restorecon",
        "semanage",
        "setenforce",
        "setsebool",
        "ufw",
    }
)
_KERNEL_BOOT_PROGRAMS = frozenset(
    {
        "bootctl",
        "dracut",
        "grubby",
        "grub-mkconfig",
        "grub2-mkconfig",
        "insmod",
        "kernel-install",
        "kexec",
        "modprobe",
        "rmmod",
        "sysctl",
        "update-grub",
    }
)
_NETWORK_PROGRAMS = frozenset(
    {
        "ethtool",
        "hostnamectl",
        "ifconfig",
        "ip",
        "nmcli",
        "resolvectl",
        "route",
        "tc",
    }
)
_REMOTE_EXEC_INTERPRETERS = frozenset(
    {"bash", "dash", "ksh", "node", "perl", "php", "python", "python2", "python3", "ruby", "sh", "zsh"}
)
_SENSITIVE_READ_PROGRAMS = frozenset(
    {
        "awk",
        "base64",
        "cat",
        "grep",
        "head",
        "less",
        "more",
        "openssl",
        "sed",
        "strings",
        "tail",
    }
)
_PROTECTED_WRITE_PROGRAMS = frozenset(
    {
        "chgrp",
        "chmod",
        "chown",
        "cp",
        "install",
        "ln",
        "mkdir",
        "mv",
        "perl",
        "rm",
        "rmdir",
        "sed",
        "setfacl",
        "shred",
        "tee",
        "touch",
        "truncate",
        "unlink",
    }
)
_PROTECTED_WRITE_PATHS = (
    "/bin",
    "/boot",
    "/etc",
    "/lib",
    "/lib64",
    "/opt",
    "/root",
    "/sbin",
    "/usr",
    "/var/lib/apt",
    "/var/lib/dnf",
    "/var/lib/dpkg",
    "/var/lib/rpm",
    "/var/lib/yum",
    "/var/spool/cron",
)
_SENSITIVE_READ_PATHS = (
    "/etc/gshadow",
    "/etc/nimbusops/privileged-actions.json",
    "/etc/passwd-",
    "/etc/shadow",
    "/etc/shadow-",
    "/etc/ssh/ssh_host_dsa_key",
    "/etc/ssh/ssh_host_ecdsa_key",
    "/etc/ssh/ssh_host_ed25519_key",
    "/etc/ssh/ssh_host_rsa_key",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/root/.ssh",
)
_AUTH_CONFIG_PATHS = (
    "/etc/group",
    "/etc/gshadow",
    "/etc/nsswitch.conf",
    "/etc/pam.d",
    "/etc/passwd",
    "/etc/security",
    "/etc/shadow",
    "/etc/ssh/sshd_config",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/root/.ssh/authorized_keys",
)


def current_action_ast() -> ActionAst | None:
    """Return the action AST currently being evaluated by an OpsShield Predicate."""
    return _current_action_ast.get()


@contextmanager
def action_ast_context(action_ast: ActionAst):
    token = _current_action_ast.set(action_ast)
    try:
        yield
    finally:
        _current_action_ast.reset(token)


def register_predicate(
    name: str,
    predicate: Predicate,
    *,
    replace: bool = False,
    source_kind: str | None = None,
    source_path: str | None = None,
    source: str | None = None,
) -> None:
    """Register a predicate used by OpsShield's ``check`` clause."""
    normalized = name.strip()
    if not normalized or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
        raise ValueError(f"Invalid OpsShield predicate name: {name!r}")
    if normalized in predicate_table and not replace:
        raise ValueError(f"OpsShield predicate already registered: {normalized}")
    predicate_table[normalized] = predicate
    module = getattr(predicate, "__module__", "") or ""
    _predicate_metadata[normalized] = {
        "source_kind": source_kind
        or ("builtin" if module.startswith("my_agent.agentspec.predicates") else "custom"),
        "source_path": source_path,
        "source": source,
    }


def unregister_predicate(name: str) -> None:
    """Remove one Predicate and its display metadata."""
    predicate_table.pop(name, None)
    _predicate_metadata.pop(name, None)


def compile_predicate_source(
    source: str,
    *,
    expected_name: str | None = None,
    source_path: Path | None = None,
) -> tuple[str, Predicate]:
    """Compile and validate one user-managed Predicate module."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        location = f"line {exc.lineno}, column {exc.offset}" if exc.lineno else "unknown location"
        raise ValueError(f"Predicate syntax error at {location}: {exc.msg}") from exc

    candidates: list[ast.FunctionDef] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        if len(positional) == 3 and node.args.vararg is None and node.args.kwarg is None:
            candidates.append(node)
    if expected_name is not None:
        candidates = [node for node in candidates if node.name == expected_name]
    if len(candidates) != 1:
        if expected_name:
            raise ValueError(
                f"Predicate source must define exactly one three-argument function named {expected_name}"
            )
        raise ValueError(
            "Predicate source must define exactly one three-argument function "
            "(user_input, tool_input, history)"
        )

    name = candidates[0].name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid OpsShield predicate name: {name!r}")

    filename = str(source_path or f"<agentspec-predicate:{name}>")
    namespace: dict[str, Any] = {
        "__name__": f"agentspec_user_predicate_{name}",
        "__file__": filename,
    }
    try:
        exec(compile(tree, filename, "exec"), namespace, namespace)
    except Exception as exc:
        raise ValueError(f"Predicate module failed to load: {type(exc).__name__}: {exc}") from exc
    predicate = namespace.get(name)
    if not callable(predicate):
        raise ValueError(f"Predicate function was not created: {name}")
    return name, predicate


def predicate_catalog() -> list[dict[str, str | None]]:
    """Return inspectable metadata for every registered Predicate."""
    catalog: list[dict[str, str | None]] = []
    for name, predicate in sorted(predicate_table.items()):
        module = getattr(predicate, "__module__", "") or ""
        qualified_name = getattr(predicate, "__qualname__", "") or getattr(
            predicate,
            "__name__",
            name,
        )
        try:
            signature = str(inspect.signature(predicate))
        except (TypeError, ValueError):
            signature = "(user_input, tool_input, history)"
        try:
            source = textwrap.dedent(inspect.getsource(predicate)).strip()
        except (OSError, TypeError):
            source = None
        metadata = _predicate_metadata.get(name, {})
        catalog.append(
            {
                "name": name,
                "description": inspect.getdoc(predicate) or "No description provided.",
                "module": module,
                "qualified_name": qualified_name,
                "signature": signature,
                "source": metadata.get("source") or source,
                "source_kind": metadata.get("source_kind")
                or ("builtin" if module.startswith("my_agent.") else "custom"),
                "source_path": metadata.get("source_path"),
            }
        )
    return catalog


def _action_from_context_or_input(action_name: str, tool_input: Any) -> ActionAst:
    action = current_action_ast()
    if action is not None:
        return action
    return parse_action_ast(action_name, tool_input)


def _tool_text(tool_input: Any) -> str:
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "code", "input", "script"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    return str(tool_input)


def _is_shell_assignment(word: str) -> bool:
    name, separator, _ = word.partition("=")
    return bool(
        separator
        and name
        and (name[0].isalpha() or name[0] == "_")
        and all(character.isalnum() or character == "_" for character in name)
    )


def _normalize_program(value: str | None, variables: dict[str, str] | None = None) -> str | None:
    if not value:
        return None
    variables = variables or {}
    if value.startswith("${") and value.endswith("}"):
        value = variables.get(value[2:-1], value)
    elif value.startswith("$"):
        value = variables.get(value[1:], value)
    if any(marker in value for marker in ("$", "`")):
        return None
    return PurePath(value).name.lower()


def _record_assignments(command: ShellCommand, variables: dict[str, str]) -> None:
    if command.program is not None or command.redirections or command.children:
        return
    for word in command.words:
        if not _is_shell_assignment(word):
            continue
        name, _, value = word.partition("=")
        if any(marker in value for marker in ("$", "`")):
            continue
        variables[name] = value


def _option_name(value: str) -> str:
    return value.split("=", 1)[0]


def _strip_wrapper_arguments(program: str, arguments: list[str]) -> list[str]:
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            return arguments[index + 1 :]
        if _is_shell_assignment(value):
            index += 1
            continue
        if not value.startswith("-") or value == "-":
            return arguments[index:]
        option = _option_name(value)
        index += 1
        if option in _WRAPPER_OPTIONS_WITH_VALUE and "=" not in value:
            index += 1
    return []


def _effective_program_and_args(
    command: ShellCommand,
    variables: dict[str, str] | None = None,
) -> tuple[str | None, list[str]]:
    variables = variables or {}
    words = list(command.words)
    while words and _is_shell_assignment(words[0]):
        words.pop(0)
    if not words:
        return None, []

    program = _normalize_program(words[0], variables)
    arguments = words[1:]
    for _ in range(6):
        if program not in _SHELL_WRAPPERS:
            break
        nested = _strip_wrapper_arguments(program, arguments)
        if not nested:
            break
        program = _normalize_program(nested[0], variables)
        arguments = nested[1:]
    return program, arguments


def _option_value(arguments: list[str], option: str) -> str | None:
    for index, value in enumerate(arguments):
        if value == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(option) and value != option:
            return value[len(option) :]
    return None


def _non_option_args(
    arguments: list[str],
    options_with_value: frozenset[str] = frozenset(),
) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            result.extend(arguments[index + 1 :])
            break
        if value.startswith("-") and value != "-":
            option = _option_name(value)
            index += 1
            if option in options_with_value and "=" not in value:
                index += 1
            continue
        result.append(value)
        index += 1
    return result


def _iter_shell_commands_from_script(
    script: ShellScript,
    variables: dict[str, str] | None = None,
    *,
    depth: int = 0,
) -> Iterable[tuple[str, list[str], ShellCommand]]:
    local_variables = dict(variables or {})
    for command in script.commands:
        for child in command.children:
            yield from _iter_shell_commands_from_script(
                child,
                local_variables,
                depth=depth + 1,
            )

        program, arguments = _effective_program_and_args(command, local_variables)
        if program is not None:
            yield program, arguments, command
            if depth < 3 and program in _SHELL_INTERPRETERS:
                nested_source = _option_value(arguments, "-c")
                if nested_source:
                    try:
                        nested = parse_shell_source(nested_source)
                    except ValueError:
                        nested = None
                    if nested is not None:
                        yield from _iter_shell_commands_from_script(
                            nested,
                            local_variables,
                            depth=depth + 1,
                        )
            elif depth < 3 and program == "eval" and arguments:
                try:
                    nested = parse_shell_source(" ".join(arguments))
                except ValueError:
                    nested = None
                if nested is not None:
                    yield from _iter_shell_commands_from_script(
                        nested,
                        local_variables,
                        depth=depth + 1,
                    )

        _record_assignments(command, local_variables)


def _iter_shell_commands(
    tool_input: Any,
) -> Iterable[tuple[str, list[str], ShellCommand]]:
    action = _action_from_context_or_input("exec", tool_input)
    if action.kind == "shell" and isinstance(action.tree, ShellScript):
        yield from _iter_shell_commands_from_script(action.tree)


def _path_from_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().strip("'\"")
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    if "=" in raw and not raw.startswith("/"):
        _, candidate = raw.split("=", 1)
        if candidate.startswith("/"):
            raw = candidate
    if raw == "~root":
        raw = "/root"
    elif raw.startswith("~root/"):
        raw = "/root/" + raw[len("~root/") :]
    raw = raw.rstrip(",:;)")
    if not raw.startswith("/"):
        return None
    return re.sub(r"/+", "/", raw)


def _path_matches_prefixes(path: str | None, prefixes: tuple[str, ...]) -> bool:
    if not path:
        return False
    normalized = re.sub(r"/+", "/", path)
    for prefix in prefixes:
        clean_prefix = prefix.rstrip("/") or "/"
        if normalized == clean_prefix or normalized.startswith(clean_prefix + "/"):
            return True
    return False


def _payload_path(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return str(path)
    scope = current_workspace_scope()
    if scope is None:
        return None
    return str(scope.project_path / path)


def _payload_paths(payload: dict[str, Any]) -> Iterable[str]:
    path = _payload_path(payload.get("path"))
    if path is not None:
        yield path
    edits = payload.get("edits")
    if isinstance(edits, list):
        for item in edits:
            if isinstance(item, dict):
                path = _payload_path(item.get("path"))
                if path is not None:
                    yield path


def _command_path_tokens(arguments: list[str]) -> list[str]:
    return [path for item in arguments if (path := _path_from_token(item)) is not None]


def _shell_command_writes_prefixes(
    program: str,
    arguments: list[str],
    command: ShellCommand,
    prefixes: tuple[str, ...],
) -> bool:
    for redirection in command.redirections:
        if _redirection_writes_prefixes(redirection, prefixes):
            return True
    if program not in _PROTECTED_WRITE_PROGRAMS:
        return False

    candidates: list[str] = []
    operands = _non_option_args(arguments, _WRAPPER_OPTIONS_WITH_VALUE)
    if program in {"cp", "install", "ln", "mv"}:
        for index, value in enumerate(arguments):
            if value in {"-t", "--target-directory"} and index + 1 < len(arguments):
                candidates.append(arguments[index + 1])
            elif value.startswith("--target-directory="):
                candidates.append(value.split("=", 1)[1])
        if operands:
            candidates.append(operands[-1])
    elif program == "tee":
        candidates.extend(operands)
    elif program in {"perl", "sed"}:
        if not any(value == "-i" or value.startswith("-i") for value in arguments):
            return False
        candidates.extend(operands)
    else:
        candidates.extend(arguments)

    return any(_path_matches_prefixes(_path_from_token(item), prefixes) for item in candidates)


def _redirection_writes_prefixes(
    redirection: ShellRedirection,
    prefixes: tuple[str, ...],
) -> bool:
    return (
        redirection.operator in _SHELL_WRITE_REDIRECTS
        and _path_matches_prefixes(_path_from_token(redirection.target), prefixes)
    )


def _service_name(value: str) -> str:
    name = PurePath(value).name.lower()
    for suffix in (".service", ".socket", ".target", ".timer"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _systemctl_invocation(arguments: list[str]) -> tuple[str | None, list[str]]:
    values = _non_option_args(
        arguments,
        frozenset({"--host", "--machine", "--property", "--root", "--type", "-H", "-M", "-p", "-t"}),
    )
    if not values:
        return None, []
    return values[0].lower(), values[1:]


def _service_invocation(arguments: list[str]) -> tuple[str | None, list[str]]:
    values = _non_option_args(arguments)
    if len(values) < 2:
        return None, []
    return values[1].lower(), [values[0], *values[2:]]


def _rpm_is_mutating(arguments: list[str]) -> bool:
    for value in arguments:
        option = _option_name(value)
        if option in _RPM_MUTATING_LONG_OPTIONS:
            return True
        if value.startswith("-") and not value.startswith("--"):
            flags = value.lstrip("-")
            if any(flag in flags for flag in ("e", "F", "i", "U")):
                return True
    return False


def _package_manager_is_mutating(program: str, arguments: list[str]) -> bool:
    if program == "rpm":
        return _rpm_is_mutating(arguments)
    if program == "dpkg":
        return any(
            value in {"--configure", "--install", "--purge", "--remove", "-i", "-P", "-r"}
            for value in arguments
        )
    values = [value.lower() for value in _non_option_args(arguments)]
    if not values:
        return False
    if values[0] == "history" and len(values) > 1:
        return values[1] in {"rollback", "undo"}
    return values[0] in _PACKAGE_MANAGER_VERBS


def _security_control_is_mutating(program: str, arguments: list[str]) -> bool:
    lowered = [value.lower() for value in arguments]
    if program == "firewall-cmd":
        readonly_prefixes = ("--get", "--help", "--info", "--list", "--query", "--state", "--version")
        return any(
            value.startswith(("--add", "--change", "--complete-reload", "--delete", "--lockdown", "--new", "--panic", "--reload", "--remove", "--runtime-to-permanent", "--set"))
            for value in lowered
            if not value.startswith(readonly_prefixes)
        )
    if program in {"iptables", "iptables-restore", "ip6tables", "ip6tables-restore"}:
        long_mutating = {"--append", "--delete", "--delete-chain", "--flush", "--insert", "--new-chain", "--policy", "--replace", "--zero"}
        short_mutating = {"-A", "-D", "-F", "-I", "-N", "-P", "-R", "-X", "-Z"}
        return program.endswith("restore") or any(
            value in short_mutating or value.lower() in long_mutating
            for value in arguments
        )
    if program == "nft":
        values = _non_option_args(lowered)
        return bool(values and values[0] in {"add", "delete", "destroy", "flush", "insert", "replace", "reset"})
    if program == "auditctl":
        return any(value in {"-a", "-A", "-b", "-d", "-D", "-e", "-f", "-R"} for value in arguments)
    if program == "kysec":
        return any(value in {"disable", "enable", "enforcing", "off", "on", "permissive", "set", "setstatus"} for value in lowered)
    return True


def _network_command_is_mutating(program: str, arguments: list[str]) -> bool:
    values = [value.lower() for value in _non_option_args(arguments)]
    lowered = [value.lower() for value in arguments]
    if program == "ip":
        return len(values) >= 2 and values[0] in {"addr", "address", "link", "neigh", "netns", "route", "rule", "tunnel"} and values[1] in {"add", "change", "delete", "del", "flush", "replace", "set"}
    if program == "nmcli":
        return len(values) >= 2 and values[0] in {"con", "connection", "dev", "device", "networking"} and values[1] in {"add", "delete", "down", "edit", "import", "load", "modify", "reload", "up"}
    if program == "ifconfig":
        return any(value in {"add", "del", "delete", "down", "mtu", "netmask", "up"} for value in values)
    if program == "route":
        return bool(values and values[0] in {"add", "del", "delete"})
    if program == "hostnamectl":
        return bool(values and values[0].startswith("set-"))
    if program == "resolvectl":
        return bool(values and values[0] in {"default-route", "dns", "dnssec", "dnsovertls", "domain", "llmnr", "mdns", "revert"})
    if program == "ethtool":
        long_mutating = {"--change", "--coalesce", "--features", "--offload", "--pause", "--set-ring"}
        short_mutating = {"-C", "-G", "-K", "-L", "-s"}
        return any(
            value in short_mutating or value.lower() in long_mutating
            for value in arguments
        )
    if program == "tc":
        return bool(values and values[0] in {"class", "filter", "qdisc"} and len(values) > 1 and values[1] in {"add", "change", "delete", "del", "replace"})
    return False


def _sysctl_is_mutating(arguments: list[str]) -> bool:
    for value in arguments:
        if value in {"-w", "--write"}:
            return True
        if "=" in value and not value.startswith("-"):
            return True
    return False


def is_destructive(user_input: Any, tool_input: Any, intermediate_steps: list[Any]) -> bool:
    """通过当前 action AST 检测破坏性文件系统操作和高权限命令。"""
    del user_input, intermediate_steps
    action = current_action_ast()
    if action is not None:
        return action_ast_is_destructive(action)
    if isinstance(tool_input, str):
        return action_ast_is_destructive(
            parse_action_ast("exec", tool_input)
        ) or action_ast_is_destructive(parse_action_ast("PythonREPL", tool_input))
    return action_ast_is_destructive(parse_action_ast("", tool_input))


def is_destructive_file_edit(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """检测使用空内容覆盖已有文件，或使用空文本替换大量文件内容的操作。"""
    del user_input, intermediate_steps
    action = current_action_ast()
    if action is not None and action.kind == "structured" and isinstance(action.tree, dict):
        payload = action.tree
    elif isinstance(tool_input, dict):
        payload = tool_input
    else:
        return False

    path_value = payload.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return False

    if "content" in payload:
        content = payload.get("content")
        if not isinstance(content, str) or content.strip():
            return False
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            scope = current_workspace_scope()
            if scope is None:
                return True
            path = scope.project_path / path
        return path.is_file()

    old_text = payload.get("old_text")
    new_text = payload.get("new_text")
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        return False
    if new_text.strip() or not old_text:
        return False
    return (
        len(old_text) >= _LARGE_REPLACEMENT_CHARS
        or len(old_text.splitlines()) >= _LARGE_REPLACEMENT_LINES
    )


def is_protected_service_change(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect attempts to stop, mask, disable, or isolate critical OS services."""
    del user_input, intermediate_steps
    for program, arguments, _command in _iter_shell_commands(tool_input):
        if program == "systemctl":
            verb, units = _systemctl_invocation(arguments)
        elif program == "service":
            verb, units = _service_invocation(arguments)
        else:
            continue
        if verb not in _PROTECTED_SERVICE_VERBS:
            continue
        service_names = {_service_name(unit) for unit in units}
        if service_names & _PROTECTED_SERVICES:
            return True
        if verb == "isolate" and service_names & _PROTECTED_TARGETS:
            return True
    return False


def is_service_state_change(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect service lifecycle changes such as systemctl restart/enable/disable."""
    del user_input, intermediate_steps
    for program, arguments, _command in _iter_shell_commands(tool_input):
        if program == "systemctl":
            verb, _units = _systemctl_invocation(arguments)
            if verb in _SERVICE_MUTATING_VERBS:
                return True
        elif program == "service":
            verb, _units = _service_invocation(arguments)
            if verb in {"reload", "restart", "start", "status", "stop"} and verb != "status":
                return True
        elif program in {"chkconfig", "rc-update", "update-rc.d"}:
            values = [value.lower() for value in _non_option_args(arguments)]
            if any(value in {"add", "defaults", "delete", "del", "disable", "enable", "off", "on", "remove"} for value in values):
                return True
    return False


def is_package_manager_change(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect package install/remove/update operations on Kylin/Linux hosts."""
    del user_input, intermediate_steps
    return any(
        program in _PACKAGE_MANAGERS and _package_manager_is_mutating(program, arguments)
        for program, arguments, _command in _iter_shell_commands(tool_input)
    )


def is_identity_or_auth_change(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect account, group, PAM, sudoers, SSH auth, and password policy changes."""
    del user_input, intermediate_steps
    action = _action_from_context_or_input("exec", tool_input)
    if action.kind == "structured" and isinstance(action.tree, dict):
        if any(_path_matches_prefixes(path, _AUTH_CONFIG_PATHS) for path in _payload_paths(action.tree)):
            return True
    elif isinstance(tool_input, dict):
        if any(_path_matches_prefixes(path, _AUTH_CONFIG_PATHS) for path in _payload_paths(tool_input)):
            return True

    for program, arguments, command in _iter_shell_commands(tool_input):
        if program in _IDENTITY_AUTH_PROGRAMS:
            return True
        if _shell_command_writes_prefixes(program, arguments, command, _AUTH_CONFIG_PATHS):
            return True
    return False


def is_security_control_change(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect KYSEC/SELinux/audit/firewall security control changes."""
    del user_input, intermediate_steps
    for program, arguments, _command in _iter_shell_commands(tool_input):
        if program in _SECURITY_CONTROL_PROGRAMS and _security_control_is_mutating(
            program,
            arguments,
        ):
            return True
    return False


def is_kernel_or_boot_change(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect kernel module, sysctl, bootloader, and /proc/sys changes."""
    del user_input, intermediate_steps
    for program, arguments, command in _iter_shell_commands(tool_input):
        if program in _KERNEL_BOOT_PROGRAMS:
            if program == "sysctl":
                if _sysctl_is_mutating(arguments):
                    return True
            else:
                return True
        if _shell_command_writes_prefixes(
            program,
            arguments,
            command,
            ("/boot", "/proc/sys", "/sys/module"),
        ):
            return True
    return False


def is_network_config_change(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect network interface, route, DNS, hostname, and traffic-control changes."""
    del user_input, intermediate_steps
    return any(
        program in _NETWORK_PROGRAMS and _network_command_is_mutating(program, arguments)
        for program, arguments, _command in _iter_shell_commands(tool_input)
    )


def is_protected_path_write(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect writes or permission changes under critical Linux system paths."""
    del user_input, intermediate_steps
    action = _action_from_context_or_input("exec", tool_input)
    if action.kind == "structured" and isinstance(action.tree, dict):
        if any(
            _path_matches_prefixes(path, _PROTECTED_WRITE_PATHS)
            for path in _payload_paths(action.tree)
        ):
            return True
    elif isinstance(tool_input, dict):
        if any(
            _path_matches_prefixes(path, _PROTECTED_WRITE_PATHS)
            for path in _payload_paths(tool_input)
        ):
            return True

    for program, arguments, command in _iter_shell_commands(tool_input):
        if _shell_command_writes_prefixes(program, arguments, command, _PROTECTED_WRITE_PATHS):
            return True
    return False


def is_sensitive_data_read(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect reads or copies of shadow files, sudoers, host keys, and root SSH material."""
    del user_input, intermediate_steps
    action = _action_from_context_or_input("read_file", tool_input)
    if action.kind == "structured" and isinstance(action.tree, dict):
        if any(
            _path_matches_prefixes(path, _SENSITIVE_READ_PATHS)
            for path in _payload_paths(action.tree)
        ):
            return True
    elif isinstance(tool_input, dict):
        if any(
            _path_matches_prefixes(path, _SENSITIVE_READ_PATHS)
            for path in _payload_paths(tool_input)
        ):
            return True

    read_programs = _SENSITIVE_READ_PROGRAMS | {"cp", "rsync", "scp"}
    for program, arguments, _command in _iter_shell_commands(tool_input):
        if program in read_programs and any(
            _path_matches_prefixes(path, _SENSITIVE_READ_PATHS)
            for path in _command_path_tokens(arguments)
        ):
            return True
    return False


def is_remote_code_execution(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect curl/wget/fetch output being executed by a shell or interpreter."""
    del user_input, intermediate_steps
    text = _tool_text(tool_input)
    pipe_pattern = (
        r"\b(?:curl|fetch|wget)\b[^\n;|&]*\|\s*"
        r"(?:(?:sudo|doas|pkexec|env|timeout)\s+)*"
        r"(?:bash|dash|ksh|node|perl|php|python(?:2|3)?|ruby|sh|zsh)\b"
    )
    if re.search(pipe_pattern, text, flags=re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:bash|dash|ksh|sh|zsh)\b[^\n;]*<\s*\(\s*(?:curl|fetch|wget)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    for program, arguments, _command in _iter_shell_commands(tool_input):
        if program in _REMOTE_EXEC_INTERPRETERS:
            source = _option_value(arguments, "-c")
            if source and re.search(r"\b(?:curl|fetch|wget)\b", source, flags=re.IGNORECASE):
                return True
        if program in _SHELL_INTERPRETERS and any(
            re.search(r"<\(\s*(?:curl|fetch|wget)\b", item, flags=re.IGNORECASE)
            for item in arguments
        ):
            return True
    return False


def is_unbounded_process_kill(
    user_input: Any,
    tool_input: Any,
    intermediate_steps: list[Any],
) -> bool:
    """Detect broad kill patterns that can terminate many or critical OS processes."""
    del user_input, intermediate_steps
    broad_patterns = {"*", ".*", ".", "^.*$", "all"}
    for program, arguments, _command in _iter_shell_commands(tool_input):
        lowered = [value.lower() for value in arguments]
        if program == "kill":
            if any(value in {"-1", "0", "1"} for value in lowered):
                return True
        elif program in {"killall", "pkill"}:
            operands = [value.lower() for value in _non_option_args(arguments)]
            if any(value in broad_patterns for value in operands):
                return True
            if any(value in {"-u", "--user"} for value in lowered) and any(
                value in {"0", "root"} for value in lowered
            ):
                return True
        elif program == "fuser":
            operands = _non_option_args(arguments)
            if "-k" in lowered and any(value == "/" for value in operands):
                return True
    return False


register_predicate("is_destructive", is_destructive)
register_predicate("is_destructive_file_edit", is_destructive_file_edit)
register_predicate("is_identity_or_auth_change", is_identity_or_auth_change)
register_predicate("is_kernel_or_boot_change", is_kernel_or_boot_change)
register_predicate("is_network_config_change", is_network_config_change)
register_predicate("is_package_manager_change", is_package_manager_change)
register_predicate("is_protected_path_write", is_protected_path_write)
register_predicate("is_protected_service_change", is_protected_service_change)
register_predicate("is_remote_code_execution", is_remote_code_execution)
register_predicate("is_security_control_change", is_security_control_change)
register_predicate("is_sensitive_data_read", is_sensitive_data_read)
register_predicate("is_service_state_change", is_service_state_change)
register_predicate("is_unbounded_process_kill", is_unbounded_process_kill)
