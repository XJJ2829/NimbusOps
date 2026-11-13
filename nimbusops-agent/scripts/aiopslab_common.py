#!/usr/bin/env python3
"""Shared helpers for terminal-driven AIOpsLab scenarios."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AIOPSLAB_HOME = PROJECT_ROOT.parent / "AIOpsLab-main"
STATE_DIR = PROJECT_ROOT / ".aiopslab-terminal"
STATE_FILE = STATE_DIR / "state.json"


def resolve_aiopslab_home(value: str | None = None) -> Path:
    raw = value or os.environ.get("AIOPSLAB_HOME") or str(DEFAULT_AIOPSLAB_HOME)
    return Path(raw).expanduser().resolve()


def maybe_reexec_with_aiopslab_python(aiopslab_home: Path) -> None:
    """Prefer AIOpsLab's own venv when it exists.

    The user can still run commands from My-agent's venv; this function swaps to
    AIOpsLab's Python process before importing AIOpsLab dependencies.
    """
    if os.environ.get("NIMBUSOPS_AIOPSLAB_REEXECED") == "1":
        return

    requested = os.environ.get("AIOPSLAB_PYTHON")
    if requested:
        target = Path(requested).expanduser().resolve()
        if not target.is_file():
            raise SystemExit(f"AIOPSLAB_PYTHON does not exist: {target}")
    else:
        target = aiopslab_home / ".venv" / "bin" / "python"
        if not target.is_file():
            return

    current = Path(sys.executable).resolve()
    if target == current:
        return

    env = os.environ.copy()
    env["NIMBUSOPS_AIOPSLAB_REEXECED"] = "1"
    env["AIOPSLAB_HOME"] = str(aiopslab_home)
    pythonpath_parts = [str(PROJECT_ROOT), str(aiopslab_home)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    os.execve(str(target), [str(target), *sys.argv], env)


def preflight(aiopslab_home: Path) -> None:
    errors: list[str] = []
    if not aiopslab_home.is_dir():
        errors.append(f"AIOpsLab directory not found: {aiopslab_home}")

    config = aiopslab_home / "aiopslab" / "config.yml"
    if not config.is_file():
        errors.append(
            "Missing AIOpsLab config: "
            f"{config}\n  Create it with: cp aiopslab/config.yml.example aiopslab/config.yml"
        )

    apps = aiopslab_home / "aiopslab-applications"
    if not apps.is_dir() or not any(apps.iterdir()):
        errors.append(
            "Missing benchmark applications under: "
            f"{apps}\n  Clone/fill the aiopslab-applications directory first."
        )

    if errors:
        raise SystemExit("\n\n".join(errors))


def load_aiopslab(aiopslab_home: Path) -> dict[str, Any]:
    preflight(aiopslab_home)
    if str(aiopslab_home) not in sys.path:
        sys.path.insert(0, str(aiopslab_home))
    os.chdir(aiopslab_home)

    try:
        from aiopslab.orchestrator.problems.registry import ProblemRegistry
        from aiopslab.service.kubectl import KubeCtl
        from aiopslab.service.telemetry.prometheus import Prometheus
        from aiopslab.session import SessionItem
    except Exception as exc:
        raise SystemExit(
            "Failed to import AIOpsLab. Use AIOpsLab's own Python environment "
            "or install its dependencies there.\n"
            f"Details: {exc}"
        ) from exc

    return {
        "ProblemRegistry": ProblemRegistry,
        "KubeCtl": KubeCtl,
        "Prometheus": Prometheus,
        "SessionItem": SessionItem,
    }


def save_state(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        raise SystemExit(
            "No active AIOpsLab scenario state found. "
            "Run: python scripts/aiopslab_start.py <problem_id>"
        )
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)
    try:
        STATE_DIR.rmdir()
    except OSError:
        pass


def parse_solution(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def infer_task_type(problem_id: str) -> str:
    for task_type in ("detection", "localization", "analysis", "mitigation"):
        if task_type in problem_id:
            return task_type
    return "unknown"


def duration_since(started_at: float | None) -> float:
    if not started_at:
        return 0.0
    return max(0.0, time.time() - float(started_at))


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))

