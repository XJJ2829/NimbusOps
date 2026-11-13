#!/usr/bin/env python3
"""Start a real AIOpsLab fault scenario and leave it running."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import signal
import time
from pathlib import Path
from typing import Any

from aiopslab_common import (
    STATE_FILE,
    infer_task_type,
    load_aiopslab,
    maybe_reexec_with_aiopslab_python,
    print_json,
    resolve_aiopslab_home,
    save_state,
)


DEFAULT_PROBLEM = "misconfig_app_hotel_res-mitigation-1"


class WorkloadTimeout(Exception):
    pass


def _raise_workload_timeout(signum: int, frame: Any) -> None:
    raise WorkloadTimeout


def run_workload(problem: Any, timeout_seconds: int) -> bool:
    if timeout_seconds <= 0:
        if inspect.iscoroutinefunction(problem.start_workload):
            asyncio.run(problem.start_workload())
        else:
            problem.start_workload()
        return False

    previous_handler = signal.signal(signal.SIGALRM, _raise_workload_timeout)
    signal.alarm(timeout_seconds)
    try:
        if inspect.iscoroutinefunction(problem.start_workload):
            asyncio.run(problem.start_workload())
        else:
            problem.start_workload()
        return False
    except WorkloadTimeout:
        print(
            f"\nWorkload wait timed out after {timeout_seconds}s. "
            "Continuing because the workload job has already been requested."
        )
        return True
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start an AIOpsLab fault scenario.")
    parser.add_argument(
        "problem_id",
        nargs="?",
        default=DEFAULT_PROBLEM,
        help=f"AIOpsLab problem id. Default: {DEFAULT_PROBLEM}",
    )
    parser.add_argument("--aiopslab-home", default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite existing local scenario state.")
    parser.add_argument("--no-delete-first", action="store_true", help="Do not delete app resources before deploy.")
    parser.add_argument("--no-workload", action="store_true", help="Deploy and inject fault without starting workload.")
    parser.add_argument(
        "--workload-timeout",
        type=int,
        default=0,
        help="Maximum seconds to wait for AIOpsLab workload startup. Default 0 disables the timeout.",
    )
    parser.add_argument(
        "--skip-platform-setup",
        action="store_true",
        help="Skip OpenEBS/Prometheus setup for Kubernetes scenarios.",
    )
    args = parser.parse_args()

    if STATE_FILE.exists() and not args.force:
        raise SystemExit(
            f"Existing scenario state found at {STATE_FILE}.\n"
            "Run cleanup first, or pass --force if you intentionally want to overwrite it."
        )

    aiopslab_home = resolve_aiopslab_home(args.aiopslab_home)
    maybe_reexec_with_aiopslab_python(aiopslab_home)
    imports = load_aiopslab(aiopslab_home)

    registry = imports["ProblemRegistry"]()
    if registry.get_problem(args.problem_id) is None:
        available = "\n".join(registry.get_problem_ids(task_type="mitigation"))
        raise SystemExit(f"Problem not found: {args.problem_id}\n\nMitigation problems:\n{available}")

    deployment = registry.get_problem_deployment(args.problem_id)
    problem = registry.get_problem_instance(args.problem_id)
    namespace = getattr(problem, "namespace", "")
    prometheus_deployed = False

    print(f"Starting AIOpsLab scenario: {args.problem_id}")
    print(f"AIOpsLab home: {aiopslab_home}")
    print(f"Deployment: {deployment}")
    if namespace:
        print(f"Namespace: {namespace}")

    if deployment != "docker" and not args.skip_platform_setup:
        print("\n== Platform Setup ==")
        kubectl = imports["KubeCtl"]()
        kubectl.exec_command("kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml")
        kubectl.exec_command(
            "kubectl patch storageclass openebs-hostpath -p "
            "'{\"metadata\": {\"annotations\":{\"storageclass.kubernetes.io/is-default-class\":\"true\"}}}'"
        )
        kubectl.wait_for_ready("openebs")
        prometheus = imports["Prometheus"]()
        prometheus.deploy()
        prometheus_deployed = True

    print("\n== Deploy Application ==")
    if not args.no_delete_first:
        problem.app.delete()
    problem.app.deploy()

    print("\n== Inject Fault ==")
    problem.inject_fault()

    workload_started = False
    workload_wait_timed_out = False
    if not args.no_workload:
        print("\n== Start Workload ==")
        workload_wait_timed_out = run_workload(problem, args.workload_timeout)
        workload_started = True

    state = {
        "problem_id": args.problem_id,
        "task_type": infer_task_type(args.problem_id),
        "deployment": deployment,
        "namespace": namespace,
        "aiopslab_home": str(aiopslab_home),
        "started_at": time.time(),
        "prometheus_deployed": prometheus_deployed,
        "workload_started": workload_started,
        "workload_wait_timed_out": workload_wait_timed_out,
    }
    save_state(state)

    print("\nAIOpsLab fault scenario is running.")
    print(f"State saved to: {STATE_FILE}")
    print("\nAsk NimbusOps something like:")
    print(
        "当前运行环境存在服务故障，请排查根因并完成修复，使服务恢复正常。"
        "不要假设故障类型，先观察系统状态、日志和事件；修复后说明你做了什么。"
    )
    print("\nAfter NimbusOps finishes, evaluate with:")
    print("python scripts/aiopslab_eval.py")
    print("\nCleanup when done:")
    print("python scripts/aiopslab_cleanup.py")
    print("\nReset the same scenario for another demo round:")
    print("python scripts/aiopslab_cleanup.py --reset-scenario")
    print("\nFull platform cleanup when you need a fresh environment:")
    print("python scripts/aiopslab_cleanup.py --full")
    print("\nScenario state:")
    print_json(state)


if __name__ == "__main__":
    main()
