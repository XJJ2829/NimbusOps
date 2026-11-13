#!/usr/bin/env python3
"""Clean up or reset the active AIOpsLab scenario."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import signal
import time
from typing import Any

from aiopslab_common import (
    clear_state,
    load_aiopslab,
    load_state,
    maybe_reexec_with_aiopslab_python,
    print_json,
    resolve_aiopslab_home,
    save_state,
)


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


def recover_fault(problem: Any, errors: list[str]) -> None:
    try:
        problem.recover_fault()
    except Exception as exc:
        errors.append(f"recover_fault: {exc}")


def cleanup_app(problem: Any, errors: list[str]) -> None:
    try:
        problem.app.cleanup()
    except Exception as exc:
        errors.append(f"app.cleanup: {exc}")


def teardown_platform(imports: dict[str, Any], errors: list[str]) -> None:
    try:
        imports["Prometheus"]().teardown()
    except Exception as exc:
        errors.append(f"prometheus.teardown: {exc}")

    kubectl = imports["KubeCtl"]()
    for command in (
        "kubectl delete namespace observe --ignore-not-found",
        "kubectl delete sc openebs-hostpath openebs-device --ignore-not-found",
        "kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml --ignore-not-found",
        "kubectl delete namespace openebs --ignore-not-found",
    ):
        try:
            kubectl.exec_command(command)
        except Exception as exc:
            errors.append(f"{command}: {exc}")

    for namespace in ("observe", "openebs"):
        try:
            kubectl.wait_for_namespace_deletion(namespace)
        except Exception as exc:
            errors.append(f"wait_for_namespace_deletion({namespace}): {exc}")


def reset_scenario(
    registry: Any,
    state: dict[str, Any],
    args: argparse.Namespace,
    errors: list[str],
) -> None:
    problem_id = state["problem_id"]
    cleanup_problem = registry.get_problem_instance(problem_id)

    if not args.skip_recover:
        recover_fault(cleanup_problem, errors)

    cleanup_errors_before = len(errors)
    cleanup_app(cleanup_problem, errors)
    if len(errors) > cleanup_errors_before:
        return

    reset_problem = registry.get_problem_instance(problem_id)
    try:
        reset_problem.app.deploy()
    except Exception as exc:
        errors.append(f"app.deploy: {exc}")
        return

    try:
        reset_problem.inject_fault()
    except Exception as exc:
        errors.append(f"inject_fault: {exc}")
        return

    workload_started = False
    workload_wait_timed_out = False
    if state.get("workload_started") and not args.no_workload:
        try:
            workload_wait_timed_out = run_workload(reset_problem, args.workload_timeout)
            workload_started = True
        except Exception as exc:
            errors.append(f"start_workload: {exc}")

    now = time.time()
    state["namespace"] = getattr(reset_problem, "namespace", state.get("namespace", ""))
    state["started_at"] = now
    state["reset_at"] = now
    state["reset_count"] = int(state.get("reset_count", 0)) + 1
    state["workload_started"] = workload_started
    state["workload_wait_timed_out"] = workload_wait_timed_out
    state.pop("last_eval_at", None)
    state.pop("last_eval_results", None)
    save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up or reset the active AIOpsLab scenario.")
    parser.add_argument("--aiopslab-home", default=None)
    parser.add_argument("--skip-recover", action="store_true", help="Do not call problem.recover_fault().")
    parser.add_argument("--keep-app", action="store_true", help="Do not delete the benchmark application namespace.")
    parser.add_argument("--keep-state", action="store_true", help="Keep the local scenario state file.")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Remove AIOpsLab platform components such as Prometheus and OpenEBS.",
    )
    parser.add_argument(
        "--reset-scenario",
        action="store_true",
        help="Delete and recreate the current scenario, then inject the same fault again.",
    )
    parser.add_argument(
        "--no-workload",
        action="store_true",
        help="With --reset-scenario, do not restart the workload even if the original scenario did.",
    )
    parser.add_argument(
        "--workload-timeout",
        type=int,
        default=0,
        help="With --reset-scenario, maximum seconds to wait for workload startup.",
    )
    args = parser.parse_args()

    if args.full and args.reset_scenario:
        parser.error("--full and --reset-scenario cannot be used together.")
    if args.reset_scenario and args.keep_app:
        parser.error("--reset-scenario cannot be used with --keep-app.")

    state = load_state()
    aiopslab_home = resolve_aiopslab_home(args.aiopslab_home or state.get("aiopslab_home"))
    maybe_reexec_with_aiopslab_python(aiopslab_home)
    imports = load_aiopslab(aiopslab_home)

    registry = imports["ProblemRegistry"]()
    errors: list[str] = []

    if args.reset_scenario:
        print("Resetting scenario resources. Platform infrastructure will be preserved.")
        reset_scenario(registry, state, args, errors)
        print_json({"mode": "reset-scenario", "cleaned": not errors, "errors": errors})
        return

    if args.full:
        print("Full cleanup enabled. Removing AIOpsLab platform components.")
    else:
        print("Cleaning scenario resources only. Platform infrastructure will be preserved.")

    problem = registry.get_problem_instance(state["problem_id"])

    if not args.skip_recover:
        recover_fault(problem, errors)

    if not args.keep_app:
        cleanup_app(problem, errors)

    if args.full and state.get("deployment") != "docker":
        teardown_platform(imports, errors)

    if not args.keep_state:
        clear_state()

    print_json(
        {
            "mode": "full" if args.full else "scenario",
            "cleaned": not errors,
            "platform_preserved": not args.full,
            "errors": errors,
        }
    )


if __name__ == "__main__":
    main()
