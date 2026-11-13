from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from my_agent.agent.tools import mcp as mcp_tools
from my_agent.agent.tools.sandbox import SandboxedProcess
from my_agent.apps.cli.service import CliAppManager
from my_agent.apps.cli import service as cli_service
from my_agent.apps.mcp.service import McpPackageError, McpPackageManager
from my_agent.config.schema import MCPServerConfig
from my_agent.webui import archive_packages
from my_agent.webui.archive_packages import ArchivePackageError


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_skill_archive_installs_valid_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills_dir = tmp_path / "my_agent" / "skills"
    monkeypatch.setattr(archive_packages, "BUILTIN_SKILLS_DIR", skills_dir)
    payload = _zip_bytes(
        {
            "release/demo-skill/SKILL.md": (
                "---\n"
                "name: demo-skill\n"
                "description: Run a focused demo workflow.\n"
                "---\n\n"
                "# Demo\n"
            ),
            "release/demo-skill/scripts/demo.sh": "#!/bin/sh\nprintf demo\n",
        }
    )

    result = archive_packages.install_skill_archive(payload, "demo-skill.zip")

    assert result["ok"] is True
    assert result["skill"]["name"] == "demo-skill"
    assert (skills_dir / "demo-skill" / "SKILL.md").is_file()
    assert (skills_dir / "demo-skill" / "scripts" / "demo.sh").is_file()


def test_skill_archive_rejects_path_traversal(tmp_path: Path) -> None:
    payload = _zip_bytes({"../SKILL.md": "---\nname: bad\ndescription: bad\n---\n"})

    with pytest.raises(ArchivePackageError, match="Unsafe archive path"):
        archive_packages._extract_zip_archive(payload, tmp_path, filename="bad.zip")


def test_cli_archive_installs_and_runs_bundled_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CliAppManager(
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "runtime" / "cli-apps",
    )
    monkeypatch.setattr(manager, "catalog", lambda force_refresh=False: ([], None))
    manifest = {
        "schema": "agent-app.v1",
        "id": "demo-cli",
        "display_name": "Demo CLI",
        "version": "1.0.0",
        "description": "A bundled CLI import fixture.",
        "capabilities": [
            {
                "type": "cli",
                "entry_point": "demo-cli",
                "entry_path": "bin/demo-cli",
            }
        ],
    }
    payload = _zip_bytes(
        {
            "demo-cli/agent-app.json": json.dumps(manifest),
            "demo-cli/bin/demo-cli": (
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--help\" ]; then printf 'demo help\\n'; exit 0; fi\n"
                "printf 'demo run\\n'\n"
            ),
            "demo-cli/skills/cli-app-demo-cli/SKILL.md": (
                "---\n"
                "name: cli-app-demo-cli\n"
                "description: Use the imported demo CLI tool.\n"
                "---\n\n"
                "# Demo CLI\n"
                "\n"
                "## Commands\n"
                "\n"
                "- `demo-cli inspect`: Inspect the demo fixture.\n"
            ),
        }
    )

    result = manager.import_archive(payload, "demo-cli.zip")

    assert result["last_action"]["ok"] is True
    assert result["apps"][0]["name"] == "demo-cli"
    assert result["apps"][0]["available"] is True
    assert result["apps"][0]["commands"][0]["command"] == "demo-cli inspect"
    assert result["apps"][0]["commands"][0]["description"] == "Inspect the demo fixture."
    assert "demo run" in manager.run("demo-cli")
    assert (manager.packages_dir / "demo-cli" / "bin" / "demo-cli").is_file()
    assert (manager.workspace / "skills" / "cli-app-demo-cli" / "SKILL.md").is_file()


