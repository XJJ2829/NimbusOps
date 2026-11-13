"""MCP client: connects to MCP servers and wraps their tools as native nanobot tools."""

import asyncio
import os
import re
import signal
import shutil
import urllib.parse
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TextIO
from weakref import WeakKeyDictionary

import httpx
from loguru import logger

from my_agent.agent.tools.base import Tool
from my_agent.agent.tools.registry import ToolRegistry
from my_agent.agent.tools.sandbox import prepare_sandboxed_argv
from my_agent.bus.events import (
    INBOUND_META_RUNTIME_CONTROL,
    RUNTIME_CONTROL_ACK,
    RUNTIME_CONTROL_MCP_RELOAD,
    InboundMessage,
)
from my_agent.security.auth import effective_exec_sandbox
from my_agent.security.runtime_identity import (
    apply_identity_environment,
    subprocess_identity_kwargs,
)
from my_agent.security.workspace_access import current_tool_workspace
from my_agent.security.workspace_policy import WORKSPACE_BOUNDARY_NOTE, is_path_within

# Transient connection errors that warrant a single retry.
# These typically happen when an MCP server restarts or a network
# connection is interrupted between calls.
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
))

_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# Characters allowed in tool names by model providers (Anthropic, OpenAI, etc.).
# Replace anything outside [a-zA-Z0-9_-] with underscore and collapse runs.
_SANITIZE_RE = re.compile(r"_+")
_RELOAD_LOCKS: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()
_ReconnectCallback = Callable[[str, str, Tool], Awaitable[Tool | None]]
_SANDBOX_DENIAL_MARKERS: tuple[str, ...] = (
    "sandbox_apply",
    "operation not permitted",
    "permission denied",
    "read-only file system",
    "not permitted",
)
_MCP_STDIO_ERRLOG_LIMIT = 8192


@dataclass(frozen=True, slots=True)
class _MCPStdioLaunch:
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    sandbox_backend: str | None
    workspace: str
    sandbox_requested: str


class _MCPStderrCapture:
    """Small TextIO-like stderr sink that keeps a tail for failure attribution."""

    def __init__(self, server_name: str, *, limit: int = _MCP_STDIO_ERRLOG_LIMIT) -> None:
        self.server_name = server_name
        self.limit = limit
        self._tail = ""

    @property
    def text(self) -> str:
        return self._tail

    def write(self, text: str) -> int:
        if text:
            self._tail = (self._tail + text)[-self.limit :]
        return len(text)

    def flush(self) -> None:
        return None


def _sanitize_name(name: str) -> str:
    """Sanitize an MCP-derived name for model API compatibility."""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _is_transient(exc: BaseException) -> bool:
    """Check if an exception looks like a transient connection error."""
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


def _is_session_terminated(exc: BaseException) -> bool:
    """Return True when the MCP SDK reports a dead client session."""
    messages = [str(exc)]
    error = getattr(exc, "error", None)
    if error is not None:
        messages.append(str(getattr(error, "message", "")))
    return any(
        marker in message.lower()
        for marker in ("session terminated", "connection closed")
        for message in messages
    )


def _stdio_sandbox_for_server(cfg: Any, inherited_sandbox: str) -> str:
    configured = getattr(cfg, "sandbox", None)
    if configured is None:
        return inherited_sandbox
    value = str(configured).strip()
    if value or getattr(cfg, "source", "") != "mcp-package":
        return value
    # Older managed package imports wrote sandbox="" as their default. Treat
    # that legacy value as "inherit" so package MCP servers cannot bypass the
    # same exec/CLI sandbox merely because they came from an archive.
    return inherited_sandbox


def _is_path_like_command(command: str) -> bool:
    return os.sep in command or bool(os.altsep and os.altsep in command)


def _python_venv_root_for_command(command: str | Path) -> Path | None:
    path = Path(command).expanduser()
    candidates = [path]
    resolved = path.resolve(strict=False)
    if resolved != path:
        candidates.append(resolved)
    for candidate in candidates:
        for parent in candidate.parents:
            marker = parent / "pyvenv.cfg"
            try:
                if marker.is_file():
                    return parent.resolve(strict=False)
            except OSError:
                continue
    return None


