"""Command-line interface for HostOpsBench."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .capabilities import detect
from .controller import load_scenario, print_summary, run_episode
from .models import AgentResult, EpisodeResult, Scenario

SCENARIO_DIR = Path("benchmark/scenarios")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hostopsbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one benchmark episode")
    run.add_argument("scenario", type=Path)
    run.add_argument("--gateway-url", help="attach to an existing MCP gateway")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--output-dir", type=Path, default=Path("benchmark/results"))
    run.add_argument("--keep-sandbox", action="store_true")

    validate = sub.add_parser("validate", help="validate a scenario YAML")
    validate.add_argument("scenario", type=Path)

    sub.add_parser("preflight", help="show host capabilities and degradations")
    schema = sub.add_parser("schema", help="write the benchmark JSON Schemas")
    schema.add_argument("--output-dir", type=Path, default=Path("benchmark/schema"))

    _add_session_parser(sub)
    return parser


def _add_session_parser(sub: argparse._SubParsersAction) -> None:
    session = sub.add_parser(
        "session", help="interactive human-driven session (operate your own agent)"
    )
    ssub = session.add_subparsers(dest="session_command", required=True)

    start = ssub.add_parser("start", help="start a session and arm the first scenario")
    start.add_argument("scenarios", nargs="*", type=Path, help="scenario YAML files")
    start.add_argument("--all", action="store_true", help="use every built-in scenario in order")
    start.add_argument("--seed", type=int, default=0)
    start.add_argument("--gateway-url", help="attach to an existing gateway instead of starting one")
    start.add_argument(
        "--container",
        action="store_true",
        help="run the control plane (daemon+gateway+faults) inside an isolated "
        "container so the agent can only reach it via the gateway URL (Linux/Kylin)",
    )
    start.add_argument("--image", help="container image to use (default hostopsbench-session:local)")
    start.add_argument(
        "--no-privileged",
        action="store_true",
        help="run the session container without --privileged (live faults degrade)",
    )

    ssub.add_parser("status", help="show the current scenario brief and gateway URL")
    ssub.add_parser("brief", help="reprint the current scenario symptom brief")

    evaluate = ssub.add_parser("evaluate", help="score the current live environment state")
    evaluate.add_argument("--findings", help="optional AgentResult JSON to also grade diagnosis")

    ssub.add_parser("next", help="arm the next scenario")
    ssub.add_parser("abort", help="tear down the session without scoring")
    ssub.add_parser("report", help="print the aggregate scorecard for the session")

    daemon = ssub.add_parser("_daemon")  # hidden: internal daemon entrypoint
    daemon.add_argument("bootstrap")


def main() -> None:
    args = _parser().parse_args()
    if args.command == "preflight":
        caps = detect()
        for key, value in vars(caps).items():
            print(f"{key}: {value}")
        return
    if args.command == "schema":
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, model in (
            ("scenario", Scenario),
            ("agent-result", AgentResult),
            ("episode-result", EpisodeResult),
        ):
            (args.output_dir / f"{name}.schema.json").write_text(
                json.dumps(model.model_json_schema(), indent=2) + "\n",
                encoding="utf-8",
            )
        print(f"wrote schemas to {args.output_dir}")
        return
    if args.command == "validate":
        scenario = load_scenario(args.scenario)
        scenario.scoring.normalized()
        print(f"valid: {scenario.id}@{scenario.version}")
        return
    if args.command == "session":
        _run_session(args)
        return
    result = run_episode(
        args.scenario,
        gateway_url=args.gateway_url,
        seed=args.seed,
        output_dir=args.output_dir,
        keep_sandbox=args.keep_sandbox,
    )
    print_summary(result)
    if not result.valid or not result.safety.passed:
        raise SystemExit(2)


# --------------------------------------------------------------------------- #
# Interactive session command dispatch + presentation
# --------------------------------------------------------------------------- #
def _run_session(args: argparse.Namespace) -> None:
    from . import session as sess

    command = args.session_command
    if command == "_daemon":
        sess.run_daemon(args.bootstrap)
        return
    if command == "start":
        scenarios = _resolve_scenarios(args)
        status = sess.start_session(
            scenarios,
            seed=args.seed,
            gateway_url=args.gateway_url,
            container=args.container,
            image=args.image,
            privileged=not args.no_privileged,
        )
        _print_started(status)
        return
    if command == "status":
        _print_status(sess.cmd_status())
        return
    if command == "brief":
        status = sess.cmd_status()
        brief = status.get("brief")
        if brief:
            _print_brief(brief)
        else:
            print("当前没有已装载的场景；运行 `session next` 装载下一个场景。")
        return
    if command == "next":
        result = sess.cmd_next()
        if result.get("finished"):
            print("所有场景已完成。运行 `session report` 查看汇总，或 `session abort` 结束会话。")
            return
        _print_brief(result["brief"])
        return
    if command == "evaluate":
        _print_scorecard(sess.cmd_evaluate(args.findings))
        return
    if command == "abort":
        sess.cmd_abort()
        print("会话已中止，故障已回滚、沙箱已清理。")
        return
    if command == "report":
        _print_report(sess.cmd_report())
        return


def _resolve_scenarios(args: argparse.Namespace) -> list[Path]:
    if args.all:
        scenarios = sorted(SCENARIO_DIR.glob("*.yaml"))
        if not scenarios:
            raise SystemExit(f"no scenarios found under {SCENARIO_DIR}")
        return scenarios
    if args.scenarios:
        return list(args.scenarios)
    raise SystemExit("provide scenario files or --all")


def _rule(char: str = "-") -> str:
    return char * 64


def _print_started(status: dict[str, Any]) -> None:
    print(_rule("="))
    print(f"会话已启动：{status.get('session_id', '')}  网关 URL: {status.get('gateway_url', '')}")
    print(_rule("="))
    brief = status.get("brief")
    if brief:
        _print_brief(brief)
    print("\n把你的 Agent 指向上面的网关 URL 进行排查/修复。")
    print("处理完成后运行 `python -m benchmark session evaluate` 打分。")


def _print_brief(brief: dict[str, Any]) -> None:
    print(_rule())
    print(f"场景 {brief.get('position', '')}  [{brief.get('scenario_id', '')}]  模式: {brief.get('mode', '')}")
    print(f"标题: {brief.get('title', '')}")
    print(_rule())
    print("【现象】")
    print(f"  {brief.get('symptom', '')}")
    print(f"【要求】{brief.get('expectation', '')}")
    print(
        f"【预算】工具调用 {brief.get('tool_budget', '?')} 次 / 时间 {brief.get('time_budget_s', '?')}s"
    )
    print(f"【网关】{brief.get('gateway_url', '')}")
    if not brief.get("setup_ok", True):
        print("⚠ 环境未就绪（基线/故障探针或能力检查未通过），评分可能记为 Invalid；")
        print("  live 场景需在 Linux/麒麟 主机运行。")


def _grade_tag(card: dict[str, Any]) -> str:
    return f"{card.get('episode_score', 0):.2f}/100  ({card.get('grade', '?')})"


def _print_scorecard(card: dict[str, Any]) -> None:
    if card.get("error"):
        print(f"错误：{card['error']}")
        return
    print(_rule("="))
    print(f"评分：{card.get('title', '')}  [{card.get('scenario_id', '')}]  模式: {card.get('mode', '')}")
    print(_rule("="))
    print(f"综合得分: {_grade_tag(card)}    加权原始分: {card.get('weighted_score', 0):.2f}/100")
    print(f"安全门槛: {'PASS' if card.get('safety_passed') else 'FAIL'}")
    if card.get("safety_violations"):
        for v in card["safety_violations"]:
            print(f"  - 违规: {v}")
    if not card.get("valid", True):
        print(f"有效性: Invalid — {card.get('invalid_reason', '')}")
    print("\n各维度:")
    weights = card.get("weights", {})
    for name, block in card.get("metrics", {}).items():
        w = weights.get(name)
        wtxt = f"  权重 {w:.2f}" if isinstance(w, (int, float)) else ""
        print(f"  - {name:<11} {block.get('score', 0):.4f}{wtxt}")
    print(f"\n审计到的工具调用: {card.get('audited_tool_calls', 0)}  用时: {card.get('duration_s', 0)}s")
    if card.get("has_next"):
        print("\n运行 `python -m benchmark session next` 进入下一个场景。")
    else:
        print("\n这是最后一个场景。运行 `python -m benchmark session report` 查看汇总。")


def _print_status(status: dict[str, Any]) -> None:
    print(
        f"会话 {status.get('session_id', '')}  进度 {status.get('index', -1) + 1}/{status.get('total', 0)}  "
        f"状态 {status.get('status', '')}"
    )
    print(f"网关 URL: {status.get('gateway_url', '')}")
    brief = status.get("brief")
    if brief:
        _print_brief(brief)
    results = status.get("results", [])
    if results:
        print("\n已完成:")
        for card in results:
            print(f"  - {card.get('scenario_id', '')}: {_grade_tag(card)}")


def _print_report(report: dict[str, Any]) -> None:
    results = report.get("results", [])
    if not results:
        print(report.get("note", "暂无评分记录。"))
        return
    print(_rule("="))
    print(f"HostOpsBench 交互式评测汇总  会话 {report.get('session_id', '')}")
    print(_rule("="))
    scored = [c for c in results if c.get("valid", True)]
    for card in results:
        tag = "Invalid" if not card.get("valid", True) else _grade_tag(card)
        print(f"  {card.get('scenario_id', ''):<32} {tag}")
    if scored:
        avg = sum(c.get("episode_score", 0) for c in scored) / len(scored)
        print(_rule())
        print(f"有效场景 {len(scored)}/{len(results)}    平均分 {avg:.2f}/100")


if __name__ == "__main__":
    main()
