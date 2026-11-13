"""Interactive, human-driven benchmark sessions.

The default :func:`benchmark.controller.run_episode` runs the whole lifecycle in
one process with a *scripted* reference agent. This module instead lets a human
operator drive *their own* agent against a live gateway:

    start  -> arm scenario 1: sandbox + gateway + injected fault, print a
              symptom-only brief and the gateway MCP URL;
    (operator troubleshoots / remediates with their own agent);
    evaluate -> grade the live environment state + audit log, print a scorecard,
              revert the fault and destroy the sandbox;
    next   -> arm the following scenario;
    ...    -> repeat until every scenario is done; report the aggregate.

Because a fault can only be reverted by the process that injected it, the moving
parts (gateway process, sandbox, fault object) are owned by a small long-lived
**session daemon**. The ``session`` CLI subcommands are thin clients that talk to
the daemon over a localhost-only control endpoint, so the operator gets discrete
commands without losing the ability to roll faults back.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .capabilities import detect
from .controller import (
    GatewayProcess,
    _materialize,
    _materialize_probes,
    _missing_capabilities,
    load_scenario,
)
from .faults import build as build_fault
from .faults.base import FaultContext
from .mcp_client import RecordingMCP
from .models import AgentResult
from .probes import evaluate_all
from .sandbox import create as create_sandbox
from .sandbox import destroy as destroy_sandbox
from .session_scorer import load_audit, resolve_plan, score_state

SESSIONS_ROOT = Path("benchmark/.sessions")
ACTIVE_FILE = SESSIONS_ROOT / "active.json"


# --------------------------------------------------------------------------- #
# Session runner (owned by the daemon)
# --------------------------------------------------------------------------- #
class SessionRunner:
    """Owns the gateway, and the current scenario's sandbox + injected fault."""

    def __init__(
        self,
        session_id: str,
        scenarios: list[Path],
        session_dir: Path,
        *,
        seed: int = 0,
        gateway_url: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.scenarios = scenarios
        self.session_dir = session_dir
        self.seed = seed
        self.caps = detect()
        self.index = -1
        self.status = "idle"
        self.current: dict[str, Any] | None = None
        self.results: list[dict[str, Any]] = []
        self.lock = threading.Lock()

        self.owns_gateway = gateway_url is None
        if self.owns_gateway:
            self._gateway = GatewayProcess(session_dir)
            self.gateway_url = self._gateway.start()
        else:
            self._gateway = None
            self.gateway_url = gateway_url
        self.audit_path = session_dir / "gateway-state" / "audit.jsonl"
        self.results_path = session_dir / "results.jsonl"
        self._stop_reaper = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    # -- child reaping --------------------------------------------------- #
    def _reap(self) -> None:
        """Reap fault-worker children the operator has killed via the gateway.

        The fault workers are children of this daemon; a gateway ``kill_process``
        leaves them as zombies until their parent waits on them. Polling the
        ``Popen`` handles reaps them cleanly so ``/proc/<pid>`` disappears and the
        recovery probes (``available: falsy``) reflect the operator's fix.
        """
        cur = self.current
        fault = cur.get("fault") if cur else None
        if fault is None:
            return
        procs = list(getattr(fault, "_procs", []) or [])
        single = getattr(fault, "_proc", None)
        if single is not None:
            procs.append(single)
        for proc in procs:
            try:
                proc.poll()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def _reap_loop(self) -> None:
        while not self._stop_reaper.wait(1.0):
            self._reap()

    # -- lifecycle ------------------------------------------------------- #
    def _audit_len(self) -> int:
        if not self.audit_path.exists():
            return 0
        with self.audit_path.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)

    def arm_next(self) -> dict[str, Any]:
        with self.lock:
            if self.current is not None:
                self._teardown_current()
            if self.index + 1 >= len(self.scenarios):
                self.status = "finished"
                return {"finished": True, **self._progress()}

            self.index += 1
            path = self.scenarios[self.index]
            original = load_scenario(path)
            missing = _missing_capabilities(self.caps, original.required_capabilities)

            sbx = create_sandbox(
                self.caps,
                size_mb=int(original.fault.params.get("filesystem_mb", 64)),
                base=self.session_dir / f"sandbox-{self.index}",
            )
            probe_mcp = RecordingMCP(self.gateway_url)
            baseline = _materialize_probes(original.baseline_probes, sbx)
            baseline_ok, _ = evaluate_all(probe_mcp, baseline)

            fault = build_fault(
                original.fault.type,
                FaultContext(
                    sandbox=sbx, caps=self.caps, params=original.fault.params, seed=self.seed
                ),
            )
            artifacts = fault.inject()
            time.sleep(float(original.fault.params.get("warmup_s", 2)))
            scenario_mat = _materialize(original, sbx, artifacts)
            effect_ok, _ = evaluate_all(probe_mcp, scenario_mat.fault_effect_probes)

            self.current = {
                "path": str(path),
                "original": original,
                "scenario": scenario_mat,
                "sandbox": sbx,
                "fault": fault,
                "artifacts": artifacts,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "baseline_ok": baseline_ok,
                "effect_ok": effect_ok,
                "arm_offset": self._audit_len(),
                "missing_caps": missing,
            }
            self.status = "armed"
            return {"finished": False, "brief": self.brief(), **self._progress()}

    def _teardown_current(self) -> None:
        cur = self.current
        if cur is None:
            return
        try:
            cur["fault"].revert()
        except Exception:  # noqa: BLE001 - best-effort, teardown must not raise
            pass
        try:
            destroy_sandbox(cur["sandbox"])
        except Exception:  # noqa: BLE001
            pass
        self.current = None

    def evaluate(self, findings_path: str | None = None) -> dict[str, Any]:
        with self.lock:
            if self.current is None:
                return {"error": "no scenario is armed; run `session next` first"}
            self._reap()
            cur = self.current
            scenario = cur["scenario"]

            eval_offset = self._audit_len()
            records = load_audit(self.audit_path)[cur["arm_offset"] : eval_offset]

            findings = self._load_findings(findings_path)
            mcp = RecordingMCP(self.gateway_url)
            card = score_state(scenario, mcp, records, cur["artifacts"], findings)

            card["title"] = scenario.title
            card["baseline_ok"] = cur["baseline_ok"]
            card["fault_effect_ok"] = cur["effect_ok"]
            card["duration_s"] = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(cur["started_at"]))
                .total_seconds(),
                1,
            )
            if cur["missing_caps"]:
                card["valid"] = False
                card["invalid_reason"] = (
                    f"missing required capabilities: {', '.join(cur['missing_caps'])}"
                )
                card["episode_score"] = 0.0
                card["grade"] = "Invalid"
            elif not (cur["baseline_ok"] and cur["effect_ok"]):
                card["valid"] = False
                card["invalid_reason"] = (
                    "baseline probe failed" if not cur["baseline_ok"] else "fault-effect probe failed"
                )
                card["episode_score"] = 0.0
                card["grade"] = "Invalid"

            self.results.append(card)
            self._persist_result(card)
            self._teardown_current()
            self.status = "evaluated"
            card["progress"] = self._progress()
            card["has_next"] = self.index + 1 < len(self.scenarios)
            return card

    def abort(self) -> dict[str, Any]:
        with self.lock:
            self._teardown_current()
            self.status = "aborted"
            return {"aborted": True, **self._progress()}

    def close(self) -> None:
        self._stop_reaper.set()
        self._teardown_current()
        if self.owns_gateway and self._gateway is not None:
            self._gateway.stop()

    # -- read-only views ------------------------------------------------- #
    def _load_findings(self, findings_path: str | None) -> AgentResult | None:
        if not findings_path:
            return None
        raw = json.loads(Path(findings_path).read_text(encoding="utf-8"))
        return AgentResult.model_validate(raw)

    def _persist_result(self, card: dict[str, Any]) -> None:
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        with self.results_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")

    def _progress(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "total": len(self.scenarios),
            "status": self.status,
            "gateway_url": self.gateway_url,
        }

    def brief(self) -> dict[str, Any]:
        cur = self.current
        if cur is None:
            return {}
        scenario = cur["scenario"]
        symptom = scenario.symptom or (
            f"{scenario.title}：系统出现异常，请用你的 Agent 排查（不透露根因）。"
        )
        mode = resolve_plan(scenario).mode
        return {
            "scenario_id": scenario.id,
            "title": scenario.title,
            "symptom": symptom,
            "mode": mode,
            "expectation": (
                "定位并修复问题，使系统恢复健康状态。" if mode == "remediation"
                else "只读排查并定位根因；请勿 kill/重启/改配置/删文件。"
            ),
            "tool_budget": scenario.tool_budget,
            "time_budget_s": scenario.time_budget_s,
            "gateway_url": self.gateway_url,
            "position": f"{cur_index_display(self.index)}/{len(self.scenarios)}",
            "setup_ok": bool(cur["baseline_ok"] and cur["effect_ok"] and not cur["missing_caps"]),
        }

    def status_info(self) -> dict[str, Any]:
        info = {"session_id": self.session_id, **self._progress()}
        if self.current is not None:
            info["brief"] = self.brief()
        info["results"] = self.results
        return info