def test_cli_local_package_root_is_readable_under_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CliAppManager(
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "runtime" / "cli-apps",
    )
    monkeypatch.setattr(manager, "catalog", lambda force_refresh=False: ([], None))
    manifest = {
        "schema": "agent-app.v1",
        "id": "reader-cli",
        "display_name": "Reader CLI",
        "version": "1.0.0",
        "description": "A bundled CLI that reads package resources.",
        "capabilities": [
            {
                "type": "cli",
                "entry_point": "reader-cli",
                "entry_path": "bin/reader-cli",
            }
        ],
    }
    payload = _zip_bytes(
        {
            "reader-cli/agent-app.json": json.dumps(manifest),
            "reader-cli/bin/reader-cli": (
                "#!/bin/sh\n"
                "cat \"$(dirname \"$0\")/../lib/message.txt\"\n"
            ),
            "reader-cli/lib/message.txt": "package resource\n",
            "reader-cli/skills/cli-app-reader-cli/SKILL.md": (
                "---\n"
                "name: cli-app-reader-cli\n"
                "description: Use the imported reader CLI tool.\n"
                "---\n\n"
                "# Reader CLI\n"
            ),
        }
    )
    manager.import_archive(payload, "reader-cli.zip")
    captured: dict[str, Any] = {}

    def fake_prepare_sandboxed_argv(**kwargs: Any) -> SandboxedProcess:
        captured.update(kwargs)
        return SandboxedProcess(
            argv=[str(item) for item in kwargs["argv"]],
            cwd=str(kwargs["cwd"]),
            env=dict(kwargs["env"]),
            backend="test",
        )

    monkeypatch.setattr(cli_service, "prepare_sandboxed_argv", fake_prepare_sandboxed_argv)

    assert "package resource" in manager.run("reader-cli")
    read_roots = {Path(path).resolve(strict=False) for path in captured["extra_read_roots"]}
    assert manager._package_path("reader-cli").resolve(strict=False) in read_roots
    assert captured["extra_write_roots"] == [manager.workspace.resolve(strict=False)]


def test_cli_archive_ignores_packaging_noise_before_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_packages, "MAX_ARCHIVE_FILES", 3)
    manager = CliAppManager(
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "runtime" / "cli-apps",
    )
    monkeypatch.setattr(manager, "catalog", lambda force_refresh=False: ([], None))
    manifest = {
        "schema": "agent-app.v1",
        "id": "noisy-cli",
        "display_name": "Noisy CLI",
        "description": "A CLI import with ignored packaging noise.",
        "capabilities": [
            {
                "type": "cli",
                "entry_point": "noisy-cli",
                "entry_path": "bin/noisy-cli",
            }
        ],
    }
    files = {
        "noisy-cli/agent-app.json": json.dumps(manifest),
        "noisy-cli/bin/noisy-cli": "#!/bin/sh\nprintf noisy\n",
        "noisy-cli/skills/cli-app-noisy-cli/SKILL.md": (
            "---\n"
            "name: cli-app-noisy-cli\n"
            "description: Use the imported noisy CLI tool.\n"
            "---\n\n"
            "# Noisy CLI\n"
        ),
    }
    files.update({f"noisy-cli/.git/objects/{idx:02d}": "git-noise" for idx in range(20)})
    files.update({f"noisy-cli/__pycache__/mod{idx}.cpython-311.pyc": "pyc" for idx in range(20)})
    files["noisy-cli/.DS_Store"] = "finder"
    payload = _zip_bytes(files)

    result = manager.import_archive(payload, "noisy-cli.zip")

    assert result["last_action"]["ok"] is True
    assert (manager.packages_dir / "noisy-cli" / "bin" / "noisy-cli").is_file()
    assert not (manager.packages_dir / "noisy-cli" / ".git").exists()
    assert not (manager.packages_dir / "noisy-cli" / "__pycache__").exists()


def test_mcp_archive_installs_stdio_python_package(tmp_path: Path) -> None:
    manager = McpPackageManager(data_dir=tmp_path / "runtime" / "mcp")
    manifest = {
        "schema": "agent-app.v1",
        "id": "demo-mcp",
        "display_name": "Demo MCP",
        "version": "1.0.0",
        "description": "A bundled local MCP server.",
        "tools": [
            {"name": "plan_trip", "description": "Find route alternatives."},
        ],
        "capabilities": [
            {
                "type": "mcp",
                "transport": "stdio",
                "runtime": "python",
                "entry_module": "demo_mcp.server",
                "env": {"DEMO_MCP_SECRET": "${generated_secret}"},
            }
        ],
    }
    payload = _zip_bytes(
        {
            "demo-mcp/agent-app.json": json.dumps(manifest),
            "demo-mcp/demo_mcp/__init__.py": "",
            "demo-mcp/demo_mcp/server.py": "print('demo')\n",
        }
    )

    imported = manager.import_archive(payload, "demo-mcp.zip")

    assert imported.name == "demo-mcp"
    assert imported.config.type == "stdio"
    assert imported.config.command == sys.executable
    assert imported.config.args == ["-m", "demo_mcp.server"]
    assert imported.config.cwd == str(imported.package_dir)
    assert imported.config.sandbox is None
    assert imported.config.source == "mcp-package"
    assert imported.config.managed is True
    assert imported.config.tool_details == [
        {"name": "plan_trip", "description": "Find route alternatives."}
    ]
    assert imported.config.env["DEMO_MCP_SECRET"] != "${generated_secret}"
    assert (imported.package_dir / "agent-app.json").is_file()


