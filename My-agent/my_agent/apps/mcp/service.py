"""Archive-installed MCP servers.

The package format is intentionally narrow: one archive, one ``agent-app.json``,
one MCP capability. Runtime connection still uses the normal MCP config model.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from my_agent.config.paths import get_runtime_subdir
from my_agent.config.schema import MCPServerConfig
from my_agent.utils.helpers import ensure_dir
from my_agent.webui.archive_packages import (
    ARCHIVE_COPY_IGNORE_PATTERNS,
    ArchivePackageError,
    _ensure_inside,
    _extract_zip_archive,
)

_MCP_PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_PYTHON_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "local": "stdio",
    "streamablehttp": "streamableHttp",
    "streamable-http": "streamableHttp",
    "http": "streamableHttp",
    "sse": "sse",
}
_GENERATED_SECRET = "${generated_secret}"
_MAX_DECLARED_TOOLS = 512


class McpPackageError(ValueError):
    """User-facing MCP package import failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ImportedMcpPackage:
    name: str
    display_name: str
    package_dir: Path
    config: MCPServerConfig


class McpPackageManager:
    """Validate and stage local MCP server packages."""

    def __init__(self, data_dir: Path | None = None) -> None:
        base = data_dir or get_runtime_subdir("mcp")
        self.data_dir = ensure_dir(Path(base))
        self.packages_dir = ensure_dir(self.data_dir / "packages")

    def import_archive(
        self,
        data: bytes,
        filename: str | None = None,
        *,
        existing: MCPServerConfig | None = None,
    ) -> ImportedMcpPackage:
        try:
            with tempfile.TemporaryDirectory(prefix="my-agent-mcp-package-") as tmp:
                root = Path(tmp)
                _extract_zip_archive(data, root, filename=filename)
                package_root = _find_package_root(root)
                manifest = _read_manifest(package_root / "agent-app.json")
                name, display_name, description, cfg = _config_from_manifest(manifest)
                if existing is not None and not (
                    existing.managed and existing.source == "mcp-package"
                ):
                    raise ArchivePackageError(
                        f"MCP server '{name}' already exists and is not managed by an archive package."
                    )

                package_dir = (self.packages_dir / name).resolve(strict=False)
                _ensure_inside(package_dir, self.packages_dir.resolve(strict=False))
                staged = package_dir.with_name(f".{package_dir.name}.incoming.{os.getpid()}")
                if staged.exists():
                    shutil.rmtree(staged)
                shutil.copytree(
                    package_root,
                    staged,
                    ignore=shutil.ignore_patterns(*ARCHIVE_COPY_IGNORE_PATTERNS),
                )
                if package_dir.exists():
                    shutil.rmtree(package_dir)
                staged.replace(package_dir)

                cfg.source = "mcp-package"
                cfg.display_name = display_name
                cfg.description = description
                cfg.package_dir = str(package_dir)
                cfg.managed = True
                cfg.installed_at = int(time.time())
                if cfg.type == "stdio" and not cfg.cwd:
                    cfg.cwd = str(package_dir)
                return ImportedMcpPackage(
                    name=name,
                    display_name=display_name,
                    package_dir=package_dir,
                    config=cfg,
                )
        except ArchivePackageError as exc:
            raise McpPackageError(exc.message, status=exc.status) from exc

    def remove_package(self, name: str, cfg: MCPServerConfig | None) -> bool:
        if cfg is None or not cfg.package_dir:
            return False
        package_dir = Path(cfg.package_dir).expanduser().resolve(strict=False)
        _ensure_inside(package_dir, self.packages_dir.resolve(strict=False))
        if not package_dir.exists():
            return False
        shutil.rmtree(package_dir)
        return True


def _find_package_root(root: Path) -> Path:
    if (root / "agent-app.json").is_file():
        return root
    visible = [
        path
        for path in root.iterdir()
        if path.name not in {"__MACOSX", ".DS_Store"} and not path.name.startswith(".")
    ]
    if len(visible) == 1 and visible[0].is_dir() and (visible[0] / "agent-app.json").is_file():
        return visible[0]
    raise ArchivePackageError(
        "MCP package must contain exactly one package root with agent-app.json."
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArchivePackageError(f"Could not read agent-app.json: {exc}") from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArchivePackageError(f"agent-app.json must be valid JSON: {exc.msg}") from exc
    if not isinstance(manifest, dict):
        raise ArchivePackageError("agent-app.json must be a JSON object.")
    return manifest


def _config_from_manifest(manifest: Mapping[str, Any]) -> tuple[str, str, str, MCPServerConfig]:
    schema = str(manifest.get("schema") or "").strip()
    if schema != "agent-app.v1":
        raise ArchivePackageError("MCP package manifest schema must be agent-app.v1.")
    name = str(manifest.get("id") or manifest.get("name") or "").strip().lower()
    if not _MCP_PACKAGE_ID_RE.fullmatch(name):
        raise ArchivePackageError(
            "MCP package id must start with a letter or digit and contain only letters, digits, '_' or '-'."
        )
    display_name = str(
        manifest.get("display_name") or manifest.get("displayName") or name
    ).strip()
    description = str(manifest.get("description") or "").strip()
    if not display_name:
        raise ArchivePackageError("MCP package display_name is required.")
    if not description:
        raise ArchivePackageError("MCP package description is required.")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list):
        raise ArchivePackageError("MCP package capabilities must be an array.")
    mcp_caps = [
        cap
        for cap in capabilities
        if isinstance(cap, Mapping) and str(cap.get("type") or "").lower() == "mcp"
    ]
    if len(mcp_caps) != 1:
        raise ArchivePackageError("MCP package must define exactly one mcp capability.")
    cfg = _capability_to_config(mcp_caps[0])
    cfg.display_name = display_name
    cfg.description = description
    cfg.tool_details = _tool_details_from_manifest(
        mcp_caps[0].get("tools", manifest.get("tools"))
    )
    return name, display_name, description, cfg


