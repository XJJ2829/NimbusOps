"""CLI-Anything catalog, install state, and safe CLI execution."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from my_agent.apps.protocol import app_manifest, compact_dict
from my_agent.agent.tools.sandbox import prepare_sandboxed_argv
from my_agent.config.paths import get_runtime_subdir
from my_agent.security.auth import effective_exec_sandbox
from my_agent.security.runtime_identity import (
    apply_identity_environment,
    subprocess_identity_kwargs,
)
from my_agent.security.workspace_policy import is_path_within
from my_agent.webui.archive_packages import (
    ARCHIVE_COPY_IGNORE_PATTERNS,
    ArchivePackageError,
    _extract_zip_archive,
    copy_validated_skill,
    find_skill_package,
)

CLI_ANYTHING_REGISTRY_URL = "https://hkuds.github.io/CLI-Anything/registry.json"
CLI_ANYTHING_PUBLIC_REGISTRY_URL = "https://hkuds.github.io/CLI-Anything/public_registry.json"
CLI_ANYTHING_RAW_BASE = "https://raw.githubusercontent.com/HKUDS/CLI-Anything/main"
NANOBOT_EXTENSION_REGISTRY_URL = "https://raw.githubusercontent.com/Re-bin/nanobot-extension/main/registry.json"
NANOBOT_EXTENSION_RAW_BASE = "https://raw.githubusercontent.com/Re-bin/nanobot-extension/main"
_CATALOG_SOURCES = (
    ("harness", CLI_ANYTHING_REGISTRY_URL, CLI_ANYTHING_RAW_BASE, True),
    ("public", CLI_ANYTHING_PUBLIC_REGISTRY_URL, CLI_ANYTHING_RAW_BASE, True),
    ("extensions", NANOBOT_EXTENSION_REGISTRY_URL, NANOBOT_EXTENSION_RAW_BASE, False),
)

_MAX_TOOL_OUTPUT_CHARS = 12_000
_MAX_ARTIFACT_SCAN_PATHS = 4_000
_MAX_ARTIFACT_REPORT = 12
_SAFE_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_SAFE_NPM_DIR_RE = re.compile(r"^[a-z0-9._-]+$", re.IGNORECASE)
_LOCAL_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_LOCAL_ENTRY_POINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MENTION_RE = re.compile(r"(^|[\s([{])@([a-z0-9_-]+)\b", re.IGNORECASE)
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_CLI_COMMAND_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*(?:\s+[A-Za-z0-9][A-Za-z0-9_.:/=@-]*){0,5}$")
_SHELL_META_CHARS = ("|", "&&", "||", ";", "$(", "`", ">", "<")
_ENDORSEMENT_WORD_RE = re.compile(r"\bofficial\s+", re.IGNORECASE)
_CLI_COMMAND_LIMIT = 80
_ARTIFACT_EXTENSIONS = frozenset({
    ".csv",
    ".drawio",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".svg",
    ".txt",
    ".vsdx",
    ".webp",
    ".xml",
})
_INLINE_ARTIFACT_EXTENSIONS = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
_ARTIFACT_IGNORE_DIRS = frozenset({
    ".git",
    ".hg",
    ".mypy_cache",
    ".nanobot",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
})


class CliAppError(ValueError):
    """User-facing CLI Apps failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(slots=True)
class CliAppsRuntimeConfig:
    """Runtime knobs for CLI Apps."""

    install_timeout: int = 300
    run_timeout: int = 60
    catalog_ttl_seconds: int = 3600
    sandbox: str = ""
    runtime_identity: Any | None = None


_BRANDS: dict[str, tuple[str, str]] = {
    "1password-cli": ("1password", "#3B66BC"),
    "arcgis": ("arcgis", "#2C7AC3"),
    "arcgis-pro": ("arcgis", "#2C7AC3"),
    "audacity": ("audacity", "#0000CC"),
    "blender": ("blender", "#E87D0D"),
    "browser": ("googlechrome", "#4285F4"),
    "calibre": ("calibre", "#45B29D"),
    "chromadb": ("chroma", "#FFDE2D"),
    "comfyui": ("comfyui", "#111827"),
    "contentful": ("contentful", "#2478CC"),
    "dify": ("dify", "#155EEF"),
    "drawio": ("diagramsdotnet", "#F08705"),
    "elevenlabs": ("elevenlabs", "#000000"),
    "eth2-quickstart": ("ethereum", "#627EEA"),
    "firefly-iii": ("fireflyiii", "#CD5029"),
    "freecad": ("freecad", "#418FDE"),
    "generate-veo-video": ("googlegemini", "#8E75B2"),
    "gimp": ("gimp", "#5C5543"),
    "godot": ("godotengine", "#478CBF"),
    "hacker-feeds-cli": ("rss", "#FFA500"),
    "inkscape": ("inkscape", "#000000"),
    "intelwatch": ("intel", "#0071C5"),
    "iterm2": ("iterm2", "#000000"),
    "jimeng": ("bytedance", "#3C8CFF"),
    "joplin": ("joplin", "#1071D3"),
    "kdenlive": ("kdenlive", "#527EB2"),
    "krita": ("krita", "#3BABFF"),
    "libreoffice": ("libreoffice", "#18A303"),
    "mailchimp": ("mailchimp", "#FFE01B"),
    "mermaid": ("mermaid", "#FF3670"),
    "minimax": ("minimax", "#111827"),
    "musescore": ("musescore", "#1A70B8"),
    "n8n": ("n8n", "#EA4B71"),
    "notebooklm": ("googlenotebooklm", "#4285F4"),
    "obs-studio": ("obsstudio", "#302E31"),
    "obsidian": ("obsidian", "#7C3AED"),
    "ollama": ("ollama", "#000000"),
    "pm2": ("pm2", "#2B037A"),
    "qgis": ("qgis", "#589632"),
    "safari": ("safari", "#006CFF"),
    "sanity": ("sanity", "#F03E2F"),
    "sentry": ("sentry", "#362D59"),
    "sketch": ("sketch", "#F7B500"),
    "shopify": ("shopify", "#7AB55C"),
    "nsight-graphics": ("nvidia", "#76B900"),
    "unrealinsights": ("unrealengine", "#0E1128"),
    "ueatelier": ("unrealengine", "#0E1128"),
    "ve-twini": ("x", "#000000"),
    "wecom": ("wechat", "#07C160"),
    "suno": ("suno", "#000000"),
    "lldb": ("llvm", "#262D3A"),
    "android-cli": ("android", "#3DDC84"),
    "adguardhome": ("adguard", "#68BC71"),
    "zotero": ("zotero", "#CC2936"),
    "zoom": ("zoom", "#0B5CFF"),
}

_BRAND_DOMAINS: dict[str, tuple[str, str]] = {
    "3mf": ("3mf.io", "#00A1DE"),
    "anygen": ("anygen.io", "#111827"),
    "clibrowser": ("github.com/allthingssecurity/clibrowser", "#24292F"),
    "cloudanalyzer": ("github.com/rsasaki0109/CloudAnalyzer", "#2563EB"),
    "cloudcompare": ("cloudcompare.org", "#4D83C3"),
    "deployhq": ("deployhq.com", "#00A2D9"),
    "exa": ("exa.ai", "#111827"),
    "feishu": ("larksuite.com", "#00A5FF"),
    "inkstitch": ("inkstitch.org", "#222222"),
    "macrocli": ("github.com/HKUDS/CLI-Anything/tree/main/macrocli", "#24292F"),
    "mubu": ("mubu.com", "#16A085"),
    "nslogger": ("github.com/fpillet/NSLogger", "#24292F"),
    "novita": ("novita.ai", "#7C3AED"),
    "openscreen": ("openscreen.com", "#2563EB"),
    "py4csr": ("github.com/yanmingyu92/py4csr", "#24292F"),
    "quietshrink": ("github.com/achiya-automation/quietshrink", "#111827"),
    "renderdoc": ("renderdoc.org", "#2C7DB8"),
    "rms": ("rms.teltonika-networks.com", "#0054A6"),
    "sbox": ("sbox.game", "#F59E0B"),
    "seaclip": ("github.com/SeaClip-Lite/SeaClip", "#0284C7"),
    "shotcut": ("shotcut.org", "#3B82F6"),
    "slay-the-spire-ii": ("megacrit.com", "#B91C1C"),
    "stata": ("stata.com", "#1F4E79"),
    "unimol-tools": ("github.com/deepmodeling/Uni-Mol", "#4F46E5"),
    "videocaptioner": ("github.com/WEIFENG2333/VideoCaptioner", "#2563EB"),
    "wiremock": ("wiremock.org", "#FF6A00"),
}