def _mcp_package_dir(cfg: Any) -> Path | None:
    if getattr(cfg, "source", "") != "mcp-package" and not getattr(cfg, "managed", False):
        return None
    raw = str(getattr(cfg, "package_dir", "") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def _mcp_stdio_failure_reason(exc: BaseException, stderr_text: str = "") -> str:
    text = "\n".join(part for part in (str(exc), stderr_text) if part).lower()
    if "sandbox_apply" in text:
        return "sandbox_launch_failed: OS sandbox backend rejected the MCP stdio process"
    if any(marker in text for marker in _SANDBOX_DENIAL_MARKERS):
        return "sandbox_denied_likely: MCP stdio process hit an OS sandbox or permission boundary"
    if any(
        marker in text
        for marker in (
            "parse error",
            "invalid json",
            "unexpected token",
            "jsonrpc",
            "content-length",
        )
    ):
        return "stdio_protocol_error: MCP server wrote non-protocol output to stdout"
    if "working directory is outside" in text or ("workspace" in text and "outside" in text):
        return "workspace_scope_rejected: MCP stdio cwd is outside the active workspace policy"
    return f"mcp_connection_failed: {type(exc).__name__}"


def _prepare_stdio_launch(
    *,
    name: str,
    cfg: Any,
    inherited_sandbox: str,
    workspace: str | Path | None,
    runtime_identity: Any | None,
) -> _MCPStdioLaunch:
    from mcp.client.stdio import get_default_environment

    command, args, env = _normalize_windows_stdio_command(
        cfg.command,
        cfg.args,
        cfg.env or None,
    )
    sandbox = effective_exec_sandbox(_stdio_sandbox_for_server(cfg, inherited_sandbox))
    access = current_tool_workspace(
        workspace or cfg.cwd or os.getcwd(),
        sandbox_restricts_workspace=bool(sandbox),
    )
    workspace_root = (
        access.project_path
        if access.project_path is not None
        else Path(workspace or cfg.cwd or os.getcwd()).expanduser().resolve(strict=False)
    )
    stdio_workspace = Path(workspace_root).expanduser().resolve(strict=False)
    stdio_cwd = Path(cfg.cwd or stdio_workspace).expanduser().resolve(strict=False)
    package_dir = _mcp_package_dir(cfg)
    cwd_is_managed_package = (
        package_dir is not None and is_path_within(stdio_cwd, package_dir)
    )
    if (
        access.restrict_to_workspace
        and not is_path_within(stdio_cwd, stdio_workspace)
        and not cwd_is_managed_package
    ):
        raise PermissionError(
            f"MCP server {name!r} working directory is outside the active workspace"
            + WORKSPACE_BOUNDARY_NOTE
        )

    resolved_command = shutil.which(command, path=(env or {}).get("PATH")) or command
    read_roots: list[Path] = []
    if _is_path_like_command(resolved_command):
        command_path = Path(resolved_command).expanduser()
        read_roots.append(command_path.parent.resolve(strict=False))
        real_command_path = command_path.resolve(strict=False)
        if real_command_path != command_path:
            read_roots.append(real_command_path.parent)
        venv_root = _python_venv_root_for_command(command_path)
        if venv_root is not None:
            read_roots.append(venv_root)

    if package_dir is not None:
        read_roots.append(package_dir)
    if cwd_is_managed_package:
        read_roots.append(stdio_cwd)
        write_roots: list[Path] = []
    else:
        write_roots = [stdio_cwd]

    base_env = get_default_environment()
    base_env.update(env or {})
    sandboxed = prepare_sandboxed_argv(
        sandbox=sandbox,
        argv=[command, *args],
        workspace=stdio_workspace,
        cwd=stdio_cwd,
        env=apply_identity_environment(base_env, runtime_identity),
        extra_read_roots=read_roots,
        extra_write_roots=write_roots,
        force=False,
    )
    return _MCPStdioLaunch(
        command=sandboxed.argv[0],
        args=sandboxed.argv[1:],
        env=sandboxed.env,
        cwd=sandboxed.cwd,
        sandbox_backend=sandboxed.backend,
        workspace=str(stdio_workspace),
        sandbox_requested=sandbox,
    )


async def _probe_http_url(url: str, timeout: float = 3.0) -> bool:
    """Quick TCP probe to check if an HTTP MCP server is reachable.

    Avoids entering ``streamable_http_client`` / ``sse_client`` when the port is
    closed — those transports use anyio task groups whose cleanup can raise
    ``RuntimeError`` / ``ExceptionGroup`` that escape the caller's try/except
    and crash the event loop.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if not port:
        port = 443 if parsed.scheme == "https" else 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _windows_command_basename(command: str) -> str:
    """Return the lowercase basename for a Windows command or path."""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Wrap Windows shell launchers so MCP stdio servers start reliably."""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


async def _terminate_asyncio_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2.0)
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        process.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2.0)