def _capability_to_config(capability: Mapping[str, Any]) -> MCPServerConfig:
    transport = _normalize_transport(capability.get("transport"))
    enabled_tools = _string_list(capability.get("enabledTools", capability.get("enabled_tools")), ["*"])
    tool_timeout = _bounded_timeout(capability.get("toolTimeout", capability.get("tool_timeout", 30)))
    if transport == "stdio":
        return _stdio_config(capability, enabled_tools=enabled_tools, tool_timeout=tool_timeout)
    return _remote_config(
        capability,
        transport=transport,
        enabled_tools=enabled_tools,
        tool_timeout=tool_timeout,
    )


def _stdio_config(
    capability: Mapping[str, Any],
    *,
    enabled_tools: list[str],
    tool_timeout: int,
) -> MCPServerConfig:
    runtime = str(capability.get("runtime") or "").strip().lower()
    entry_module = str(capability.get("entry_module") or capability.get("entryModule") or "").strip()
    if runtime == "python" and entry_module:
        if not _PYTHON_MODULE_RE.fullmatch(entry_module):
            raise ArchivePackageError("MCP package entry_module must be a valid Python module path.")
        args = ["-m", entry_module, *_string_list(capability.get("args"), [])]
        command = str(capability.get("command") or sys.executable).strip()
    else:
        command = str(capability.get("command") or "").strip()
        args = _string_list(capability.get("args"), [])
    if not command:
        raise ArchivePackageError("stdio MCP packages require runtime+entry_module or command.")
    if len(command) > 512 or any(ch.isspace() for ch in command) or any(
        ch in command for ch in ("|", "&", ";", "<", ">", "`", "$")
    ):
        raise ArchivePackageError("MCP package command must be a single executable path or name.")
    return MCPServerConfig(
        type="stdio",
        command=command,
        args=args,
        env=_string_map(capability.get("env"), allow_generated_secret=True),
        cwd=str(capability.get("cwd") or "").strip(),
        sandbox=_sandbox_value(capability.get("sandbox"), default=None),
        tool_timeout=tool_timeout,
        enabled_tools=enabled_tools,
    )


def _remote_config(
    capability: Mapping[str, Any],
    *,
    transport: str,
    enabled_tools: list[str],
    tool_timeout: int,
) -> MCPServerConfig:
    url = str(capability.get("url") or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise ArchivePackageError("remote MCP packages require an http(s) url.")
    return MCPServerConfig(
        type=transport,  # type: ignore[arg-type]
        url=url,
        headers=_string_map(capability.get("headers")),
        sandbox=_sandbox_value(capability.get("sandbox"), default=None),
        tool_timeout=tool_timeout,
        enabled_tools=enabled_tools,
    )


def _normalize_transport(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = _TRANSPORT_ALIASES.get(raw.lower())
    if normalized is None:
        raise ArchivePackageError("MCP package transport must be stdio, streamableHttp, or sse.")
    return normalized


def _string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ArchivePackageError("MCP package string-list fields must be arrays of strings.")
    return list(value)


def _string_map(value: Any, *, allow_generated_secret: bool = False) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ArchivePackageError("MCP package object fields must contain string keys and values.")
    out = dict(value)
    if allow_generated_secret:
        generated = secrets.token_urlsafe(32)
        out = {
            key: (generated if item == _GENERATED_SECRET else item)
            for key, item in out.items()
        }
    return out


def _sandbox_value(value: Any, *, default: str | None) -> str | None:
    if value is None:
        return default
    raw = str(value).strip()
    if raw.lower() in {"", "none", "off", "false", "disabled"}:
        return ""
    return raw


def _tool_details_from_manifest(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ArchivePackageError("MCP package tools must be an array of objects.")
    if len(value) > _MAX_DECLARED_TOOLS:
        raise ArchivePackageError(f"MCP package tools cannot exceed {_MAX_DECLARED_TOOLS} entries.")
    details: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ArchivePackageError("MCP package tools entries must be JSON objects.")
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or "").strip()
        if not name:
            raise ArchivePackageError("MCP package tool names are required.")
        if len(name) > 128:
            raise ArchivePackageError("MCP package tool names must be 128 characters or less.")
        if name in seen:
            continue
        seen.add(name)
        details.append({
            "name": name,
            "description": description[:500],
        })
    return details


def _bounded_timeout(value: Any) -> int:
    try:
        return max(5, min(int(value), 600))
    except (TypeError, ValueError):
        return 30