def test_mcp_stdio_managed_package_cwd_can_live_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_dir = tmp_path / "runtime" / "mcp" / "packages" / "demo-mcp"
    workspace = tmp_path / "workspace"
    package_dir.mkdir(parents=True)
    workspace.mkdir()
    cfg = MCPServerConfig(
        type="stdio",
        command=sys.executable,
        args=["-m", "demo_mcp.server"],
        cwd=str(package_dir),
        source="mcp-package",
        managed=True,
        package_dir=str(package_dir),
    )
    captured: dict[str, Any] = {}

    def fake_prepare_sandboxed_argv(**kwargs: Any) -> SandboxedProcess:
        captured.update(kwargs)
        return SandboxedProcess(
            argv=[str(item) for item in kwargs["argv"]],
            cwd=str(kwargs["cwd"]),
            env=dict(kwargs["env"]),
            backend="test",
        )

    monkeypatch.setattr(mcp_tools, "prepare_sandboxed_argv", fake_prepare_sandboxed_argv)

    launch = mcp_tools._prepare_stdio_launch(
        name="demo-mcp",
        cfg=cfg,
        inherited_sandbox="auto",
        workspace=workspace,
        runtime_identity=None,
    )

    assert launch.cwd == str(package_dir)
    read_roots = {Path(path).resolve(strict=False) for path in captured["extra_read_roots"]}
    assert package_dir.resolve(strict=False) in read_roots
    assert captured["extra_write_roots"] == []


def test_mcp_archive_ignores_packaging_noise_before_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_packages, "MAX_ARCHIVE_FILES", 2)
    manager = McpPackageManager(data_dir=tmp_path / "runtime" / "mcp")
    manifest = {
        "schema": "agent-app.v1",
        "id": "noisy-mcp",
        "display_name": "Noisy MCP",
        "description": "A bundled local MCP server with ignored packaging noise.",
        "capabilities": [
            {
                "type": "mcp",
                "transport": "stdio",
                "runtime": "python",
                "entry_module": "noisy_mcp.server",
            }
        ],
    }
    files = {
        "noisy-mcp/agent-app.json": json.dumps(manifest),
        "noisy-mcp/noisy_mcp/server.py": "print('demo')\n",
    }
    files.update({f"noisy-mcp/.git/objects/{idx:02d}": "git-noise" for idx in range(20)})
    files.update({f"noisy-mcp/noisy_mcp/__pycache__/server{idx}.pyc": "pyc" for idx in range(20)})
    files["noisy-mcp/.DS_Store"] = "finder"
    payload = _zip_bytes(files)

    imported = manager.import_archive(payload, "noisy-mcp.zip")

    assert imported.name == "noisy-mcp"
    assert (imported.package_dir / "agent-app.json").is_file()
    assert not (imported.package_dir / ".git").exists()
    assert not (imported.package_dir / "noisy_mcp" / "__pycache__").exists()


def test_mcp_archive_preserves_remote_transport(tmp_path: Path) -> None:
    manager = McpPackageManager(data_dir=tmp_path / "runtime" / "mcp")
    manifest = {
        "schema": "agent-app.v1",
        "id": "remote-mcp",
        "display_name": "Remote MCP",
        "description": "A remote MCP service.",
        "capabilities": [
            {
                "type": "mcp",
                "transport": "streamableHttp",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer test"},
                "enabled_tools": ["search"],
            }
        ],
    }
    payload = _zip_bytes({
        "remote-mcp/agent-app.json": json.dumps(manifest),
    })

    imported = manager.import_archive(payload, "remote-mcp.zip")

    assert imported.config.type == "streamableHttp"
    assert imported.config.url == "https://example.com/mcp"
    assert imported.config.headers == {"Authorization": "Bearer test"}
    assert imported.config.enabled_tools == ["search"]
    assert imported.config.managed is True


def test_mcp_archive_rejects_missing_manifest(tmp_path: Path) -> None:
    manager = McpPackageManager(data_dir=tmp_path / "runtime" / "mcp")
    payload = _zip_bytes({"demo-mcp/server.py": "print('demo')\n"})

    with pytest.raises(McpPackageError, match="agent-app.json"):
        manager.import_archive(payload, "demo-mcp.zip")