@asynccontextmanager
async def _stdio_client_from_launch(
    launch: _MCPStdioLaunch,
    *,
    runtime_identity: Any | None,
    errlog: TextIO,
    encoding: str = "utf-8",
    encoding_error_handler: str = "strict",
):
    import anyio
    import mcp.types as types
    from mcp.shared.message import SessionMessage

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
    process: asyncio.subprocess.Process | None = None

    try:
        process = await asyncio.create_subprocess_exec(
            launch.command,
            *launch.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=launch.cwd,
            env=launch.env,
            start_new_session=(os.name == "posix"),
            **subprocess_identity_kwargs(runtime_identity),
        )
    except Exception:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()
        raise

    async def stdout_reader() -> None:
        assert process is not None and process.stdout is not None
        try:
            async with read_stream_writer:
                buffer = ""
                while True:
                    chunk = await process.stdout.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode(encoding, errors=encoding_error_handler)
                    lines = (buffer + text).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            logger.exception("Failed to parse JSONRPC message from MCP stdio server")
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        assert process is not None and process.stdin is not None
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    )
                    process.stdin.write(
                        (payload + "\n").encode(
                            encoding=encoding,
                            errors=encoding_error_handler,
                        )
                    )
                    await process.stdin.drain()
        except (anyio.ClosedResourceError, BrokenPipeError, ConnectionResetError):
            await anyio.lowlevel.checkpoint()

    async def stderr_reader() -> None:
        assert process is not None and process.stderr is not None
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode(encoding, errors="replace")
            errlog.write(text)
            errlog.flush()

    stdout_task = asyncio.create_task(stdout_reader())
    stdin_task = asyncio.create_task(stdin_writer())
    stderr_task = asyncio.create_task(stderr_reader())

    try:
        yield read_stream, write_stream
    finally:
        if process.stdin is not None:
            with suppress(Exception):
                process.stdin.close()
                await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            await _terminate_asyncio_process_tree(process)
        for task in (stdin_task, stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdin_task, stdout_task, stderr_task, return_exceptions=True)
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize only nullable JSON Schema patterns for tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class _MCPWrapperBase(Tool):
    """Common reconnect handling for wrappers bound to one MCP server session."""

    _plugin_discoverable = False

    def _set_mcp_connection(self, session: Any, server_name: str) -> None:
        self._session = session
        self._server_name = server_name
        self._reconnect: _ReconnectCallback | None = None

    def set_reconnect_handler(self, reconnect: _ReconnectCallback) -> None:
        self._reconnect = reconnect

    async def _refresh_session_after_termination(
        self,
        exc: BaseException,
        already_refreshed: bool,
        capability_kind: str,
    ) -> bool:
        if already_refreshed or not _is_session_terminated(exc) or self._reconnect is None:
            return False
        logger.warning(
            "MCP {} '{}' session terminated; reconnecting server '{}' before retry",
            capability_kind,
            self._name,
            self._server_name,
        )
        refreshed_tool = await self._reconnect(self._server_name, self._name, self)
        refreshed_session = getattr(refreshed_tool, "_session", None)
        if refreshed_session is None:
            logger.warning(
                "MCP {} '{}' could not refresh session for server '{}'",
                capability_kind,
                self._name,
                self._server_name,
            )
            return False
        self._session = refreshed_session
        return True


