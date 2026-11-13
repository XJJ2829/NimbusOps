"""Sandbox backends for local process execution.

To add a new backend, implement a function with the signature:
    _wrap_<name>(
        command: str,
        workspace: str,
        cwd: str,
        extra_read_roots: list[Path],
        extra_write_roots: list[Path],
    ) -> str
and register it in _BACKENDS below.
"""

import json
import os
import platform
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from pathlib import Path

from my_agent.config.paths import get_config_path


def _media_path() -> Path:
    """Return the media path without creating directories during wrapping."""
    return (get_config_path().expanduser().parent / "media").resolve()


@dataclass(frozen=True, slots=True)
class SandboxedProcess:
    """Prepared argv/cwd/env tuple for launching a local process inside OS sandbox."""

    argv: list[str]
    cwd: str
    env: dict[str, str]
    backend: str | None = None


def _normalize_roots(paths: Sequence[str | Path] | None) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in paths or ():
        try:
            path = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _bwrap(
    command: str,
    workspace: str,
    cwd: str,
    extra_read_roots: Sequence[str | Path] | None = None,
    extra_write_roots: Sequence[str | Path] | None = None,
) -> str:
    """Wrap command in a bubblewrap sandbox (requires bwrap in container).

    Only the workspace is bind-mounted read-write; its parent dir (which holds
    config.json) is hidden behind a fresh tmpfs.  The media directory is
    bind-mounted read-only so exec commands can read uploaded attachments.
    """
    executable = shutil.which("bwrap")
    if executable is None:
        raise RuntimeError("Bubblewrap backend requires the 'bwrap' executable")

    ws = Path(workspace).resolve()
    media = _media_path()

    cwd_path = Path(cwd).expanduser().resolve(strict=False)
    sandbox_cwd = str(ws)
    for root in [ws, *_normalize_roots(extra_read_roots), *_normalize_roots(extra_write_roots)]:
        try:
            cwd_path.relative_to(root)
        except ValueError:
            continue
        sandbox_cwd = str(cwd_path)
        break

    required  = ["/usr"]
    optional  = ["/bin", "/lib", "/lib64", "/etc/alternatives",
                 "/etc/ssl/certs", "/etc/resolv.conf", "/etc/ld.so.cache"]

    args = [executable, "--new-session", "--die-with-parent"]
    for p in required: args += ["--ro-bind",     p, p]
    for p in optional: args += ["--ro-bind-try", p, p]
    args += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--tmpfs", str(ws.parent),        # mask config dir
        "--dir", str(ws),                 # recreate workspace mount point
        "--bind", str(ws), str(ws),
        "--ro-bind-try", str(media), str(media),  # read-only access to media
    ]
    for root in _normalize_roots(extra_read_roots):
        if root == ws or _is_child(root, ws):
            continue
        args += ["--dir", str(root), "--ro-bind-try", str(root), str(root)]
    for root in _normalize_roots(extra_write_roots):
        if root == ws or _is_child(root, ws):
            continue
        args += ["--dir", str(root), "--bind-try", str(root), str(root)]
    args += ["--chdir", sandbox_cwd, "--", "sh", "-c", command]
    return shlex.join(args)


def _sbpl_string(value: str | Path) -> str:
    """Encode one path as a quoted Seatbelt profile string."""
    return json.dumps(str(Path(value).expanduser().resolve()), ensure_ascii=False)


def _is_child(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _metadata_roots(paths: Sequence[str | Path]) -> list[Path]:
    """Return literal path components needed to resolve authorized roots."""
    roots: list[Path] = []
    seen: set[str] = set()
    for path in _normalize_roots(paths):
        chain = [*reversed(path.parents), path]
        for item in chain:
            key = str(item)
            if key in seen:
                continue
            seen.add(key)
            roots.append(item)
    return roots


def _seatbelt_profile(
    workspace: str,
    extra_read_roots: Sequence[str | Path] | None = None,
    extra_write_roots: Sequence[str | Path] | None = None,
) -> str:
    """Build a macOS Seatbelt profile matching the bwrap workspace boundary."""
    ws = Path(workspace).expanduser().resolve()
    media = _media_path()
    home = Path.home().resolve()
    temp_dir = Path(tempfile.gettempdir()).resolve()
    read_roots = _normalize_roots(extra_read_roots)
    write_roots = _normalize_roots(extra_write_roots)
    authorized_roots = [ws, media, temp_dir, *read_roots, *write_roots]

    # Seatbelt uses the most specific matching path rule. User files are hidden
    # by default while the selected workspace, uploaded media, and temporary
    # directories receive explicit access.
    lines = [
        "(version 1)",
        "(allow default)",
        f"(deny file-read* (subpath {_sbpl_string(home)}))",
        '(deny file-read* (subpath "/Volumes"))',
        '(deny file-read* (subpath "/Network"))',
    ]
    for root in _metadata_roots(authorized_roots):
        lines.append(f"(allow file-read-metadata (literal {_sbpl_string(root)}))")
    lines.extend([
        f"(allow file-read* (subpath {_sbpl_string(ws)}))",
        f"(allow file-read* (subpath {_sbpl_string(media)}))",
        f"(allow file-read* (subpath {_sbpl_string(temp_dir)}))",
        "(deny file-write*)",
        f"(allow file-write* (subpath {_sbpl_string(ws)}))",
        f"(allow file-write* (subpath {_sbpl_string(temp_dir)}))",
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-write* (literal "/dev/tty"))',
        '(allow file-write* (literal "/dev/stdout"))',
        '(allow file-write* (literal "/dev/stderr"))',
    ])
    for root in read_roots:
        if root == ws or _is_child(root, ws):
            continue
        lines.append(f"(allow file-read* (subpath {_sbpl_string(root)}))")
    for root in write_roots:
        if root == ws or _is_child(root, ws):
            continue
        lines.append(f"(allow file-read* (subpath {_sbpl_string(root)}))")
        lines.append(f"(allow file-write* (subpath {_sbpl_string(root)}))")
    return "\n".join(lines)


