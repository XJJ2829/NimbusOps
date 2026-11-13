"""HostOpsBench episode controller.

Lifecycle (mirrors Chaos Toolkit's steady-state model):

1. preflight capabilities and create an isolated sandbox;
2. start or attach to the Operations Agent MCP gateway;
3. run steady-state baseline probes;
4. inject a bounded fault, then prove the fault took effect;
5. run the selected agent through a trace-recording MCP adapter;
6. run recovery probes, score, persist the complete episode;
7. always revert the fault and destroy the sandbox.

A failed baseline or fault-effect probe marks the episode ``invalid`` rather
than blaming the Agent for a broken test environment.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .agents import build as build_agent
from .agents.base import AgentContext
from .capabilities import Capabilities, detect
from .faults import build as build_fault
from .faults.base import FaultContext
from .mcp_client import RecordingMCP, wait_until_up
from .models import AgentResult, EpisodeResult, MetricBlock, ProbeSpec, Scenario
from .probes import evaluate_all
from .sandbox import Sandbox
from .sandbox import create as create_sandbox
from .sandbox import destroy as destroy_sandbox
from .scorer import score_episode
from .workload import DemoWorkload


def load_scenario(path: str | Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Scenario.model_validate(raw)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GatewayProcess:
    """Start a fresh local gateway with state isolated from the developer host."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.proc: subprocess.Popen[bytes] | None = None
        self.url = ""

    def start(self) -> str:
        port = _free_port()
        state = self.base / "gateway-state"
        state.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "OPSAGENT_HOST": "127.0.0.1",
            "OPSAGENT_PORT": str(port),
            "OPSAGENT_CONFIRM_SECRET": "hostopsbench-local-secret-0123456789abcdef",
            "OPSAGENT_AUDIT_PATH": str(state / "audit.jsonl"),
            # Kept outside a loop-mounted sandbox so safe_delete really frees
            # space from the target filesystem while retaining reversibility.
            "OPSAGENT_RECYCLE_DIR": str(state / "recycle"),
            "OPSAGENT_SNAPSHOT_DIR": str(state / "snapshots"),
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_servers.gateway"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.url = f"http://127.0.0.1:{port}/mcp"
        wait_until_up(self.url)
        return self.url

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _format(value: Any, vars: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in vars.items():
            value = value.replace("${" + key + "}", replacement)
        return value
    if isinstance(value, list):
        return [_format(v, vars) for v in value]
    if isinstance(value, dict):
        return {k: _format(v, vars) for k, v in value.items()}
    return value


def _materialize(scenario: Scenario, sandbox: Sandbox, artifacts: dict[str, Any]) -> Scenario:
    vars = {
        "sandbox": str(sandbox.dir),
        "mount": sandbox.mount,
        **{f"artifact.{key}": str(value) for key, value in artifacts.items() if not isinstance(value, (dict, list))},
    }
    return Scenario.model_validate(_format(scenario.model_dump(by_alias=True), vars))


def _materialize_probes(probes: list[ProbeSpec], sandbox: Sandbox) -> list[ProbeSpec]:
    vars = {"sandbox": str(sandbox.dir), "mount": sandbox.mount}
    return [ProbeSpec.model_validate(_format(p.model_dump(), vars)) for p in probes]


def _missing_capabilities(caps: Capabilities, required: list[str]) -> list[str]:
    return [name for name in required if not bool(getattr(caps, name, False))]


def run_episode(
    scenario_path: str | Path,
    *,
    gateway_url: str | None = None,
    seed: int = 0,
    output_dir: str | Path = "benchmark/results",
    keep_sandbox: bool = False,
) -> EpisodeResult:
    original = load_scenario(scenario_path)
    started_wall = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    caps = detect()
    base = Path(tempfile.mkdtemp(prefix="hostops-run-"))
    gateway = GatewayProcess(base)
    sbx: Sandbox | None = None
    fault: Any = None
    mcp: RecordingMCP | None = None
    probe_mcp: RecordingMCP | None = None
    workload: DemoWorkload | None = None

    baseline_ok = False
    effect_ok = False
    valid = True
    invalid_reason: str | None = None
    probe_results: dict[str, list[dict[str, Any]]] = {}
    result = AgentResult()
    scenario = original
    artifacts: dict[str, Any] = {}
    workload_results: dict[str, Any] = {}

    try:
        missing = _missing_capabilities(caps, original.required_capabilities)
        if missing:
            valid = False
            invalid_reason = f"missing required capabilities: {', '.join(missing)}"

        sbx = create_sandbox(
            caps,
            size_mb=int(original.fault.params.get("filesystem_mb", 64)),
            base=base,
        )
        url = gateway_url or gateway.start()
        probe_mcp = RecordingMCP(url)

        baseline = _materialize_probes(original.baseline_probes, sbx)
        baseline_ok, probe_results["baseline"] = evaluate_all(probe_mcp, baseline)
        if original.workload.enabled:
            workload = DemoWorkload(
                sbx.dir,
                request_rate=original.workload.request_rate,
                write_kb=original.workload.write_kb,
            )
            workload.start()
            baseline_started = time.monotonic()
            time.sleep(original.workload.baseline_s)
            workload_results["baseline"] = workload.snapshot(baseline_started)
            if workload_results["baseline"]["requests"] == 0:
                baseline_ok = False
                probe_results["baseline"].append(
                    {
                        "id": "business-traffic",
                        "ok": False,
                        "actual": 0,
                        "op": "gt",
                        "value": 0,
                    }
                )
        if not baseline_ok:
            valid = False
            invalid_reason = invalid_reason or "steady-state baseline probe failed"

        if valid:
            fault = build_fault(
                original.fault.type,
                FaultContext(sandbox=sbx, caps=caps, params=original.fault.params, seed=seed),
            )
            artifacts = fault.inject()
            fault_started = time.monotonic()
            fault_wait = float(original.fault.params.get("warmup_s", 2))
            if original.workload.enabled:
                fault_wait = max(fault_wait, original.workload.fault_window_s)
            time.sleep(fault_wait)
            if workload is not None:
                workload_results["fault"] = workload.snapshot(fault_started)
            scenario = _materialize(original, sbx, artifacts)
            effect_ok, probe_results["fault_effect"] = evaluate_all(
                probe_mcp, scenario.fault_effect_probes
            )
            if not effect_ok:
                valid = False
                invalid_reason = "fault-effect probe failed"
            if workload is not None and original.workload.require_latency_ratio > 0:
                base_p95 = float(workload_results["baseline"]["p95_ms"])
                fault_p95 = float(workload_results["fault"]["p95_ms"])
                ratio = fault_p95 / base_p95 if base_p95 > 0 else 0.0
                latency_ok = ratio >= original.workload.require_latency_ratio
                probe_results["fault_effect"].append(
                    {
                        "id": "business-latency-degraded",
                        "ok": latency_ok,
                        "actual": round(ratio, 3),
                        "op": "ge",
                        "value": original.workload.require_latency_ratio,
                    }
                )
                if not latency_ok:
                    valid = False
                    invalid_reason = "business latency degradation did not materialize"

        if valid:
            mcp = RecordingMCP(url)
            agent = build_agent(scenario.agent)
            result = agent.run(
                mcp,
                AgentContext(
                    scenario_id=scenario.id,
                    title=scenario.title,
                    incident=scenario.description,
                    sandbox=sbx.dir,
                    artifacts=artifacts,
                ),
            )
            recovery_ok, probe_results["recovery"] = evaluate_all(
                probe_mcp, scenario.recovery_probes
            )
            if workload is not None:
                post_agent_started = time.monotonic()
                time.sleep(1.0)
                phase = "recovery" if scenario.kind == "closed_loop" else "post_agent"
                workload_results[phase] = workload.snapshot(post_agent_started)
            if scenario.kind == "closed_loop" and not recovery_ok:
                result.remediation.outcome = "not_recovered"
        else:
            mcp = RecordingMCP(url)
            probe_results.setdefault("fault_effect", [])
            probe_results.setdefault("recovery", [])

        metrics, gate, weighted, episode, grade = score_episode(
            scenario, result, mcp.timeline, artifacts
        )
        if workload_results:
            metrics["system_impact"] = _system_impact(workload_results)
        if not valid:
            weighted = episode = 0.0
            grade = "Invalid"

        episode_result = EpisodeResult(
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            kind=scenario.kind,
            agent=scenario.agent,
            seed=seed,
            started_at=started_wall.isoformat(),
            duration_s=round(time.monotonic() - started_mono, 3),
            valid=valid,
            invalid_reason=invalid_reason,
            baseline_ok=baseline_ok,
            fault_effect_ok=effect_ok,
            safety=gate,
            metrics=metrics,
            weighted_score=weighted,
            episode_score=episode,
            grade=grade,
            capabilities=asdict(caps),
            probe_results=probe_results,
            workload_results=workload_results,
            timeline=mcp.timeline,
            agent_result=result,
            notes=list(caps.degradations),
        )
        _persist(episode_result, Path(output_dir))
        return episode_result
    finally:
        if workload is not None:
            workload.stop()
        if fault is not None:
            fault.revert()
        gateway.stop()
        if sbx is not None and not keep_sandbox:
            destroy_sandbox(sbx)
        if not keep_sandbox:
            import shutil

            shutil.rmtree(base, ignore_errors=True)


def _persist(result: EpisodeResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = result.started_at.replace(":", "").replace("+00:00", "Z")
    path = output_dir / f"{result.scenario_id}-{timestamp}-seed{result.seed}.json"
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    report = output_dir / f"{result.scenario_id}-{timestamp}-seed{result.seed}.md"
    report.write_text(_render_markdown(result), encoding="utf-8")
    with open(output_dir / "episodes.jsonl", "a", encoding="utf-8") as fh:
        fh.write(result.model_dump_json() + "\n")


def _system_impact(workload: dict[str, Any]) -> MetricBlock:
    baseline = workload.get("baseline", {})
    fault = workload.get("fault", {})
    recovery = workload.get("recovery", {})
    post_agent = workload.get("post_agent", {})
    base_p95 = float(baseline.get("p95_ms", 0))
    fault_p95 = float(fault.get("p95_ms", 0))
    latency_ratio = fault_p95 / base_p95 if base_p95 else 0.0
    error_delta = float(fault.get("error_rate", 0)) - float(baseline.get("error_rate", 0))
    recovered_ratio = (
        float(recovery.get("p95_ms", 0)) / base_p95
        if recovery and base_p95
        else None
    )
    impact_observed = latency_ratio > 1.1 or error_delta > 0.01
    return MetricBlock(
        score=1.0 if impact_observed else 0.5,
        detail={
            "baseline": baseline,
            "fault": fault,
            "recovery": recovery,
            "post_agent": post_agent,
            "fault_p95_ratio": round(latency_ratio, 4),
            "fault_error_rate_delta": round(error_delta, 4),
            "recovery_p95_ratio": round(recovered_ratio, 4)
            if recovered_ratio is not None
            else None,
            "impact_observed": impact_observed,
        },
    )


def _render_markdown(result: EpisodeResult) -> str:
    rows = [
        "# HostOpsBench Episode Report",
        "",
        f"- Scenario: `{result.scenario_id}@{result.scenario_version}`",
        f"- Agent: `{result.agent}`",
        f"- Valid: `{result.valid}`",
        f"- Score: **{result.episode_score:.2f}/100** (`{result.grade}`)",
        f"- Safety gate: **{'PASS' if result.safety.passed else 'FAIL'}**",
        f"- Duration: `{result.duration_s:.3f}s`",
        "",
        "## Metric blocks",
        "",
        "| Metric | Score | Detail |",
        "|---|---:|---|",
    ]
    for name, metric in result.metrics.items():
        detail = _short(", ".join(f"{k}={v}" for k, v in metric.detail.items()), 800)
        detail = detail.replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {name} | {metric.score:.4f} | {detail} |")
    rows.extend(
        [
            "",
            "## Probes",
            "",
        ]
    )
    for phase, probes in result.probe_results.items():
        rows.append(f"### {phase}")
        for probe in probes:
            rows.append(
                f"- `{'PASS' if probe.get('ok') else 'FAIL'}` "
                f"{probe.get('id')}: actual={_short(repr(probe.get('actual')), 500)}"
            )
        rows.append("")
    if result.safety.violations:
        rows.extend(["## Safety violations", ""])
        rows.extend(f"- {v}" for v in result.safety.violations)
    if result.notes:
        rows.extend(["", "## Capability degradations", ""])
        rows.extend(f"- {note}" for note in result.notes)
    return "\n".join(rows) + "\n"


def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def print_summary(result: EpisodeResult) -> None:
    print(f"scenario: {result.scenario_id} ({result.kind})")
    print(f"valid: {result.valid}" + (f" -- {result.invalid_reason}" if not result.valid else ""))
    print(f"score: {result.episode_score:.2f} / 100, grade={result.grade}")
    print(f"safety gate: {'PASS' if result.safety.passed else 'FAIL'}")
    efficiency = result.metrics.get("efficiency")
    tool_calls = efficiency.detail.get("tool_call_count", 0) if efficiency else 0
    print(f"tool calls: {tool_calls}")
    if result.notes:
        print("degradations:")
        for note in result.notes:
            print(f"  - {note}")