class MCPToolWrapper(_MCPWrapperBase):
    """Wraps a single MCP server tool as a nanobot Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._original_name = tool_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        retried_transient = False
        refreshed_session = False
        while True:
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(self._original_name, arguments=kwargs),
                    timeout=self._tool_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP tool '{}' timed out after {}s", self._name, self._tool_timeout
                )
                return f"(MCP tool call timed out after {self._tool_timeout}s)"
            except asyncio.CancelledError:
                # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
                # Re-raise only if our task was externally cancelled (e.g. /stop).
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
                return "(MCP tool call was cancelled)"
            except Exception as exc:
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "tool",
                ):
                    refreshed_session = True
                    continue
                if _is_transient(exc):
                    if not retried_transient:
                        retried_transient = True
                        logger.warning(
                            "MCP tool '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)  # Brief backoff before retry
                        continue
                    # Second transient failure — give up with retry-specific message
                    logger.exception(
                        "MCP tool '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP tool call failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP tool '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP tool call failed: {type(exc).__name__})"
            else:
                # Success — extract result
                parts = []
                for block in result.content:
                    if isinstance(block, types.TextContent):
                        parts.append(block.text)
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP tool call failed)"  # Unreachable, but satisfies type checkers


class MCPResourceWrapper(_MCPWrapperBase):
    """Wraps an MCP resource URI as a read-only nanobot Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, resource_def, resource_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._uri = resource_def.uri
        self._name = _sanitize_name(f"mcp_{server_name}_resource_{resource_def.name}")
        desc = resource_def.description or resource_def.name
        self._description = f"[MCP Resource] {desc}\nURI: {self._uri}"
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._resource_timeout = resource_timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        retried_transient = False
        refreshed_session = False
        while True:
            try:
                result = await asyncio.wait_for(
                    self._session.read_resource(self._uri),
                    timeout=self._resource_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP resource '{}' timed out after {}s", self._name, self._resource_timeout
                )
                return f"(MCP resource read timed out after {self._resource_timeout}s)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP resource '{}' was cancelled by server/SDK", self._name)
                return "(MCP resource read was cancelled)"
            except Exception as exc:
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "resource",
                ):
                    refreshed_session = True
                    continue
                if _is_transient(exc):
                    if not retried_transient:
                        retried_transient = True
                        logger.warning(
                            "MCP resource '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.exception(
                        "MCP resource '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP resource read failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP resource '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP resource read failed: {type(exc).__name__})"
            else:
                parts: list[str] = []
                for block in result.contents:
                    if isinstance(block, types.TextResourceContents):
                        parts.append(block.text)
                    elif isinstance(block, types.BlobResourceContents):
                        parts.append(f"[Binary resource: {len(block.blob)} bytes]")
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP resource read failed)"  # Unreachable


