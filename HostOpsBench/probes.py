"""Probe evaluation: turn a declarative :class:`ProbeSpec` into a pass/fail.

A probe names an MCP ``call`` (a tool name, or ``resource:<uri>``), extracts a
value from the JSON result via a small dotted/indexed path language, and applies
a comparison operator. This keeps the baseline / fault-effect / recovery checks
fully machine-verifiable and identical in shape to the ground-truth evidence
matchers.
"""
from __future__ import annotations

import re
from typing import Any

from .mcp_client import RecordingMCP
from .models import EvidenceTruth, ProbeSpec

_INDEX = re.compile(r"^(.*?)\[(\d+|\*)\]$")


def extract(data: Any, path: str) -> Any:
    """Resolve a dotted path with optional ``[i]`` / ``[*]`` list access.

    ``partitions[*].use_percent`` returns the list of ``use_percent`` values;
    ``files[0].path`` returns a single value. Missing keys yield ``None``.
    """
    if path in ("", "."):
        return data
    parts = path.split(".")

    def walk(cur: Any, offset: int) -> Any:
        if offset >= len(parts):
            return cur
        raw = parts[offset]
        m = _INDEX.match(raw)
        key = m.group(1) if m else raw
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if m:
            idx = m.group(2)
            if not isinstance(cur, list):
                return None
            if idx == "*":
                if offset == len(parts) - 1:
                    return cur
                return [walk(item, offset + 1) for item in cur]
            i = int(idx)
            if i >= len(cur):
                return None
            cur = cur[i]
        return walk(cur, offset + 1)

    return walk(data, 0)


def _as_number(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compare(actual: Any, op: str, expected: Any) -> bool:
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    if op == "contains":
        return _contains(actual, expected)
    if op == "not_contains":
        return not _contains(actual, expected)
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    a, e = _as_number(actual), _as_number(expected)
    if a is None or e is None:
        return False
    return {
        "gt": a > e,
        "ge": a >= e,
        "lt": a < e,
        "le": a <= e,
    }[op]


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return str(expected) in actual
    if isinstance(actual, (list, tuple, set)):
        return expected in actual or any(
            isinstance(x, str) and str(expected) in x for x in actual
        )
    if isinstance(actual, dict):
        return expected in actual
    return False


def _invoke(mcp: RecordingMCP, call: str, args: dict[str, Any]) -> Any:
    if call.startswith("resource:"):
        return mcp.read_resource(call[len("resource:") :])
    return mcp.call(call, args)


def evaluate_probe(mcp: RecordingMCP, probe: ProbeSpec) -> tuple[bool, Any]:
    try:
        result = _invoke(mcp, probe.call, probe.args)
    except Exception:  # noqa: BLE001 - a failed call is a failed probe
        return False, None
    actual = extract(result, probe.path)
    if probe.op in ("gt", "ge", "lt", "le") and isinstance(actual, list):
        nums = [n for n in (_as_number(x) for x in actual) if n is not None]
        ok = any(compare(n, probe.op, probe.value) for n in nums)
        return ok, actual
    ok = compare(actual, probe.op, probe.value)
    return ok, actual


def evaluate_all(mcp: RecordingMCP, probes: list[ProbeSpec]) -> tuple[bool, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    all_ok = True
    for p in probes:
        ok, actual = evaluate_probe(mcp, p)
        all_ok = all_ok and ok
        results.append({"id": p.id, "ok": ok, "actual": actual, "op": p.op, "value": p.value})
    return all_ok, results


def evaluate_evidence(mcp: RecordingMCP, ev: EvidenceTruth) -> tuple[bool, Any]:
    try:
        result = _invoke(mcp, ev.call, {})
    except Exception:  # noqa: BLE001
        return False, None
    actual = extract(result, ev.path)
    if ev.op in ("gt", "ge", "lt", "le") and isinstance(actual, list):
        nums = [n for n in (_as_number(x) for x in actual) if n is not None]
        return any(compare(n, ev.op, ev.value) for n in nums), actual
    return compare(actual, ev.op, ev.value), actual
