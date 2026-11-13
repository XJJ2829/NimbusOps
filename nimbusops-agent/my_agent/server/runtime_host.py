"""Local host actions for the browser WebUI runtime surface."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from my_agent.config.paths import get_data_dir, get_logs_dir
from my_agent.config.schema import Config

_LOG_SINK_ID: int | None = None
_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "key", "token", "secret", "password", "authorization")


def ensure_runtime_logging() -> Path:
    """Write server logs to the local runtime logs directory."""
    global _LOG_SINK_ID
    logs_dir = get_logs_dir()
    log_path = logs_dir / "server.log"
    if _LOG_SINK_ID is None:
        _LOG_SINK_ID = logger.add(
            log_path,
            rotation="10 MB",
            retention=10,
            encoding="utf-8",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
    return log_path


def runtime_info(config_path: Path | None, config: Config | None) -> dict[str, Any]:
    workspace_path = ""
    gateway_host = "127.0.0.1"
    gateway_port = 18790
    if config is not None:
        workspace_path = str(config.workspace_path)
        gateway_host = config.gateway.host
        gateway_port = config.gateway.port
    return {
        "surface": "browser",
        "app_version": "0.1.0",
        "engine_status": "ready",
        "data_dir": str(get_data_dir()),
        "logs_dir": str(get_logs_dir()),
        "config_path": str(config_path or ""),
        "workspace_path": workspace_path,
        "gateway_host": gateway_host,
        "gateway_port": gateway_port,
        "python": sys.executable,
        "engine_transport": "http",
    }


def open_logs() -> dict[str, Any]:
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    _open_path(logs_dir)
    return {"ok": True, "path": str(logs_dir)}


def export_diagnostics(
    *,
    config_path: Path | None,
    config: Config | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "runtime": runtime_info(config_path, config),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
        },
        "generated_at": int(time.time() * 1000),
    }
    if config is not None:
        payload["config"] = _redact(config.model_dump(mode="json", by_alias=True))
    if extra:
        payload["extra"] = _redact(extra)

    path = Path(tempfile.gettempdir()) / f"my-agent-diagnostics-{int(time.time() * 1000)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _show_in_folder(path)
    return {"ok": True, "path": str(path)}


def pick_folder() -> dict[str, Any]:
    selected = _pick_folder_native()
    return {"path": selected}


def _pick_folder_native() -> str | None:
    system = platform.system()
    if system == "Darwin" and shutil.which("osascript"):
        script = 'POSIX path of (choose folder with prompt "Select My-agent folder")'
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            return str(Path(value).expanduser().resolve()) if value else None
        return None

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            folder = filedialog.askdirectory(mustexist=False)
        finally:
            root.destroy()
        return str(Path(folder).expanduser().resolve()) if folder else None
    except Exception:
        logger.exception("Folder picker failed")
        return None


def _open_path(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(path)])
        return
    if system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, str(path)])
        return
    raise RuntimeError("No system opener is available.")


def _show_in_folder(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", "-R", str(path)])
        return
    if system == "Windows":
        subprocess.Popen(["explorer", "/select,", str(path)])
        return
    _open_path(path.parent)


def _redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    lowered = key.lower()
    if isinstance(value, str) and any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "***REDACTED***" if value else value
    return value