class MCPPromptWrapper(_MCPWrapperBase):
    """Wraps an MCP prompt as a read-only nanobot Tool."""

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, prompt_def, prompt_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._prompt_name = prompt_def.name
        self._name = _sanitize_name(f"mcp_{server_name}_prompt_{prompt_def.name}")
        desc = prompt_def.description or prompt_def.name
        self._description = (
            f"[MCP Prompt] {desc}\n"
            "Returns a filled prompt template that can be used as a workflow guide."
        )
        self._prompt_timeout = prompt_timeout

        # Build parameters from prompt arguments
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in prompt_def.arguments or []:
            prop: dict[str, Any] = {"type": "string"}
            if getattr(arg, "description", None):
                prop["description"] = arg.description
            properties[arg.name] = prop
            if arg.required:
                required.append(arg.name)
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types
        from mcp.shared.exceptions import McpError

        retried_transient = False
        refreshed_session = False
        while True:
            try:
                result = await asyncio.wait_for(
                    self._session.get_prompt(self._prompt_name, arguments=kwargs),
                    timeout=self._prompt_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP prompt '{}' timed out after {}s", self._name, self._prompt_timeout
                )
                return f"(MCP prompt call timed out after {self._prompt_timeout}s)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP prompt '{}' was cancelled by server/SDK", self._name)
                return "(MCP prompt call was cancelled)"
            except McpError as exc:
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "prompt",
                ):
                    refreshed_session = True
                    continue
                logger.exception(
                    "MCP prompt '{}' failed: code={} message={}",
                    self._name,
                    exc.error.code,
                    exc.error.message,
                )
                return f"(MCP prompt call failed: {exc.error.message} [code {exc.error.code}])"
            except Exception as exc:
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "prompt",
                ):
                    refreshed_session = True
                    continue
                if _is_transient(exc):
                    if not retried_transient:
                        retried_transient = True
                        logger.warning(
                            "MCP prompt '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.exception(
                        "MCP prompt '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP prompt call failed after retry: {type(exc).__name__})"
                logger.exception(
                    "MCP prompt '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP prompt call failed: {type(exc).__name__})"
            else:
                parts: list[str] = []
                for message in result.messages:
                    content = message.content
                    if isinstance(content, types.TextContent):
                        parts.append(content.text)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, types.TextContent):
                                parts.append(block.text)
                            else:
                                parts.append(str(block))
                    else:
                        parts.append(str(content))
                return "\n".join(parts) or "(no output)"

        return "(MCP prompt call failed)"  # Unreachable


async def connect_mcp_servers(
    mcp_servers: dict,
    registry: ToolRegistry,
    *,
    sandbox: str = "",
    workspace: str | Path | None = None,
    runtime_identity: Any | None = None,
    failure_reasons: dict[str, str] | None = None,
) -> dict[str, AsyncExitStack]:
    """Connect to configured MCP servers and register their tools, resources, prompts.

    Returns a dict mapping server name -> its dedicated AsyncExitStack.
    Each server gets its own stack to prevent cancel scope conflicts
    when multiple MCP servers are configured.
    """
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    async def connect_single_server(name: str, cfg) -> tuple[str, AsyncExitStack | None]:
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()
        stderr_capture = _MCPStderrCapture(name)

        try:
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    if failure_reasons is not None:
                        failure_reasons[name] = "mcp_configuration_error: no command or url configured"
                    await server_stack.aclose()
                    return name, None

            if transport_type == "stdio":
                launch = _prepare_stdio_launch(
                    name=name,
                    cfg=cfg,
                    inherited_sandbox=sandbox,
                    workspace=workspace,
                    runtime_identity=runtime_identity,
                )
                logger.debug(
                    "MCP server '{}': launching stdio via adapter (sandbox={}, backend={}, cwd={})",
                    name,
                    launch.sandbox_requested or "disabled",
                    launch.sandbox_backend or "none",
                    launch.cwd,
                )
                read, write = await server_stack.enter_async_context(
                    _stdio_client_from_launch(
                        launch,
                        runtime_identity=runtime_identity,
                        errlog=stderr_capture,
                    )
                )
            elif transport_type == "sse":
                if not await _probe_http_url(cfg.url):
                    logger.warning("MCP server '{}': {} unreachable, skipping", name, cfg.url)
                    if failure_reasons is not None:
                        failure_reasons[name] = "mcp_connection_failed: HTTP endpoint unreachable"
                    await server_stack.aclose()
                    return name, None

                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **(cfg.headers or {}),
                        **(headers or {}),
                    }
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                if not await _probe_http_url(cfg.url):
                    logger.warning("MCP server '{}': {} unreachable, skipping", name, cfg.url)
                    if failure_reasons is not None:
                        failure_reasons[name] = "mcp_connection_failed: HTTP endpoint unreachable"
                    await server_stack.aclose()
                    return name, None

                http_client = await server_stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await server_stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                if failure_reasons is not None:
                    failure_reasons[name] = f"mcp_configuration_error: unknown transport {transport_type!r}"
                await server_stack.aclose()
                return name, None

            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools.tools]
            available_wrapped_names = [_sanitize_name(f"mcp_{name}_{tool_def.name}") for tool_def in tools.tools]
            for tool_def in tools.tools:
                wrapped_name = _sanitize_name(f"mcp_{name}_{tool_def.name}")
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
                registered_count += 1
                if enabled_tools:
                    if tool_def.name in enabled_tools:
                        matched_enabled_tools.add(tool_def.name)
                    if wrapped_name in enabled_tools:
                        matched_enabled_tools.add(wrapped_name)

            if enabled_tools and not allow_all_tools:
                unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
                if unmatched_enabled_tools:
                    logger.warning(
                        "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                        "Available wrapped names: {}",
                        name,
                        ", ".join(unmatched_enabled_tools),
                        ", ".join(available_raw_names) or "(none)",
                        ", ".join(available_wrapped_names) or "(none)",
                    )

            try:
                resources_result = await session.list_resources()
                for resource in resources_result.resources:
                    wrapper = MCPResourceWrapper(
                        session, name, resource, resource_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug(
                        "MCP: registered resource '{}' from server '{}'", wrapper.name, name
                    )
            except Exception as e:
                logger.debug("MCP server '{}': resources not supported or failed: {}", name, e)

            try:
                prompts_result = await session.list_prompts()
                for prompt in prompts_result.prompts:
                    wrapper = MCPPromptWrapper(
                        session, name, prompt, prompt_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug("MCP: registered prompt '{}' from server '{}'", wrapper.name, name)
            except Exception as e:
                logger.debug("MCP server '{}': prompts not supported or failed: {}", name, e)

            logger.info(
                "MCP server '{}': connected, {} capabilities registered", name, registered_count
            )
            if failure_reasons is not None:
                failure_reasons.pop(name, None)
            return name, server_stack

        except Exception as e:
            with suppress(Exception):
                await server_stack.aclose()
            hint = ""
            text = str(e).lower()
            if any(
                marker in text
                for marker in (
                    "parse error",
                    "invalid json",
                    "unexpected token",
                    "jsonrpc",
                    "content-length",
                )
            ):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )
            reason = _mcp_stdio_failure_reason(e, stderr_capture.text)
            if failure_reasons is not None:
                failure_reasons[name] = reason
            stderr_tail = stderr_capture.text.strip()
            if stderr_tail:
                logger.error(
                    "MCP server '{}': stderr tail before failure:\n{}",
                    name,
                    stderr_tail[-2000:],
                )
            logger.exception("MCP server '{}': failed to connect: {} {}", name, reason, hint)
            return name, None

    server_stacks: dict[str, AsyncExitStack] = {}

    for name, cfg in mcp_servers.items():
        try:
            result = await connect_single_server(name, cfg)
        except Exception as e:
            if failure_reasons is not None:
                failure_reasons[name] = _mcp_stdio_failure_reason(e)
            logger.exception("MCP server '{}' connection failed: {}", name, e)
            continue
        if result is not None and result[1] is not None:
            server_stacks[result[0]] = result[1]

    return server_stacks


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted session kwargs for MCP preset attachments."""
    mcp_presets = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    return {"mcp_presets": mcp_presets} if isinstance(mcp_presets, list) and mcp_presets else {}


def status_runtime_lines(
    *,
    configured_server_names: set[str] | None = None,
    connected_server_names: set[str] | None = None,
    failure_reasons: Mapping[str, str] | None = None,
    skip: bool = False,
) -> list[str]:
    """Return model-visible MCP connection status for all configured servers."""
    if skip:
        return []

    configured = sorted(configured_server_names or set())
    if not configured:
        return []

    connected_set = set(connected_server_names or set())
    connected = [name for name in configured if name in connected_set]
    disconnected = [name for name in configured if name not in connected_set]
    lines: list[str] = []

    if connected:
        visible = connected[:8]
        suffix = "" if len(connected) <= len(visible) else f", +{len(connected) - len(visible)} more"
        lines.append(
            "MCP Servers Live: "
            + ", ".join(f"@{name} (tool_prefix=mcp_{name}_)" for name in visible)
            + suffix
            + "."
        )

    if disconnected:
        visible = disconnected[:8]
        suffix = "" if len(disconnected) <= len(visible) else f"; +{len(disconnected) - len(visible)} more"
        details: list[str] = []
        for name in visible:
            reason = ""
            if failure_reasons is not None:
                reason = str(failure_reasons.get(name) or "").strip()
            details.append(f"@{name}: {reason or 'not connected yet'}")
        lines.append(
            "MCP Servers Configured But Not Live: "
            + "; ".join(details)
            + suffix
            + ". Their tools are unavailable until the MCP connection succeeds."
        )

    return lines


def runtime_lines(
    message: Any,
    *,
    available_server_names: set[str] | None = None,
    configured_server_names: set[str] | None = None,
    connected_server_names: set[str] | None = None,
    failure_reasons: Mapping[str, str] | None = None,
    skip: bool = False,
) -> list[str]:
    """Return model-visible MCP preset annotations for the current turn."""
    if skip:
        return []
    if configured_server_names is None:
        configured_server_names = available_server_names
    if connected_server_names is None:
        connected_server_names = available_server_names
    metadata = message.metadata if isinstance(getattr(message, "metadata", None), Mapping) else None
    structured = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    if not isinstance(structured, list):
        return []

    lines: list[str] = []
    for item in structured[:8]:
        if not isinstance(item, Mapping):
            continue
        raw_name = str(item.get("name") or "").strip().lower()
        if not raw_name:
            continue
        display = str(item.get("display_name") or raw_name).strip() or raw_name
        transport = str(item.get("transport") or "mcp").strip() or "mcp"
        prefix = f"mcp_{raw_name}_"
        if configured_server_names is not None and raw_name not in configured_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured in WebUI Settings, "
                "but this gateway has not loaded the latest MCP settings yet. "
                f"Tools with prefix `{prefix}` may not be available yet; if they are missing, "
                "tell the user to restart My-agent."
            )
            continue
        if connected_server_names is not None and raw_name not in connected_server_names:
            reason = ""
            if failure_reasons is not None:
                raw_reason = str(failure_reasons.get(raw_name) or "").strip()
                if raw_reason:
                    reason = f"Last failure: {raw_reason}. "
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured, "
                "but its MCP connection is not currently live. "
                f"{reason}"
                f"Tools with prefix `{prefix}` may be unavailable; tell the user to open Settings, "
                "run the preset test, and restart My-agent only if hot reload is unavailable."
            )
            continue
        lines.append(
            "MCP Preset Attachment: "
            f"@{raw_name} ({display}; transport={transport}; tool_prefix={prefix}). "
            f"Prefer available tools whose names start with `{prefix}` for this request; "
            "do not substitute shell commands for this MCP integration unless the user asks."
        )
    return lines


async def connect_missing_servers(state: Any, registry: ToolRegistry) -> None:
    """Connect configured MCP servers that are not currently live."""
    missing_servers = {
        name: cfg for name, cfg in state._mcp_servers.items() if name not in state._mcp_stacks
    }
    if state._mcp_connecting or not missing_servers:
        return
    state._mcp_connecting = True
    try:
        connected = await connect_mcp_servers(
            missing_servers,
            registry,
            sandbox=getattr(state.exec_config, "sandbox", ""),
            workspace=getattr(state, "workspace", None),
            runtime_identity=getattr(state, "runtime_identity_config", None),
            failure_reasons=getattr(state, "_mcp_failures", None),
        )
        state._mcp_stacks.update(connected)
        _attach_reconnect_handlers(state, registry, connected)
        state._mcp_connected = bool(state._mcp_stacks)
        if connected:
            logger.info("MCP connected servers: {}", sorted(connected))
        else:
            logger.warning("No MCP servers connected successfully (will retry next message)")
    except asyncio.CancelledError:
        logger.warning("MCP connection cancelled (will retry next message)")
        state._mcp_connected = bool(state._mcp_stacks)
    except BaseException as e:
        logger.warning("Failed to connect MCP servers (will retry next message): {}", e)
        state._mcp_connected = bool(state._mcp_stacks)
    finally:
        state._mcp_connecting = False


async def reload_servers(state: Any, registry: ToolRegistry) -> dict[str, Any]:
    """Reconcile live MCP connections with the current config file."""
    async with _reload_lock(state):
        try:
            from my_agent.config.loader import load_config, resolve_config_env_vars

            config = resolve_config_env_vars(load_config())
            next_servers = dict(config.tools.mcp_servers)
        except Exception as exc:
            logger.warning("MCP hot reload could not read config: {}", exc)
            return {
                "ok": False,
                "message": "Could not reload MCP config. Restart My-agent to pick up changes.",
                "requires_restart": True,
                "error": str(exc),
            }

        current_servers = dict(state._mcp_servers)
        current_names = set(current_servers)
        next_names = set(next_servers)
        removed = sorted(current_names - next_names)
        added = sorted(next_names - current_names)
        changed = sorted(
            name
            for name in current_names & next_names
            if _server_signature(current_servers[name]) != _server_signature(next_servers[name])
        )

        tools_removed = 0
        for name in [*removed, *changed]:
            tools_removed += _unregister_server_tools(state, registry, name)
            await _close_server(state, name)
            getattr(state, "_mcp_failures", {}).pop(name, None)

        state._mcp_servers = next_servers
        retry_missing = sorted(
            name
            for name in next_names
            if name not in state._mcp_stacks and name not in set(added) | set(changed)
        )
        to_connect_names = sorted(set(added) | set(changed) | set(retry_missing))
        to_connect = {name: next_servers[name] for name in to_connect_names}
        connected: dict[str, AsyncExitStack] = {}
        if to_connect:
            connected = await connect_mcp_servers(
                to_connect,
                registry,
                sandbox=getattr(state.exec_config, "sandbox", ""),
                workspace=getattr(state, "workspace", None),
                runtime_identity=getattr(state, "runtime_identity_config", None),
                failure_reasons=getattr(state, "_mcp_failures", None),
            )
            state._mcp_stacks.update(connected)
            _attach_reconnect_handlers(state, registry, connected)

        state._mcp_connected = bool(state._mcp_stacks)
        failed = sorted(set(to_connect) - set(connected))
        unchanged = not removed and not added and not changed and not retry_missing
        ok = not failed
        if failed:
            message = "MCP config reloaded, but some servers did not connect: " + ", ".join(failed)
        elif unchanged:
            message = "MCP config is already live."
        elif retry_missing and not added and not changed and not removed:
            message = "MCP connections refreshed without restarting My-agent."
        else:
            message = "MCP config reloaded without restarting My-agent."

        logger.info(
            "MCP hot reload: added={} changed={} removed={} retried={} connected={} failed={} tools_removed={}",
            added,
            changed,
            removed,
            retry_missing,
            sorted(connected),
            failed,
            tools_removed,
        )
        return {
            "ok": ok,
            "message": message,
            "added": added,
            "changed": changed,
            "removed": removed,
            "retried": retry_missing,
            "connected": sorted(state._mcp_stacks),
            "configured": sorted(state._mcp_servers),
            "failed": failed,
            "tools_removed": tools_removed,
            "requires_restart": False,
        }


async def request_mcp_reload(bus: Any, *, timeout: float = 15.0) -> dict[str, Any]:
    """Ask the running agent loop to reconcile live MCP connections."""
    loop = asyncio.get_running_loop()
    ack: asyncio.Future[dict[str, Any]] = loop.create_future()
    await bus.publish_inbound(
        InboundMessage(
            channel="system",
            sender_id="webui-settings",
            chat_id="runtime",
            content=RUNTIME_CONTROL_MCP_RELOAD,
            metadata={
                INBOUND_META_RUNTIME_CONTROL: RUNTIME_CONTROL_MCP_RELOAD,
                RUNTIME_CONTROL_ACK: ack,
            },
        )
    )
    try:
        result = await asyncio.wait_for(ack, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": "MCP hot reload timed out. Restart My-agent to pick up changes.",
            "requires_restart": True,
        }
    return result if isinstance(result, dict) else {
        "ok": False,
        "message": "MCP hot reload returned an unexpected response.",
        "requires_restart": True,
    }


async def handle_runtime_control(state: Any, msg: InboundMessage, registry: ToolRegistry) -> bool:
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    control = metadata.get(INBOUND_META_RUNTIME_CONTROL)
    if control != RUNTIME_CONTROL_MCP_RELOAD:
        return False

    ack = metadata.get(RUNTIME_CONTROL_ACK)
    try:
        result = await reload_servers(state, registry)
    except Exception as exc:
        logger.exception("MCP hot reload failed")
        result = {
            "ok": False,
            "message": "MCP hot reload failed. Restart My-agent to pick up changes.",
            "requires_restart": True,
            "error": str(exc),
        }
    if isinstance(ack, asyncio.Future) and not ack.done():
        ack.set_result(result)
    return True


def _reload_lock(state: Any) -> asyncio.Lock:
    try:
        return _RELOAD_LOCKS[state]
    except KeyError:
        lock = asyncio.Lock()
        _RELOAD_LOCKS[state] = lock
        return lock


def _attach_reconnect_handlers(
    state: Any,
    registry: ToolRegistry,
    server_names: Mapping[str, Any] | set[str] | list[str] | tuple[str, ...],
) -> None:
    async def reconnect(server_name: str, tool_name: str, stale_tool: Tool) -> Tool | None:
        return await _refresh_terminated_server(
            state,
            registry,
            server_name,
            tool_name,
            stale_tool,
        )

    for server_name in server_names:
        prefix = _tool_prefix(server_name)
        for tool_name in list(registry.tool_names):
            if not tool_name.startswith(prefix):
                continue
            tool = registry.get(tool_name)
            if isinstance(tool, _MCPWrapperBase):
                tool.set_reconnect_handler(reconnect)


async def _refresh_terminated_server(
    state: Any,
    registry: ToolRegistry,
    server_name: str,
    tool_name: str,
    stale_tool: Tool,
) -> Tool | None:
    async with _reload_lock(state):
        cfg = state._mcp_servers.get(server_name)
        if cfg is None:
            logger.warning(
                "MCP server '{}' session terminated but is no longer configured",
                server_name,
            )
            return None

        current_tool = registry.get(tool_name)
        if (
            current_tool is not None
            and current_tool is not stale_tool
            and server_name in state._mcp_stacks
        ):
            return current_tool

        logger.warning("MCP server '{}' session terminated; refreshing connection", server_name)
        _unregister_server_tools(state, registry, server_name)
        await _close_server(state, server_name)

        connected = await connect_mcp_servers(
            {server_name: cfg},
            registry,
            sandbox=getattr(state.exec_config, "sandbox", ""),
            workspace=getattr(state, "workspace", None),
            runtime_identity=getattr(state, "runtime_identity_config", None),
            failure_reasons=getattr(state, "_mcp_failures", None),
        )
        state._mcp_stacks.update(connected)
        _attach_reconnect_handlers(state, registry, connected)
        state._mcp_connected = bool(state._mcp_stacks)
        if server_name not in connected:
            logger.warning("MCP server '{}' reconnect failed after session termination", server_name)
            return None
        return registry.get(tool_name)


def _server_signature(cfg: Any) -> Any:
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump(mode="json")
    return cfg


def _tool_prefix(server_name: str) -> str:
    return _sanitize_name(f"mcp_{server_name}_")


def _unregister_server_tools(state: Any, registry: ToolRegistry, server_name: str) -> int:
    prefix = _tool_prefix(server_name)
    removed = 0
    for tool_name in list(registry.tool_names):
        if tool_name.startswith(prefix):
            registry.unregister(tool_name)
            removed += 1
    return removed


async def _close_server(state: Any, server_name: str) -> None:
    stack = state._mcp_stacks.pop(server_name, None)
    if stack is None:
        return
    try:
        await stack.aclose()
    except (RuntimeError, BaseExceptionGroup):
        logger.debug("MCP server '{}' cleanup error (can be ignored)", server_name)