def _sandbox_exec(
    command: str,
    workspace: str,
    cwd: str,
    extra_read_roots: Sequence[str | Path] | None = None,
    extra_write_roots: Sequence[str | Path] | None = None,
) -> str:
    """Wrap a command with the macOS sandbox-exec (Seatbelt) backend."""
    del cwd  # The outer ExecTool sets the workspace-safe cwd.
    executable = shutil.which("sandbox-exec")
    if executable is None:
        raise RuntimeError("macOS sandbox backend requires '/usr/bin/sandbox-exec'")
    args = [
        executable,
        "-p",
        _seatbelt_profile(
            workspace,
            extra_read_roots=extra_read_roots,
            extra_write_roots=extra_write_roots,
        ),
        "/bin/sh",
        "-c",
        command,
    ]
    return shlex.join(args)


_BACKENDS = {
    "bwrap": _bwrap,
    "sandbox-exec": _sandbox_exec,
    "seatbelt": _sandbox_exec,
}

def resolve_backend(sandbox: str) -> str:
    """Resolve aliases and the platform-aware ``auto`` backend."""
    requested = sandbox.strip().lower()
    if requested in {"sandbox_exec", "macos", "darwin"}:
        requested = "sandbox-exec"
    if requested != "auto":
        return requested

    system = platform.system()
    if system == "Darwin":
        return "sandbox-exec"
    if system == "Linux":
        return "bwrap"
    raise RuntimeError(f"No automatic sandbox backend is available for {system}")


def wrap_command(
    sandbox: str,
    command: str,
    workspace: str,
    cwd: str,
    *,
    extra_read_roots: Sequence[str | Path] | None = None,
    extra_write_roots: Sequence[str | Path] | None = None,
) -> str:
    """Wrap *command* using the named sandbox backend."""
    resolved = resolve_backend(sandbox)
    if backend := _BACKENDS.get(resolved):
        return backend(command, workspace, cwd, extra_read_roots, extra_write_roots)
    available = ["auto", *_BACKENDS]
    raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: {available}")


def prepare_sandboxed_argv(
    *,
    sandbox: str,
    argv: Sequence[str | os.PathLike[str]],
    workspace: str | Path,
    cwd: str | Path,
    env: Mapping[str, str] | None = None,
    extra_read_roots: Sequence[str | Path] | None = None,
    extra_write_roots: Sequence[str | Path] | None = None,
    force: bool = True,
) -> SandboxedProcess:
    """Return argv/cwd/env for launching a local process through the OS sandbox.

    This is the single process-entry API used by non-shell local integrations
    such as CLI Apps and stdio MCP. Their public protocols remain argv/stdio;
    only the child process is wrapped before launch.
    """
    normalized_argv = [os.fspath(item) for item in argv]
    if not normalized_argv:
        raise ValueError("sandboxed process argv must not be empty")
    if any("\0" in item for item in normalized_argv):
        raise ValueError("sandboxed process argv contains NUL byte")

    requested = (sandbox or "").strip()
    if force and not requested:
        requested = "auto"
    next_env = dict(env or {})
    if not requested:
        return SandboxedProcess(
            argv=normalized_argv,
            cwd=str(Path(cwd).expanduser().resolve(strict=False)),
            env=next_env,
            backend=None,
        )
    if sys.platform == "win32":
        raise RuntimeError("OS sandbox for local integrations is not supported on Windows")

    resolved = resolve_backend(requested)
    command = shlex.join(normalized_argv)
    wrapped = wrap_command(
        requested,
        command,
        str(workspace),
        str(cwd),
        extra_read_roots=extra_read_roots,
        extra_write_roots=extra_write_roots,
    )
    shell = shutil.which("sh") or "/bin/sh"
    workspace_cwd = str(Path(workspace).expanduser().resolve(strict=False))
    process_cwd = str(Path(cwd).expanduser().resolve(strict=False))
    next_env["NANOBOT_WORKSPACE_SANDBOX_ENFORCED"] = "1"
    next_env["NANOBOT_WORKSPACE_SANDBOX_PROVIDER"] = resolved.replace("-", "_")
    next_env["NIMBUSOPS_OS_SANDBOX_BACKEND"] = resolved
    return SandboxedProcess(
        argv=[shell, "-c", wrapped],
        cwd=process_cwd if resolved in {"sandbox-exec", "seatbelt"} else workspace_cwd,
        env=next_env,
        backend=resolved,
    )