_BRAND_ALIASES: dict[str, str] = {
    "1password": "1password-cli",
    "dify-workflow": "dify",
    "feishu-lark": "feishu",
    "lark-cli": "feishu",
    "minimax-cli": "minimax",
    "obsidian-cli": "obsidian",
    "slay-the-spire-2": "slay-the-spire-ii",
    "slay-the-spire-ii": "slay-the-spire-ii",
    "unimol-tools": "unimol-tools",
    "unimol": "unimol-tools",
    "veo": "generate-veo-video",
}

_BRAND_TRAILING_WORDS = ("cli", "workflow", "workflows", "app", "apps", "tool", "tools")


def _now() -> float:
    return time.time()


def _safe_skill_name(name: str) -> str:
    clean = _SAFE_NAME_RE.sub("-", name.lower()).strip("-")
    return f"cli-app-{clean or 'app'}"


def _has_shell_meta(command: str) -> bool:
    return any(char in command for char in _SHELL_META_CHARS)


def _command_exists(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    return shutil.which(parts[0]) is not None


def _is_pip_install_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return (
        len(tokens) >= 3
        and tokens[:2] == ["pip", "install"]
    ) or (
        len(tokens) >= 5
        and tokens[1:4] == ["-m", "pip", "install"]
        and tokens[0] in {"python", "python3", sys.executable}
    )


def _pip_uninstall_args_from_command(command: str) -> list[str] | None:
    if not command or _has_shell_meta(command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if tokens[:2] == ["pip", "uninstall"]:
        args = tokens[2:]
    elif (
        len(tokens) >= 5
        and tokens[1:4] == ["-m", "pip", "uninstall"]
        and tokens[0] in {"python", "python3", sys.executable}
    ):
        args = tokens[4:]
    else:
        return None
    packages = [arg for arg in args if arg not in {"-y", "--yes"}]
    if not packages or any(arg.startswith("-") for arg in packages):
        return None
    return packages


def _console_script_distribution(entry_point: str) -> str | None:
    if not entry_point:
        return None
    try:
        distributions = importlib_metadata.distributions()
    except Exception:
        return None
    for distribution in distributions:
        try:
            entry_points = distribution.entry_points
        except Exception:
            continue
        for item in entry_points:
            if item.group != "console_scripts" or item.name != entry_point:
                continue
            try:
                name = distribution.metadata.get("Name")
            except Exception:
                name = None
            return str(name or getattr(distribution, "name", "") or "").strip() or None
    return None


def _brand_key(value: str) -> str:
    return _SAFE_NAME_RE.sub("-", value.lower()).replace("_", "-").strip("-")


def _brand_candidates(app: dict[str, Any]) -> list[str]:
    values = [
        str(app.get("name") or ""),
        str(app.get("display_name") or ""),
        str(app.get("entry_point") or "").removeprefix("cli-anything-"),
    ]
    seen: set[str] = set()
    candidates: list[str] = []
    for value in values:
        key = _brand_key(value)
        while key and key not in seen:
            seen.add(key)
            candidates.append(key)
            parts = key.split("-")
            if len(parts) <= 1 or parts[-1] not in _BRAND_TRAILING_WORDS:
                break
            key = "-".join(parts[:-1])
    return candidates


def _brand_payload(app: dict[str, Any]) -> tuple[str | None, str | None]:
    declared_logo = str(app.get("logo_url") or "").strip()
    if declared_logo.startswith(("https://", "/")):
        declared_color = str(app.get("brand_color") or "").strip()
        return declared_logo, declared_color or None

    brand = None
    domain_brand = None
    for candidate in _brand_candidates(app):
        key = _BRAND_ALIASES.get(candidate, candidate)
        brand = _BRANDS.get(key)
        if brand:
            break
        domain_brand = _BRAND_DOMAINS.get(key)
        if domain_brand:
            break
    if not brand:
        if not domain_brand:
            return None, None
        domain, color = domain_brand
        return f"https://www.google.com/s2/favicons?domain={domain}&sz=64", color
    slug, color = brand
    return f"https://cdn.simpleicons.org/{slug}/{color.lstrip('#')}", color


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{int(_now() * 1_000_000)}.tmp")
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _safe_skill_path(value: str) -> str | None:
    if not value.startswith("skills/"):
        return None
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return value if parts[-1] == "SKILL.md" else None


def _skill_content_url(skill_md: str, *, raw_base: str = CLI_ANYTHING_RAW_BASE) -> str | None:
    safe_path = _safe_skill_path(skill_md)
    if safe_path:
        return f"{raw_base.rstrip('/')}/{safe_path}"
    parsed = urlparse(skill_md)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
        return None
    raw_prefix = raw_base.rstrip("/") + "/"
    if not skill_md.startswith(raw_prefix):
        return None
    suffix = skill_md.removeprefix(raw_prefix)
    return skill_md if _safe_skill_path(suffix) else None


def _truncate(text: str, limit: int = _MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n... truncated {omitted} characters ..."


def _catalog_description(app: dict[str, Any]) -> str:
    """Return catalog copy without implying vendor endorsement."""
    description = str(app.get("description") or "")
    return _ENDORSEMENT_WORD_RE.sub("", description).strip()


def _clean_markdown_inline(value: str) -> str:
    text = value.strip()
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip(" \t-:：")


def _command_name(command: str, *, entry_point: str = "") -> str:
    text = command.strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    if entry_point and len(tokens) > 1 and tokens[0] == entry_point:
        return tokens[1]
    return tokens[0] if tokens else text


def _append_cli_command(
    rows: list[dict[str, str]],
    seen: set[str],
    *,
    command: Any,
    name: Any = "",
    description: Any = "",
    entry_point: str = "",
) -> None:
    command_text = _clean_markdown_inline(str(command or ""))
    if not command_text or len(command_text) > 300:
        return
    normalized = command_text.lower()
    if normalized in seen:
        return
    seen.add(normalized)
    row = compact_dict({
        "name": _clean_markdown_inline(str(name or "")) or _command_name(command_text, entry_point=entry_point),
        "command": command_text,
        "description": _clean_markdown_inline(str(description or "")),
    })
    rows.append(row)


def _normalize_cli_commands(raw: Any, *, entry_point: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw, dict):
        iterable: list[Any] = [
            {"name": key, "description": value}
            for key, value in raw.items()
        ]
    elif isinstance(raw, list):
        iterable = raw
    else:
        return rows
    for item in iterable:
        if isinstance(item, str):
            _append_cli_command(rows, seen, command=item, entry_point=entry_point)
        elif isinstance(item, dict):
            command = item.get("command") or item.get("usage") or item.get("name")
            name = item.get("name") or _command_name(str(command or ""), entry_point=entry_point)
            description = item.get("description") or item.get("summary") or item.get("help")
            _append_cli_command(
                rows,
                seen,
                command=command,
                name=name,
                description=description,
                entry_point=entry_point,
            )
        if len(rows) >= _CLI_COMMAND_LIMIT:
            break
    return rows


def _markdown_section(markdown: str, title: str) -> tuple[int, list[str]]:
    wanted = title.strip().lower()
    lines = markdown.splitlines()
    start_level = 0
    start_index = -1
    for index, line in enumerate(lines):
        match = _MARKDOWN_HEADING_RE.match(line.strip())
        if not match:
            continue
        heading = _clean_markdown_inline(match.group(2)).lower()
        if heading == wanted:
            start_level = len(match.group(1))
            start_index = index + 1
            break
    if start_index < 0:
        return 0, []
    out: list[str] = []
    for line in lines[start_index:]:
        match = _MARKDOWN_HEADING_RE.match(line.strip())
        if match and len(match.group(1)) <= start_level:
            break
        out.append(line)
    return start_level, out


def _split_markdown_table_row(line: str) -> list[str]:
    text = line.strip().strip("|")
    return [_clean_markdown_inline(cell) for cell in text.split("|")]


def _description_after_heading(lines: list[str], start: int) -> str:
    for line in lines[start + 1 : start + 6]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```") or _MARKDOWN_HEADING_RE.match(stripped):
            return ""
        if stripped.startswith("|"):
            return ""
        return _clean_markdown_inline(stripped)
    return ""


def _commands_from_supported_native_clis(markdown: str, *, entry_point: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        if not line.strip().lower().startswith("supported native clis:"):
            continue
        candidates: list[str] = []
        for inline in re.findall(r"`([^`]+)`", line):
            candidates.extend(inline.split())
        if not candidates:
            _, _, value = line.partition(":")
            candidates.extend(_clean_markdown_inline(value).split())
        for item in candidates:
            command_name = item.strip(" ,.;")
            if not _LOCAL_ENTRY_POINT_RE.fullmatch(command_name):
                continue
            _append_cli_command(
                rows,
                seen,
                name=command_name,
                command=f"{entry_point} {command_name}" if entry_point else command_name,
                description=(
                    f"Native CLI available through {entry_point}."
                    if entry_point
                    else "Native CLI available through this app."
                ),
                entry_point=entry_point,
            )
        break
    return rows


def _commands_from_skill_markdown(markdown: str, *, entry_point: str = "") -> list[dict[str, str]]:
    section_level, lines = _markdown_section(markdown, "Commands")
    if not lines:
        return _commands_from_supported_native_clis(markdown, entry_point=entry_point)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            command = stripped.removeprefix("$").removeprefix(">").strip()
            if command and not command.startswith("#"):
                _append_cli_command(rows, seen, command=command, entry_point=entry_point)
            continue
        heading = _MARKDOWN_HEADING_RE.match(stripped)
        if heading and len(heading.group(1)) > section_level:
            command = _clean_markdown_inline(heading.group(2))
            if _CLI_COMMAND_TOKEN_RE.match(command):
                _append_cli_command(
                    rows,
                    seen,
                    command=command,
                    description=_description_after_heading(lines, index),
                    entry_point=entry_point,
                )
            continue
        if stripped.startswith("|"):
            cells = [cell for cell in _split_markdown_table_row(stripped) if cell]
            if not cells or all(_MARKDOWN_TABLE_SEPARATOR_RE.match(cell) for cell in cells):
                continue
            lower = " ".join(cells).lower()
            if "command" in lower and ("description" in lower or "summary" in lower):
                continue
            _append_cli_command(
                rows,
                seen,
                command=cells[0],
                description=cells[1] if len(cells) > 1 else "",
                entry_point=entry_point,
            )
            continue
        bullet = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$", stripped)
        if bullet:
            body = bullet.group(1).strip()
            code = re.search(r"`([^`]+)`", body)
            if code:
                description = body[code.end() :].strip(" \t-:：")
                _append_cli_command(
                    rows,
                    seen,
                    command=code.group(1),
                    description=description,
                    entry_point=entry_point,
                )
                continue
            for separator in (" - ", " — ", " – ", ": ", "："):
                if separator in body:
                    command, description = body.split(separator, 1)
                    break
            else:
                command, description = body, ""
            command = _clean_markdown_inline(command)
            if _CLI_COMMAND_TOKEN_RE.match(command):
                _append_cli_command(
                    rows,
                    seen,
                    command=command,
                    description=description,
                    entry_point=entry_point,
                )
        if len(rows) >= _CLI_COMMAND_LIMIT:
            break
    return rows or _commands_from_supported_native_clis(markdown, entry_point=entry_point)


class CliAppManager:
    """Manage CLI-Anything registry entries and local install state."""

    def __init__(
        self,
        *,
        workspace: Path,
        data_dir: Path | None = None,
        runtime: CliAppsRuntimeConfig | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()
        self.data_dir = Path(data_dir) if data_dir is not None else get_runtime_subdir("cli-apps")
        self.runtime = runtime or CliAppsRuntimeConfig()

    @property
    def installed_path(self) -> Path:
        return self.data_dir / "installed.json"

    @property
    def packages_dir(self) -> Path:
        return self.data_dir / "packages"

    def _package_path(self, name: str) -> Path:
        return self.packages_dir / name

    def _cache_path(self, source: str) -> Path:
        return self.data_dir / f"{source}_registry_cache.json"

    def _load_installed(self) -> dict[str, Any]:
        data = _read_json(self.installed_path) or {}
        apps = data.get("apps") if isinstance(data.get("apps"), dict) else data
        return apps if isinstance(apps, dict) else {}

    def _save_installed(self, installed: dict[str, Any]) -> None:
        _write_json(self.installed_path, {"schema_version": 1, "apps": installed})

    def _local_app_from_entry(self, name: str, entry: dict[str, Any]) -> dict[str, Any] | None:
        local = entry.get("local_app")
        if not isinstance(local, dict):
            return None
        app_name = str(local.get("name") or name).strip().lower()
        if not app_name:
            return None
        entry_point = str(local.get("entry_point") or entry.get("entry_point") or "").strip()
        return compact_dict({
            "name": app_name,
            "display_name": str(local.get("display_name") or app_name),
            "version": str(local.get("version") or entry.get("version") or "local"),
            "description": str(local.get("description") or f"Imported local CLI app {app_name}."),
            "category": str(local.get("category") or "local"),
            "requires": str(local.get("requires") or "Imported local CLI package"),
            "source": "local",
            "_source": "local",
            "install_strategy": "bundled",
            "package_manager": "bundled",
            "entry_point": entry_point,
            "entry_path": local.get("entry_path"),
            "logo_url": local.get("logo_url"),
            "brand_color": local.get("brand_color"),
            "docs_url": local.get("docs_url"),
            "commands": local.get("commands"),
        })

    def _installed_app_from_entry(self, name: str, entry: dict[str, Any]) -> dict[str, Any]:
        local_app = self._local_app_from_entry(name, entry)
        if local_app is not None:
            return local_app
        entry_point = str(entry.get("entry_point") or "").strip()
        return {
            "name": name,
            "display_name": name,
            "version": str(entry.get("version") or ""),
            "description": f"Installed CLI tool {name}.",
            "category": "installed",
            "requires": "",
            "source": str(entry.get("source") or "installed"),
            "_source": str(entry.get("source") or "installed"),
            "install_strategy": str(entry.get("strategy") or "bundled"),
            "package_manager": str(entry.get("strategy") or "bundled"),
            "entry_point": entry_point,
        }

    def installed_names(self) -> list[str]:
        """Return registry names explicitly installed through CLI Apps."""
        return sorted(str(name) for name in self._load_installed())

    def _fetch_registry(
        self,
        url: str,
        cache_path: Path,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        cached = _read_json(cache_path)
        if (
            not force_refresh
            and cached
            and _now() - float(cached.get("_cached_at", 0)) < self.runtime.catalog_ttl_seconds
        ):
            data = cached.get("data")
            if isinstance(data, dict):
                return data

        try:
            response = httpx.get(url, timeout=15.0, follow_redirects=True)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("registry response must be an object")
        except Exception:
            if cached and isinstance(cached.get("data"), dict):
                return cached["data"]
            raise

        _write_json(cache_path, {"_cached_at": _now(), "data": data})
        return data

    def catalog(self, *, force_refresh: bool = False) -> tuple[list[dict[str, Any]], str | None]:
        registries: list[tuple[str, str, dict[str, Any]]] = []
        for source, url, raw_base, required in _CATALOG_SOURCES:
            try:
                registry = self._fetch_registry(
                    url,
                    self._cache_path(source),
                    force_refresh=force_refresh,
                )
            except Exception:
                if required:
                    raise
                continue
            registries.append((source, raw_base, registry))
        apps_by_name: dict[str, dict[str, Any]] = {}
        updated_values: list[str] = []
        for source, raw_base, registry in registries:
            meta = registry.get("meta")
            if isinstance(meta, dict) and isinstance(meta.get("updated"), str):
                updated_values.append(meta["updated"])
            for row in registry.get("clis", []):
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                entry = dict(row)
                entry["_source"] = source
                entry["_raw_base"] = raw_base
                key = str(entry["name"]).lower()
                previous = apps_by_name.get(key)
                if previous:
                    previous_source = str(previous.get("_source") or source)
                    merged_source = (
                        previous_source if previous_source == source else f"{previous_source}+{source}"
                    )
                    apps_by_name[key] = {**previous, **entry, "_source": merged_source}
                else:
                    apps_by_name[key] = entry
        return list(apps_by_name.values()), max(updated_values) if updated_values else None

    def _manifest_source(self, app: dict[str, Any]) -> str:
        source = str(app.get("_source") or "harness")
        if source == "local":
            return "local-import"
        if source == "extensions":
            return "nanobot-extension"
        return f"cli-anything:{source}"

    def _trust_registry(self, app: dict[str, Any]) -> str:
        if str(app.get("_source") or "") == "local":
            return "local"
        return "nanobot-extension" if str(app.get("_source") or "") == "extensions" else "cli-anything"

    def get_app(self, name: str, *, force_refresh: bool = False) -> dict[str, Any]:
        wanted = name.lower()
        installed = self._load_installed()
        for installed_name, raw_entry in installed.items():
            if str(installed_name).lower() != wanted or not isinstance(raw_entry, dict):
                continue
            local_app = self._local_app_from_entry(str(installed_name), raw_entry)
            if local_app is not None:
                return local_app
        for app in self.catalog(force_refresh=force_refresh)[0]:
            if str(app.get("name", "")).lower() == wanted:
                return app
        for installed_name, raw_entry in installed.items():
            if str(installed_name).lower() != wanted or not isinstance(raw_entry, dict):
                continue
            return self._installed_app_from_entry(str(installed_name), raw_entry)
        raise CliAppError(f"CLI app '{name}' not found", status=404)

    def mentioned_installed_apps(self, text: str) -> list[dict[str, str]]:
        """Return installed CLI Apps referenced as ``@name`` in user text."""
        if "@" not in text:
            return []
        installed = self._load_installed()
        if not installed:
            return []
        installed_by_name = {
            str(name).lower(): (str(name), data if isinstance(data, dict) else {})
            for name, data in installed.items()
        }
        seen: set[str] = set()
        mentions: list[dict[str, str]] = []
        for match in _MENTION_RE.finditer(text):
            wanted = str(match.group(2)).lower()
            if wanted in seen or wanted not in installed_by_name:
                continue
            installed_name, data = installed_by_name[wanted]
            seen.add(wanted)
            entry_point = str(data.get("entry_point") or "")
            mentions.append(
                {
                    "name": installed_name,
                    "entry_point": entry_point,
                    "source": str(data.get("source") or ""),
                    "skill": f"skills/{_safe_skill_name(installed_name)}/SKILL.md",
                    "tool": "run_cli_app",
                }
            )
        return mentions

    def _strategy(self, app: dict[str, Any]) -> str:
        package_manager = str(app.get("package_manager") or "").lower()
        install_strategy = str(app.get("install_strategy") or "").lower()
        if package_manager == "bundled" or install_strategy == "bundled":
            return "bundled"
        if package_manager in {"npm", "brew", "uv", "pip"}:
            return package_manager
        if app.get("npm_package"):
            return "npm"
        install_cmd = str(app.get("install_cmd") or "")
        if _is_pip_install_command(install_cmd):
            return "pip"
        return "unsupported"

    def _install_supported(self, app: dict[str, Any]) -> bool:
        if self._strategy(app) == "unsupported":
            return False
        install_cmd = str(app.get("install_cmd") or "")
        return not _has_shell_meta(install_cmd)

    def _skill_path(self, name: str) -> Path:
        return self.workspace / "skills" / _safe_skill_name(name) / "SKILL.md"

    def _app_payload(
        self,
        app: dict[str, Any],
        installed: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(app["name"])
        entry_point = str(app.get("entry_point") or "")
        install_supported = self._install_supported(app)
        is_installed = name in installed
        installed_entry = installed.get(name)
        entry_path = (
            str(installed_entry.get("entry_point_path") or "").strip()
            if isinstance(installed_entry, dict)
            else ""
        )
        available = bool(
            (entry_path and Path(entry_path).is_file())
            or (entry_point and shutil.which(entry_point))
        )
        if is_installed and available:
            status = "installed"
        elif is_installed:
            status = "missing"
        elif not install_supported:
            status = "unsupported"
        elif available:
            status = "available"
        else:
            status = "not_installed"
        logo_url, brand_color = _brand_payload(app)
        commands = self._commands_for_app(app, name=name, entry_point=entry_point)
        return {
            "name": name,
            "display_name": app.get("display_name") or name,
            "category": app.get("category") or "uncategorized",
            "description": _catalog_description(app),
            "requires": app.get("requires") or "",
            "source": app.get("_source") or "harness",
            "entry_point": entry_point,
            "install_supported": install_supported,
            "installed": is_installed,
            "available": available,
            "status": status,
            "logo_url": logo_url,
            "brand_color": brand_color,
            "skill_installed": self._skill_path(name).is_file(),
            "commands": commands,
            "manifest": self._manifest_payload(app, logo_url=logo_url, brand_color=brand_color),
        }

    def _commands_for_app(
        self,
        app: dict[str, Any],
        *,
        name: str,
        entry_point: str,
    ) -> list[dict[str, str]]:
        commands = _normalize_cli_commands(app.get("commands"), entry_point=entry_point)
        if commands:
            return commands
        skill_path = self._skill_path(name)
        try:
            markdown = skill_path.read_text(encoding="utf-8")
        except OSError:
            markdown = ""
        if markdown:
            commands = _commands_from_skill_markdown(markdown, entry_point=entry_point)
        if commands:
            return commands
        return _normalize_cli_commands(
            [{"name": "help", "command": f"{entry_point or name} --help", "description": "Show available commands and options."}],
            entry_point=entry_point,
        )

    def _package_ref(self, app: dict[str, Any]) -> dict[str, Any] | None:
        strategy = self._strategy(app)
        name = ""
        if strategy == "pip":
            try:
                uninstall = self._pip_uninstall_argv(app)
            except CliAppError:
                uninstall = None
            name = uninstall[-1] if uninstall else ""
        elif strategy == "npm":
            name = str(app.get("npm_package") or "").strip()
        elif strategy in {"brew", "uv"}:
            try:
                uninstall = self._argv_for_action(app, "uninstall")
            except CliAppError:
                uninstall = None
            if uninstall:
                name = uninstall[-1]
        if not strategy or strategy in {"unsupported", "bundled"}:
            return None
        return compact_dict({"manager": strategy, "name": name})

    def _manifest_payload(
        self,
        app: dict[str, Any],
        *,
        logo_url: str | None,
        brand_color: str | None,
    ) -> dict[str, Any]:
        name = str(app["name"])
        entry_point = str(app.get("entry_point") or "")
        strategy = self._strategy(app)
        skill_path = f"skills/{_safe_skill_name(name)}/SKILL.md"
        commands = _normalize_cli_commands(app.get("commands"), entry_point=entry_point)
        cli_capability = compact_dict({
            "type": "cli",
            "entry_point": entry_point,
            "package": self._package_ref(app),
            "commands": commands,
        })
        capabilities = [
            cli_capability,
            {"type": "skill", "path": skill_path},
        ]
        install_supported = self._install_supported(app)
        install = compact_dict({
            "supported": install_supported,
            "strategy": strategy,
            "managed_paths": [skill_path],
            "verification": ["entry_point_available"] if entry_point else [],
        })
        remove = compact_dict({
            "supported": strategy != "unsupported",
            "strategy": strategy,
            "managed_paths": [skill_path],
            "verification": (
                ["package_manager_ok", "entry_point_absent", "managed_paths_absent"]
                if strategy not in {"bundled", "unsupported"}
                else ["nanobot_state_absent", "managed_paths_absent"]
            ),
        })
        return app_manifest(
            app_id=name,
            display_name=str(app.get("display_name") or name),
            version=str(app.get("version") or ""),
            description=_catalog_description(app),
            category=str(app.get("category") or "uncategorized"),
            source=self._manifest_source(app),
            logo_url=logo_url,
            brand_color=brand_color,
            capabilities=capabilities,
            install=install,
            remove=remove,
            trust={
                "registry": self._trust_registry(app),
                "level": "catalog",
                "review_status": "catalog_entry",
            },
        )

    def payload(
        self,
        *,
        force_refresh: bool = False,
        installed_only: bool = False,
    ) -> dict[str, Any]:
        installed = self._load_installed()
        if installed_only:
            catalog_apps: list[dict[str, Any]] = []
            updated_values: list[str] = []
            for source, _url, raw_base, _required in _CATALOG_SOURCES:
                cached = _read_json(self._cache_path(source))
                registry = cached.get("data") if isinstance(cached, dict) else None
                if not isinstance(registry, dict):
                    continue
                meta = registry.get("meta")
                if isinstance(meta, dict) and isinstance(meta.get("updated"), str):
                    updated_values.append(meta["updated"])
                for row in registry.get("clis", []):
                    if not isinstance(row, dict) or not row.get("name"):
                        continue
                    catalog_apps.append({
                        **row,
                        "_source": source,
                        "_raw_base": raw_base,
                    })
            updated = max(updated_values) if updated_values else None
            catalog_by_name = {
                str(app.get("name") or "").lower(): app
                for app in catalog_apps
            }
            apps = [
                catalog_by_name.get(str(name).lower())
                or self._installed_app_from_entry(str(name), entry)
                for name, entry in installed.items()
                if isinstance(entry, dict)
            ]
        else:
            apps, updated = self.catalog(force_refresh=force_refresh)
        catalog_names = {str(app.get("name") or "").lower() for app in apps}
        for installed_name, raw_entry in installed.items():
            if str(installed_name).lower() in catalog_names or not isinstance(raw_entry, dict):
                continue
            local_app = self._local_app_from_entry(str(installed_name), raw_entry)
            if local_app is not None:
                apps.append(local_app)
        rows = [self._app_payload(app, installed) for app in apps]
        rows.sort(key=lambda item: (str(item["category"]), str(item["display_name"]).lower()))
        return {
            "apps": rows,
            "installed_count": sum(1 for item in rows if item["installed"]),
            "catalog_updated_at": updated,
        }

    def _pip_package_from_install(self, app: dict[str, Any]) -> str | None:
        install_cmd = str(app.get("install_cmd") or "")
        try:
            tokens = shlex.split(install_cmd)
        except ValueError:
            return None
        if tokens[:2] == ["pip", "install"]:
            args = tokens[2:]
        elif len(tokens) >= 5 and tokens[1:4] == ["-m", "pip", "install"]:
            args = tokens[4:]
        else:
            return None
        args = [arg for arg in args if not arg.startswith("-")]
        if len(args) != 1 or args[0].startswith("git+"):
            return None
        return args[0]

    @staticmethod
    def _pip_available() -> bool:
        """Return True if pip is importable for the current interpreter."""
        from importlib.util import find_spec

        return find_spec("pip") is not None

    def _pip_install_argv(self, app: dict[str, Any], *, update: bool = False) -> list[str]:
        install_cmd = str(app.get("install_cmd") or "")
        if not _is_pip_install_command(install_cmd) or _has_shell_meta(install_cmd):
            raise CliAppError("unsupported pip install command")
        tokens = shlex.split(install_cmd)
        args = tokens[2:] if tokens[:2] == ["pip", "install"] else tokens[4:]
        pip_available = self._pip_available()
        if pip_available:
            prefix = [sys.executable, "-m", "pip", "install"]
        elif shutil.which("uv"):
            prefix = ["uv", "pip", "install", "--python", sys.executable]
        else:
            raise CliAppError("pip is not available and uv is not installed")
        if update:
            if pip_available:
                prefix.extend(["--upgrade", "--force-reinstall"])
            else:
                prefix.extend(["--upgrade", "--reinstall"])
        return prefix + args

    def _pip_uninstall_argv(
        self,
        app: dict[str, Any],
        installed_entry: dict[str, Any] | None = None,
    ) -> list[str]:
        if self._pip_available():
            prefix = [sys.executable, "-m", "pip", "uninstall", "-y"]
        elif shutil.which("uv"):
            prefix = ["uv", "pip", "uninstall", "--python", sys.executable]
        else:
            raise CliAppError("pip is not available and uv is not installed")
        distribution = str((installed_entry or {}).get("pip_distribution") or "").strip()
        if distribution:
            return [*prefix, distribution]
        uninstall_cmd = str(app.get("uninstall_cmd") or "")
        packages = _pip_uninstall_args_from_command(uninstall_cmd)
        if packages:
            return [*prefix, *packages]
        package = str(app.get("pip_package") or "").strip() or self._pip_package_from_install(app)
        if not package:
            entry_point = str(app.get("entry_point") or "").strip()
            package = entry_point if entry_point.startswith("cli-anything-") else f"cli-anything-{_brand_key(str(app['name']))}"
        return [*prefix, package]

    def _npm_argv(self, app: dict[str, Any], action: str) -> list[str]:
        npm = shutil.which("npm")
        if not npm:
            raise CliAppError("npm is not installed")
        package = str(app.get("npm_package") or "")
        if not package:
            raise CliAppError("registry entry has no npm_package")
        if action == "install":
            return [npm, "install", "-g", package]
        if action == "update":
            return [npm, "install", "-g", package + "@latest"]
        return [npm, "uninstall", "-g", package]

    def _cleanup_stale_npm_install(self, app: dict[str, Any]) -> bool:
        npm = shutil.which("npm")
        package = str(app.get("npm_package") or "").strip()
        if not npm or not package or "/" in package or _SAFE_NPM_DIR_RE.match(package) is None:
            return False
        result = self._run_argv([npm, "root", "-g"], timeout=min(self.runtime.install_timeout, 30))
        if result.returncode != 0:
            return False
        root = Path(result.stdout.strip()).expanduser()
        try:
            root = root.resolve(strict=True)
        except OSError:
            return False
        targets = [root / package, *root.glob(f".{package}-*")]
        removed = False
        for target in targets:
            try:
                resolved = target.resolve(strict=False)
                if not is_path_within(resolved, root) or not target.is_dir():
                    continue
                shutil.rmtree(target)
                removed = True
            except OSError:
                continue
        return removed

    def _retry_stale_npm_install(
        self,
        app: dict[str, Any],
        argv: list[str],
        result: subprocess.CompletedProcess[str],
    ) -> subprocess.CompletedProcess[str]:
        output = f"{result.stderr}\n{result.stdout}"
        if "ENOTEMPTY" not in output or "rename" not in output:
            return result
        if not self._cleanup_stale_npm_install(app):
            return result
        return self._run_argv(argv, timeout=self.runtime.install_timeout)

    def _split_safe_command(self, app: dict[str, Any], key: str, expected: str) -> list[str]:
        command = str(app.get(key) or "")
        if not command:
            raise CliAppError(f"no {key} is defined for {app['name']}")
        if _has_shell_meta(command):
            raise CliAppError("script-style install commands are disabled in this MVP")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise CliAppError(f"invalid command: {exc}") from exc
        if not argv or argv[0] != expected:
            raise CliAppError(f"unsupported {expected} command")
        return argv

    def _argv_for_action(
        self,
        app: dict[str, Any],
        action: str,
        installed_entry: dict[str, Any] | None = None,
    ) -> list[str] | None:
        strategy = self._strategy(app)
        if strategy == "pip":
            if action == "install":
                return self._pip_install_argv(app)
            if action == "update":
                return self._pip_install_argv(app, update=True)
            return self._pip_uninstall_argv(app, installed_entry=installed_entry)
        if strategy == "npm":
            return self._npm_argv(app, action)
        if strategy == "brew":
            key = {"install": "install_cmd", "update": "update_cmd", "uninstall": "uninstall_cmd"}[action]
            return self._split_safe_command(app, key, "brew")
        if strategy == "uv":
            key = {"install": "install_cmd", "update": "update_cmd", "uninstall": "uninstall_cmd"}[action]
            return self._split_safe_command(app, key, "uv")
        if strategy == "bundled":
            return None
        raise CliAppError("this CLI app uses an unsupported install strategy")

    def _run_argv(self, argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _installed_entry(self, app: dict[str, Any]) -> dict[str, Any]:
        entry_point = str(app.get("entry_point") or "")
        strategy = self._strategy(app)
        entry: dict[str, Any] = {
            "version": app.get("version") or "unknown",
            "entry_point": entry_point,
            "source": app.get("_source") or "harness",
            "strategy": strategy,
            "installed_at": int(_now()),
        }
        resolved = shutil.which(entry_point) if entry_point else None
        if resolved:
            entry["entry_point_path"] = resolved
        if strategy == "pip":
            distribution = _console_script_distribution(entry_point)
            if distribution:
                entry["pip_distribution"] = distribution
        return entry

    def _fetch_skill_content(self, app: dict[str, Any]) -> str | None:
        skill_md = str(app.get("skill_md") or "").strip()
        if not skill_md:
            return None
        url = _skill_content_url(skill_md, raw_base=str(app.get("_raw_base") or CLI_ANYTHING_RAW_BASE))
        if not url:
            return None
        try:
            response = httpx.get(url, timeout=15.0, follow_redirects=True)
            response.raise_for_status()
            text = response.text
        except Exception:
            return None
        if "SKILL.md" not in url and not text.lstrip().startswith("---"):
            return None
        return text if len(text) < 250_000 else None

    def _fallback_skill(self, app: dict[str, Any]) -> str:
        name = str(app.get("name") or "unknown")
        display = str(app.get("display_name") or name)
        entry = str(app.get("entry_point") or f"cli-anything-{name}")
        description = _catalog_description(app) or f"Use {display} from my_agent."
        return f"""---
name: {_safe_skill_name(name)}
description: >-
  {description}
---

# {display}

Use this skill when the user asks nanobot to operate {display} through its installed CLI app.

If the user attached `@{name}` in chat, treat that as the selected app for the current turn.

## Commands

```bash
{entry} --help
{entry} --json --help
```

Prefer machine-readable output when the CLI supports `--json`.
"""

    def _with_nanobot_skill_note(self, content: str, app: dict[str, Any]) -> str:
        marker = "<!-- nanobot-cli-app-note -->"
        if marker in content:
            return content
        name = str(app.get("name") or "unknown")
        note = f"""{marker}
## Nanobot execution

Use the `run_cli_app` tool with `name="{name}"` for command execution. Do not invoke this CLI through shell unless the user explicitly asks. Prefer this skill when Runtime Context mentions `@{name}` as a CLI App Attachment.
"""
        lines = content.splitlines(keepends=True)
        if lines and lines[0].strip() == "---":
            for index, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    return "".join(lines[: index + 1]) + "\n" + note + "\n" + "".join(lines[index + 1 :])
        return note + "\n" + content

    def install_skill(self, app: dict[str, Any]) -> Path:
        path = self._skill_path(str(app["name"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self._fetch_skill_content(app) or self._fallback_skill(app)
        content = self._with_nanobot_skill_note(content, app)
        path.write_text(content, encoding="utf-8")
        return path

    def import_archive(self, data: bytes, filename: str | None = None) -> dict[str, Any]:
        """Install a local CLI app package uploaded through the WebUI."""
        try:
            with tempfile.TemporaryDirectory(prefix="my-agent-cli-app-") as tmp:
                root = Path(tmp)
                _extract_zip_archive(data, root, filename=filename)
                manifest_path = self._find_local_manifest(root)
                manifest = _read_json(manifest_path)
                if manifest is None:
                    raise ArchivePackageError("CLI package manifest must be valid JSON.")
                app = self._local_app_from_manifest(manifest)
                name = str(app["name"])
                expected_skill_name = _safe_skill_name(name)
                skill = find_skill_package(
                    manifest_path.parent,
                    expected_name=expected_skill_name,
                    allowed_extra_files={"agent-app.json", "manifest.json"},
                    allowed_extra_dirs={
                        path.name
                        for path in manifest_path.parent.iterdir()
                        if path.is_dir()
                    },
                )
                if not app.get("commands"):
                    skill_commands = _commands_from_skill_markdown(
                        skill.raw_markdown,
                        entry_point=str(app.get("entry_point") or ""),
                    )
                    if skill_commands:
                        app["commands"] = skill_commands
                bundled_entry = self._bundled_entry_path(manifest_path.parent, app)
                installed = self._load_installed()
                existing = installed.get(name)
                if existing and not (
                    isinstance(existing, dict)
                    and self._local_app_from_entry(name, existing) is not None
                ):
                    raise ArchivePackageError(
                        f"CLI app '{name}' is already managed by the catalog. Remove it first."
                    )
                package_dir = self._package_path(name)
                staged_package = package_dir.with_name(
                    f".{package_dir.name}.incoming.{os.getpid()}"
                )
                if staged_package.exists():
                    shutil.rmtree(staged_package)
                package_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(
                    manifest_path.parent,
                    staged_package,
                    ignore=shutil.ignore_patterns(*ARCHIVE_COPY_IGNORE_PATTERNS),
                )
                if package_dir.exists():
                    shutil.rmtree(package_dir)
                staged_package.replace(package_dir)

                skill_dir = self._skill_path(name).parent
                staged_skill_dir = skill_dir.with_name(
                    f".{skill_dir.name}.incoming.{os.getpid()}"
                )
                if staged_skill_dir.exists():
                    shutil.rmtree(staged_skill_dir)
                copy_validated_skill(skill, staged_skill_dir)
                if skill_dir.exists():
                    shutil.rmtree(skill_dir)
                staged_skill_dir.replace(skill_dir)
                skill_md = skill_dir / "SKILL.md"
                skill_md.write_text(
                    self._with_nanobot_skill_note(skill.raw_markdown, app),
                    encoding="utf-8",
                )
                entry_point = str(app.get("entry_point") or "")
                entry: dict[str, Any] = {
                    "version": app.get("version") or "local",
                    "entry_point": entry_point,
                    "source": "local",
                    "strategy": "bundled",
                    "installed_at": int(_now()),
                    "local_app": app,
                }
                resolved = (
                    str(
                        package_dir
                        / bundled_entry.relative_to(manifest_path.parent.resolve(strict=False))
                    )
                    if bundled_entry is not None
                    else (shutil.which(entry_point) if entry_point else None)
                )
                if resolved:
                    if bundled_entry is not None:
                        resolved_path = Path(resolved)
                        resolved_path.chmod(resolved_path.stat().st_mode | 0o111)
                    entry["entry_point_path"] = resolved
                installed[name] = entry
                self._save_installed(installed)
        except ArchivePackageError as exc:
            raise CliAppError(exc.message, status=exc.status) from exc
        return self.payload(installed_only=True) | {
            "last_action": {
                "ok": True,
                "message": f"Imported CLI package for {app['display_name']}.",
                "installed": True,
                "verification": ["manifest_valid", "skill_valid", "state_recorded"],
            }
        }

    def _find_local_manifest(self, root: Path) -> Path:
        candidates = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.name in {"agent-app.json", "manifest.json"}
            and "__MACOSX" not in path.parts
        ]
        if not candidates:
            raise ArchivePackageError("CLI package must include agent-app.json or manifest.json.")
        preferred = [path for path in candidates if path.name == "agent-app.json"]
        if len(preferred) == 1:
            return preferred[0]
        if len(candidates) == 1:
            return candidates[0]
        raise ArchivePackageError("CLI package contains multiple manifests.")

    def _local_app_from_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        schema = str(manifest.get("schema") or "agent-app.v1")
        if schema != "agent-app.v1":
            raise ArchivePackageError("CLI package manifest schema must be agent-app.v1.")
        name = str(manifest.get("id") or manifest.get("name") or "").strip().lower()
        if not _LOCAL_APP_ID_RE.fullmatch(name):
            raise ArchivePackageError(
                "CLI package id must use letters, digits, underscores, or hyphens."
            )
        display_name = str(manifest.get("display_name") or manifest.get("displayName") or name).strip()
        description = str(manifest.get("description") or "").strip()
        if not display_name:
            raise ArchivePackageError("CLI package display_name is required.")
        if not description:
            raise ArchivePackageError("CLI package description is required.")
        entry_point = self._entry_point_from_manifest(manifest)
        if not _LOCAL_ENTRY_POINT_RE.fullmatch(entry_point):
            raise ArchivePackageError("CLI entry_point must be a single executable name.")
        commands = self._commands_from_manifest(manifest, entry_point=entry_point)
        return compact_dict({
            "name": name,
            "display_name": display_name,
            "version": str(manifest.get("version") or "local"),
            "description": description,
            "category": str(manifest.get("category") or "local"),
            "requires": str(manifest.get("requires") or "Imported local CLI package"),
            "source": "local",
            "_source": "local",
            "install_strategy": "bundled",
            "package_manager": "bundled",
            "entry_point": entry_point,
            "entry_path": self._entry_path_from_manifest(manifest),
            "logo_url": manifest.get("logo_url"),
            "brand_color": manifest.get("brand_color"),
            "docs_url": manifest.get("docs_url"),
            "commands": commands,
        })

    def _commands_from_manifest(
        self,
        manifest: dict[str, Any],
        *,
        entry_point: str,
    ) -> list[dict[str, str]]:
        commands = _normalize_cli_commands(manifest.get("commands"), entry_point=entry_point)
        if commands:
            return commands
        capabilities = manifest.get("capabilities")
        if isinstance(capabilities, list):
            for item in capabilities:
                if not isinstance(item, dict) or item.get("type") != "cli":
                    continue
                commands = _normalize_cli_commands(item.get("commands"), entry_point=entry_point)
                if commands:
                    return commands
        return []

    def _entry_point_from_manifest(self, manifest: dict[str, Any]) -> str:
        capabilities = manifest.get("capabilities")
        if isinstance(capabilities, list):
            for item in capabilities:
                if not isinstance(item, dict) or item.get("type") != "cli":
                    continue
                entry_point = str(item.get("entry_point") or "").strip()
                if entry_point:
                    return entry_point
        entry_point = str(manifest.get("entry_point") or manifest.get("entryPoint") or "").strip()
        if entry_point:
            return entry_point
        raise ArchivePackageError("CLI package must define a cli capability entry_point.")

    def _entry_path_from_manifest(self, manifest: dict[str, Any]) -> str | None:
        capabilities = manifest.get("capabilities")
        if isinstance(capabilities, list):
            for item in capabilities:
                if not isinstance(item, dict) or item.get("type") != "cli":
                    continue
                value = str(item.get("entry_path") or item.get("path") or "").strip()
                if value:
                    return value
        value = str(manifest.get("entry_path") or manifest.get("entryPath") or "").strip()
        return value or None

    def _bundled_entry_path(
        self,
        package_root: Path,
        app: dict[str, Any],
    ) -> Path | None:
        entry_path = str(app.get("entry_path") or "").strip()
        if not entry_path:
            entry_point = str(app.get("entry_point") or "").strip()
            if entry_point and shutil.which(entry_point):
                return None
            raise ArchivePackageError(
                "CLI package must include entry_path for its bundled executable."
            )
        relative = Path(entry_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ArchivePackageError("CLI package entry_path must be a safe relative path.")
        root = package_root.resolve(strict=False)
        candidate = (root / relative).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ArchivePackageError("CLI package entry_path escapes the package.") from exc
        if not candidate.is_file():
            raise ArchivePackageError(f"CLI package executable not found: {entry_path}")
        return candidate

    def remove_skill(self, name: str) -> None:
        skill_dir = self._skill_path(name).parent
        if skill_dir.is_dir():
            shutil.rmtree(skill_dir)

    def _record_installed(self, app: dict[str, Any]) -> dict[str, Any]:
        installed = self._load_installed()
        entry = self._installed_entry(app)
        installed[str(app["name"])] = entry
        self._save_installed(installed)
        self.install_skill(app)
        return entry

    def install(self, name: str) -> dict[str, Any]:
        app = self.get_app(name)
        if not self._install_supported(app):
            raise CliAppError("this CLI app uses an unsupported install strategy")
        strategy = self._strategy(app)
        entry_point = str(app.get("entry_point") or "")
        if entry_point and shutil.which(entry_point):
            self._record_installed(app)
            return self.payload() | {
                "last_action": {
                    "ok": True,
                    "message": f"CLI for {app['display_name']} is already available.",
                    "installed": True,
                    "verification": ["entry_point_available", "state_recorded", "managed_paths_present"],
                }
            }
        if strategy == "bundled":
            detect_cmd = str(app.get("detect_cmd") or app.get("entry_point") or "")
            if detect_cmd and _command_exists(detect_cmd):
                self._record_installed(app)
                return self.payload() | {
                    "last_action": {
                        "ok": True,
                        "message": f"CLI for {app['display_name']} is available.",
                        "installed": True,
                        "verification": ["entry_point_available", "state_recorded"],
                    }
                }
            note = app.get("install_notes") or f"{app['display_name']} is bundled with its parent app."
            raise CliAppError(str(note))
        argv = self._argv_for_action(app, "install")
        assert argv is not None
        result = self._run_argv(argv, timeout=self.runtime.install_timeout)
        if strategy == "npm" and result.returncode != 0:
            result = self._retry_stale_npm_install(app, argv, result)
        if result.returncode != 0:
            raise CliAppError(_truncate(result.stderr or result.stdout or "install failed"), status=500)
        self._record_installed(app)
        return self.payload() | {
            "last_action": {
                "ok": True,
                "message": f"Installed CLI for {app['display_name']}.",
                "installed": True,
                "verification": ["package_manager_ok", "state_recorded", "managed_paths_present"],
            }
        }

    def update(self, name: str) -> dict[str, Any]:
        app = self.get_app(name, force_refresh=True)
        if str(app["name"]) not in self._load_installed():
            raise CliAppError("CLI app is not installed")
        if str(app.get("_source") or "") == "local":
            return self.payload() | {
                "last_action": {
                    "ok": True,
                    "message": f"Local CLI package for {app['display_name']} is installed. Import a new zip package to update it.",
                    "installed": True,
                    "verification": ["state_recorded", "managed_paths_present"],
                }
            }
        if self._strategy(app) == "bundled":
            self._record_installed(app)
            return self.payload() | {
                "last_action": {
                    "ok": True,
                    "message": f"Checked {app['display_name']}.",
                    "installed": True,
                    "verification": ["state_recorded"],
                }
            }
        argv = self._argv_for_action(app, "update")
        assert argv is not None
        result = self._run_argv(argv, timeout=self.runtime.install_timeout)
        if result.returncode != 0:
            raise CliAppError(_truncate(result.stderr or result.stdout or "update failed"), status=500)
        self._record_installed(app)
        return self.payload() | {
            "last_action": {
                "ok": True,
                "message": f"Updated CLI for {app['display_name']}.",
                "installed": True,
                "verification": ["package_manager_ok", "state_recorded", "managed_paths_present"],
            }
        }

    def uninstall(self, name: str) -> dict[str, Any]:
        app = self.get_app(name)
        installed = self._load_installed()
        if str(app["name"]) not in installed:
            raise CliAppError("CLI app is not installed")
        raw_installed_entry = installed.get(str(app["name"]))
        installed_entry = raw_installed_entry if isinstance(raw_installed_entry, dict) else {}
        strategy = self._strategy(app)
        entry_point = str(app.get("entry_point") or "").strip()
        managed_entry_path = str(installed_entry.get("entry_point_path") or "").strip()
        if strategy != "bundled":
            argv = self._argv_for_action(app, "uninstall", installed_entry=installed_entry)
            assert argv is not None
            result = self._run_argv(argv, timeout=self.runtime.install_timeout)
            if result.returncode != 0:
                raise CliAppError(_truncate(result.stderr or result.stdout or "uninstall failed"), status=500)
            still_managed = bool(managed_entry_path and Path(managed_entry_path).exists())
            still_available = bool(entry_point and shutil.which(entry_point))
            if still_managed or (not managed_entry_path and still_available):
                reason = (
                    f"the recorded entry point at {managed_entry_path} still exists"
                    if still_managed
                    else f"{entry_point} is still available on PATH"
                )
                message = (
                    f"Uninstall for {app['display_name']} completed, but {reason}, "
                    "so nanobot kept it installed."
                )
                return self.payload() | {
                    "last_action": {
                        "ok": False,
                        "message": message,
                        "removed": False,
                        "still_available": True,
                        "verification_failed": ["entry_point_absent"],
                    }
                }
        else:
            if str(app.get("_source") or "") == "local":
                package_dir = self._package_path(str(app["name"]))
                if package_dir.is_dir():
                    shutil.rmtree(package_dir)
                still_available = bool(
                    entry_point
                    and shutil.which(entry_point)
                    and shutil.which(entry_point) != managed_entry_path
                )
            else:
                still_available = bool(entry_point and shutil.which(entry_point))
        installed.pop(str(app["name"]), None)
        self._save_installed(installed)
        self.remove_skill(str(app["name"]))
        if strategy == "bundled" and still_available:
            message = (
                f"Removed {app['display_name']} from my_agent. {entry_point} "
                "is still available because it is managed outside nanobot."
            )
        elif still_available:
            message = (
                f"Uninstalled CLI for {app['display_name']}, but another {entry_point} "
                "is still available on PATH."
            )
        else:
            message = f"Uninstalled CLI for {app['display_name']}."
        return self.payload() | {
            "last_action": {
                "ok": True,
                "message": message,
                "removed": True,
                "still_available": still_available,
                "verification": ["state_absent", "managed_paths_absent"]
                if still_available
                else ["entry_point_absent", "state_absent", "managed_paths_absent"],
            }
        }

    def test(self, name: str) -> dict[str, Any]:
        app = self.get_app(name)
        entry = str(app.get("entry_point") or "")
        installed_entry = self._load_installed().get(str(app["name"]))
        managed_path = (
            str(installed_entry.get("entry_point_path") or "").strip()
            if isinstance(installed_entry, dict)
            else ""
        )
        resolved = managed_path if managed_path and Path(managed_path).is_file() else shutil.which(entry)
        if not entry or not resolved:
            raise CliAppError(f"{entry or name} is not available on PATH")
        result = self._run_argv([resolved, "--help"], timeout=min(self.runtime.run_timeout, 30))
        ok = result.returncode == 0
        output = _truncate((result.stdout or result.stderr or "").strip(), 3000)
        return self.payload() | {
            "last_action": {
                "ok": ok,
                "message": f"{entry} --help exited {result.returncode}",
                "output": output,
            }
        }

    def _resolve_cwd(
        self,
        working_dir: str | None,
        *,
        restrict_to_workspace: bool,
    ) -> Path:
        cwd = Path(working_dir).expanduser() if working_dir else self.workspace
        cwd = cwd.resolve(strict=False)
        workspace = self.workspace.resolve(strict=False)
        if restrict_to_workspace and not is_path_within(cwd, workspace):
            raise CliAppError("working_dir is outside the configured workspace")
        return cwd

    def _local_package_root_for_entry(
        self,
        name: str,
        installed_entry: dict[str, Any],
        resolved_entry: str,
    ) -> Path | None:
        if self._local_app_from_entry(name, installed_entry) is None:
            return None
        package_dir = self._package_path(name).expanduser().resolve(strict=False)
        resolved_path = Path(resolved_entry).expanduser().resolve(strict=False)
        if not package_dir.is_dir() or not is_path_within(resolved_path, package_dir):
            return None
        return package_dir

    def _iter_artifact_candidates(self, cwd: Path) -> list[Path]:
        if not cwd.is_dir():
            return []
        out: list[Path] = []
        stack = [cwd]
        scanned = 0
        while stack and scanned < _MAX_ARTIFACT_SCAN_PATHS:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda path: path.name.lower())
            except OSError:
                continue
            for path in entries:
                if scanned >= _MAX_ARTIFACT_SCAN_PATHS:
                    break
                scanned += 1
                try:
                    if path.is_dir() and not path.is_symlink():
                        if path.name not in _ARTIFACT_IGNORE_DIRS:
                            stack.append(path)
                        continue
                    if path.is_file() and path.suffix.lower() in _ARTIFACT_EXTENSIONS:
                        out.append(path.resolve(strict=False))
                except OSError:
                    continue
        return out

    def _artifact_snapshot(self, cwd: Path) -> dict[Path, tuple[int, int]]:
        snapshot: dict[Path, tuple[int, int]] = {}
        for path in self._iter_artifact_candidates(cwd):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[path] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _changed_artifacts(
        self,
        cwd: Path,
        before: dict[Path, tuple[int, int]],
    ) -> list[Path]:
        changed: list[tuple[int, Path]] = []
        for path, stamp in self._artifact_snapshot(cwd).items():
            if before.get(path) == stamp:
                continue
            changed.append((stamp[0], path))
        changed.sort(key=lambda item: (item[0], item[1].name.lower()))
        return [path for _, path in changed[-_MAX_ARTIFACT_REPORT:]]

    def _format_artifact_path(self, cwd: Path, path: Path) -> str:
        try:
            return path.relative_to(cwd).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def _format_artifact_size(path: Path) -> str:
        try:
            size = path.stat().st_size
        except OSError:
            return "unknown size"
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _format_artifact_lines(self, cwd: Path, paths: list[Path]) -> list[str]:
        lines: list[str] = []
        for path in paths:
            rel = self._format_artifact_path(cwd, path)
            ext = path.suffix.lower()
            kind = (
                "previewable image"
                if ext in _INLINE_ARTIFACT_EXTENSIONS
                else ext.lstrip(".") or "file"
            )
            lines.append(f"- {rel} ({kind}, {self._format_artifact_size(path)})")
        return lines

    def run(
        self,
        name: str,
        args: list[str] | None = None,
        *,
        json_output: bool = False,
        working_dir: str | None = None,
        timeout: int | None = None,
        restrict_to_workspace: bool = False,
    ) -> str:
        app = self.get_app(name)
        installed = self._load_installed()
        if str(app["name"]) not in installed:
            raise CliAppError(f"CLI app '{name}' is not installed")
        cwd = self._resolve_cwd(working_dir, restrict_to_workspace=restrict_to_workspace)
        installed_entry = installed[str(app["name"])]
        entry = str(installed_entry.get("entry_point") or app.get("entry_point") or "")
        managed_path = str(installed_entry.get("entry_point_path") or "").strip()
        resolved = managed_path if managed_path and Path(managed_path).is_file() else shutil.which(entry)
        if not entry or not resolved:
            raise CliAppError(f"{entry or name} is not available on PATH")
        clean_args = [str(arg) for arg in (args or [])]
        if json_output and "--json" not in clean_args:
            clean_args = ["--json", *clean_args]
        effective_timeout = max(1, min(timeout or self.runtime.run_timeout, 600))
        artifact_snapshot = self._artifact_snapshot(cwd)
        env = apply_identity_environment(os.environ.copy(), self.runtime.runtime_identity)
        read_roots = [Path(resolved).expanduser().resolve(strict=False).parent]
        package_root = self._local_package_root_for_entry(
            str(app["name"]),
            installed_entry,
            resolved,
        )
        if package_root is not None:
            read_roots.append(package_root)
        sandboxed = prepare_sandboxed_argv(
            sandbox=effective_exec_sandbox(self.runtime.sandbox) or "auto",
            argv=[resolved, *clean_args],
            workspace=self.workspace,
            cwd=cwd,
            env=env,
            extra_read_roots=read_roots,
            extra_write_roots=[cwd],
            force=True,
        )
        try:
            result = subprocess.run(
                sandboxed.argv,
                cwd=sandboxed.cwd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=sandboxed.env,
                **subprocess_identity_kwargs(self.runtime.runtime_identity),
            )
        except subprocess.TimeoutExpired:
            return f"CLI app '{name}' timed out after {effective_timeout}s"
        except Exception as exc:
            return f"CLI app '{name}' failed to start: {exc}"
        output = [
            f"CLI app '{name}' exited {result.returncode}.",
            f"Command: {entry} {' '.join(shlex.quote(arg) for arg in clean_args)}".rstrip(),
        ]
        if result.stdout:
            output.append("\nSTDOUT:\n" + result.stdout.rstrip())
        if result.stderr:
            output.append("\nSTDERR:\n" + result.stderr.rstrip())
        artifacts = self._changed_artifacts(cwd, artifact_snapshot)
        if artifacts:
            output.append(
                "\nArtifacts created or updated:\n"
                + "\n".join(self._format_artifact_lines(cwd, artifacts))
            )
            if any(path.suffix.lower() in _INLINE_ARTIFACT_EXTENSIONS for path in artifacts):
                output.append(
                    "\nTo show a preview in WebUI, reference a raster artifact with Markdown "
                    "using its workspace-relative path, for example `![diagram](diagram.png)`."
                )
        return _truncate("\n".join(output))
