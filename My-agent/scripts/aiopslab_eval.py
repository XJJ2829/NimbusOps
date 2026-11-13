#!/usr/bin/env python3
"""Evaluate whether the active AIOpsLab scenario has been solved."""

from __future__ import annotations

import argparse
import time

from aiopslab_common import (
    duration_since,
    load_aiopslab,
    load_state,
    maybe_reexec_with_aiopslab_python,
    parse_solution,
    print_json,
    resolve_aiopslab_home,
    save_state,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the active AIOpsLab scenario.")
    parser.add_argument(
        "--solution",
        help=(
            "Optional JSON/string solution for non-mitigation tasks. "
            "Mitigation tasks usually do not need this."
        ),
    )
    parser.add_argument("--aiopslab-home", default=None)
    args = parser.parse_args()

    state = load_state()
    aiopslab_home = resolve_aiopslab_home(args.aiopslab_home or state.get("aiopslab_home"))
    maybe_reexec_with_aiopslab_python(aiopslab_home)
    imports = load_aiopslab(aiopslab_home)

    registry = imports["ProblemRegistry"]()
    problem_id = state["problem_id"]
    problem = registry.get_problem_instance(problem_id)
    duration = duration_since(state.get("started_at"))
    solution = parse_solution(args.solution)

    trace = []
    if solution is not None:
        SessionItem = imports["SessionItem"]
        trace.append(SessionItem(role="assistant", content=f"terminal_solution={solution!r}"))

    results = problem.eval(solution, trace, duration)
    state["last_eval_at"] = time.time()
    state["last_eval_results"] = results
    save_state(state)

    print("\nEvaluation results:")
    print_json(results)
    if results.get("success") is True:
        print("\nResult: success=true, the scenario is considered solved.")
    elif results.get("success") is False:
        print("\nResult: success=false, the scenario is not solved yet.")
    else:
        print("\nResult: no boolean success field was returned by this AIOpsLab evaluator.")


if __name__ == "__main__":
    main()