def cur_index_display(index: int) -> int:
    return index + 1 if index >= 0 else 0


# --------------------------------------------------------------------------- #
# Control server (daemon side)
# --------------------------------------------------------------------------- #
class _ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    runner: SessionRunner
    token: str
    should_exit: bool = False


class _ControlHandler(BaseHTTPRequestHandler):
    server: _ControlServer

    def log_message(self, *args: Any) -> None:  # noqa: D401 - silence access log
        return

    def _auth_ok(self) -> bool:
        return self.headers.get("X-Session-Token") == self.server.token

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        if self.path.rstrip("/") == "/status":
            self._send(200, self.server.runner.status_info())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        action = body.get("action")
        runner = self.server.runner
        try:
            if action == "arm":
                self._send(200, runner.arm_next())
            elif action == "evaluate":
                self._send(200, runner.evaluate(body.get("findings")))
            elif action == "report":
                self._send(200, {"results": runner.results, **runner._progress()})
            elif action == "abort":
                result = runner.abort()
                self._send(200, result)
                self._shutdown()
            else:
                self._send(400, {"error": f"unknown action: {action}"})
        except Exception as exc:  # noqa: BLE001 - report to client, keep daemon alive
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _shutdown(self) -> None:
        self.server.should_exit = True
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def run_daemon(bootstrap_path: str) -> None:
    """Daemon entrypoint (invoked as ``session _daemon <bootstrap.json>``)."""
    boot = json.loads(Path(bootstrap_path).read_text(encoding="utf-8"))
    session_dir = Path(boot["session_dir"])
    session_dir.mkdir(parents=True, exist_ok=True)
    token = boot["token"]

    runner: SessionRunner | None = None
    try:
        runner = SessionRunner(
            boot["session_id"],
            [Path(p) for p in boot["scenarios"]],
            session_dir,
            seed=int(boot.get("seed", 0)),
            gateway_url=boot.get("gateway_url"),
        )
        server = _ControlServer(("127.0.0.1", 0), _ControlHandler)
        server.runner = runner
        server.token = token
        control_url = f"http://127.0.0.1:{server.server_address[1]}"

        arm = runner.arm_next()
        _write_active(
            {
                "session_id": boot["session_id"],
                "session_dir": str(session_dir),
                "control_url": control_url,
                "token": token,
                "gateway_url": runner.gateway_url,
                "pid": _pid(),
                "container": boot.get("container"),
                "scenarios": boot["scenarios"],
                "status": "armed" if not arm.get("finished") else "finished",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        server.serve_forever()
    except Exception as exc:  # noqa: BLE001 - surface startup failure to the client
        _write_active({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if runner is not None:
            runner.close()
        _clear_active()


def _pid() -> int:
    import os

    return os.getpid()


def _write_active(payload: dict[str, Any]) -> None:
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_active() -> None:
    try:
        ACTIVE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Client side (CLI subcommands call these)
# --------------------------------------------------------------------------- #
def _read_active() -> dict[str, Any] | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _call(active: dict[str, Any], action: str, **extra: Any) -> dict[str, Any]:
    req = urllib.request.Request(
        active["control_url"],
        data=json.dumps({"action": action, **extra}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Session-Token": active["token"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 - localhost only
        return json.loads(resp.read())


def _get_status(active: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        active["control_url"] + "/status",
        headers={"X-Session-Token": active["token"]},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost only
        return json.loads(resp.read())


def start_session(
    scenarios: list[Path],
    *,
    seed: int = 0,
    gateway_url: str | None = None,
    container: bool = False,
    image: str | None = None,
    privileged: bool = True,
) -> dict[str, Any]:
    existing = _read_active()
    if existing and existing.get("status") not in ("finished", "error", "aborted"):
        if _daemon_alive(existing):
            raise SystemExit(
                "a session is already active; run `session status`, `session abort`, "
                "or finish it first."
            )
    # a stale/finished session may have left its container behind
    if existing:
        _teardown_container(existing)
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    session_dir = SESSIONS_ROOT / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    container_name = f"hostopsbench-{session_id}" if container else None
    bootstrap = session_dir / "bootstrap.json"
    bootstrap.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "session_dir": str(session_dir),
                "scenarios": [str(p) for p in scenarios],
                "seed": seed,
                "gateway_url": gateway_url,
                "token": token,
                "container": container_name,
            }
        ),
        encoding="utf-8",
    )
    _clear_active()
    if container:
        _start_container_daemon(
            container_name, bootstrap, session_dir, image=image, privileged=privileged
        )
    else:
        log = (session_dir / "daemon.log").open("ab")
        subprocess.Popen(
            [sys.executable, "-m", "benchmark", "session", "_daemon", str(bootstrap)],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return _wait_for_ready(timeout=300.0 if container else 180.0)


def _start_container_daemon(
    name: str,
    bootstrap: Path,
    session_dir: Path,
    *,
    image: str | None,
    privileged: bool,
) -> None:
    import os

    from . import container as ctr

    cli = ctr.require_runtime()
    python_bin = os.environ.get("HOSTOPS_SESSION_PYTHON", "python3")
    base = image or os.environ.get("HOSTOPS_SESSION_BASE")
    forced_build = os.environ.get("HOSTOPS_SESSION_BUILD", "").lower() in ("1", "true", "yes")
    forced_run = os.environ.get("HOSTOPS_SESSION_RUN_BASE", "").lower() in ("1", "true", "yes")
    # Run the base image *directly* (no build) when told to, or -- to spare the
    # common niche/offline case an extra flag -- whenever a base image is given
    # that already exists locally (e.g. a self-imported host-rootfs image on
    # LoongArch Kylin that already carries python + tools + deps). A plain
    # ``--container`` with no base still builds the derived image.
    run_base = forced_run or (
        not forced_build and base is not None and ctr.image_exists(cli, base)
    )
    if run_base:
        if not base:
            raise ctr.ContainerError(
                "run-base mode needs --image or HOSTOPS_SESSION_BASE set to the "
                "ready-to-run base image."
            )
        if not ctr.image_exists(cli, base):
            raise ctr.ContainerError(
                f"base image {base!r} not found locally; build it first, e.g.\n"
                f"  sudo tar --numeric-owner --exclude='./proc' --exclude='./sys' "
                f"--exclude='./dev' -C / -c . | {cli} import - {base}"
            )
        img = base
    else:
        img = base or ctr.IMAGE
        ctr.build_image(cli, img, context=Path.cwd())
    ctr.run_daemon(
        cli,
        name=name,
        bootstrap=bootstrap,
        repo=Path.cwd(),
        confirm_secret=secrets.token_hex(16),
        image=img,
        privileged=privileged,
        python_bin=python_bin if run_base else "python",
    )


def _teardown_container(active: dict[str, Any]) -> None:
    """Remove the session container recorded in ``active`` (best effort)."""
    name = active.get("container")
    if not name:
        return
    try:
        from . import container as ctr

        cli = ctr.runtime()
        if cli is not None:
            ctr.remove(cli, name)
    except Exception:
        pass


def _wait_for_ready(timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        active = _read_active()
        if active and active.get("status") in ("armed", "finished", "error"):
            if active.get("status") == "error":
                raise SystemExit(f"session failed to start: {active.get('error')}")
            return _get_status(active)
        time.sleep(0.5)
    raise SystemExit("timed out waiting for the session daemon to arm the first scenario")


def _daemon_alive(active: dict[str, Any]) -> bool:
    try:
        _get_status(active)
        return True
    except (urllib.error.URLError, OSError):
        return False


def require_active() -> dict[str, Any]:
    active = _read_active()
    if not active or not _daemon_alive(active):
        raise SystemExit("no active session; run `session start` first.")
    return active


def cmd_status() -> dict[str, Any]:
    return _get_status(require_active())


def cmd_next() -> dict[str, Any]:
    return _call(require_active(), "arm")


def cmd_evaluate(findings: str | None = None) -> dict[str, Any]:
    return _call(require_active(), "evaluate", findings=findings)


def cmd_abort() -> dict[str, Any]:
    active = require_active()
    try:
        result = _call(active, "abort")
    finally:
        _teardown_container(active)
    return result


def cmd_report() -> dict[str, Any]:
    active = _read_active()
    if active and _daemon_alive(active):
        return _call(active, "report")
    return _report_from_disk()


def _report_from_disk() -> dict[str, Any]:
    if not SESSIONS_ROOT.exists():
        return {"results": [], "note": "no sessions found"}
    sessions = sorted(
        (p for p in SESSIONS_ROOT.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for sess in sessions:
        results_file = sess / "results.jsonl"
        if results_file.exists():
            results = [
                json.loads(line)
                for line in results_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return {"results": results, "session_id": sess.name, "from_disk": True}
    return {"results": [], "note": "no results recorded yet"}
