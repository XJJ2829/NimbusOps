"""Read-only audit views assembled from persisted WebUI and session records."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from my_agent.webui.transcript import read_transcript_lines

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\s*[:=]\s*([^\s,;]+)"
)
_EXIT_CODE_RE = re.compile(r"(?im)\b(?:exit|return)\s+code\s*:\s*(-?\d+)")
_FAILURE_TEXT_RE = re.compile(
    r"(?i)(?:^|\n)\s*(?:error|fatal|traceback)\s*:|"
    r"command not found|no such file|permission denied|"
    r"could not be found|timed out|failed to"
)
_FAILURE_STATUSES = {
    "blocked",
    "denied",
    "error",
    "failed",
    "failure",
    "not_found",
    "rejected",
    "timed_out",
    "timeout",
    "unavailable",
}
_MAX_DETAIL_TEXT = 4000


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        clipped = value[:_MAX_DETAIL_TEXT]
        if len(value) > _MAX_DETAIL_TEXT:
            clipped += "\n...[truncated]"
        return _INLINE_SECRET_RE.sub(r"\1=[REDACTED]", clipped)
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _session_user_messages(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict) and item.get("role") == "user"]


def _group_turns(lines: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    ordered: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in lines:
        turn_id = record.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if turn_id not in grouped:
            ordered.append(turn_id)
            grouped[turn_id] = []
        grouped[turn_id].append(record)
    return [(turn_id, grouped[turn_id]) for turn_id in ordered]


def _tool_calls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        tool_events = record.get("tool_events")
        if not isinstance(tool_events, list):
            continue
        sequence = int(record.get("turn_seq") or 0)
        for raw in tool_events:
            if not isinstance(raw, dict):
                continue
            call_id = str(raw.get("call_id") or "").strip()
            if not call_id:
                continue
            if call_id not in calls:
                order.append(call_id)
                calls[call_id] = {
                    "call_id": call_id,
                    "name": str(raw.get("name") or "unknown"),
                    "arguments": raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {},
                    "result": None,
                    "error": None,
                    "sequence": sequence,
                }
            call = calls[call_id]
            if raw.get("phase") == "end":
                call["result"] = raw.get("result")
                call["error"] = raw.get("error")
                call["sequence"] = sequence
    return [calls[call_id] for call_id in order]


def _scan_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scans: list[dict[str, Any]] = []
    for record in records:
        if record.get("event") != "prompt_guardrails_scan":
            continue
        for raw in record.get("scans") or []:
            if isinstance(raw, dict):
                scans.append({**raw, "sequence": int(record.get("turn_seq") or 0)})
    return scans


def _join_stream(records: list[dict[str, Any]], event_name: str) -> str:
    parts: list[str] = []
    for record in records:
        if record.get("event") != event_name:
            continue
        value = record.get("text")
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _tool_title(name: Any) -> str:
    return str(name or "unknown").replace("mcp_operations-agent-mcp_", "")


def _structured_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str) or not result.strip().startswith("{"):
        return None
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _anomaly_text(value: Any, fallback: str) -> str:
    if value in (None, "", [], {}):
        return fallback
    if isinstance(value, str):
        return value[:320]
    return json.dumps(value, ensure_ascii=False, default=str)[:320]


def _tool_anomaly(trace_id: str, tool: dict[str, Any]) -> dict[str, Any] | None:
    name = _tool_title(tool.get("name"))
    event_id = f"{trace_id}:tool:{tool['call_id']}"
    error = tool.get("error")
    result = tool.get("result")
    structured = _structured_result(result)

    if error:
        return {
            "event_id": event_id,
            "stage": "tool_call",
            "severity": "error",
            "category": "tool_error",
            "title": f"{name} 执行失败",
            "cause": _anomaly_text(error, "工具返回了未说明原因的错误"),
            "impact": "该工具没有产生可用结果，后续诊断可能不完整。",
            "tool_name": tool.get("name"),
        }

    if structured is not None:
        exit_code = structured.get("exit_code", structured.get("returncode"))
        status = str(structured.get("status") or "").strip().lower()
        explicit_error = structured.get("error") or structured.get("errors")
        failed = (
            structured.get("ok") is False
            or structured.get("success") is False
            or status in _FAILURE_STATUSES
            or (isinstance(exit_code, int) and exit_code != 0)
            or bool(explicit_error)
        )
        if failed:
            cause = (
                explicit_error
                or structured.get("stderr")
                or structured.get("message")
                or structured.get("reason")
                or (f"进程退出码为 {exit_code}" if exit_code is not None else None)
            )
            return {
                "event_id": event_id,
                "stage": "tool_call",
                "severity": "error",
                "category": "tool_error",
                "title": f"{name} 返回异常",
                "cause": _anomaly_text(cause, "工具明确返回了失败状态"),
                "impact": "该步骤未按预期完成，应优先核对参数、权限和目标资源状态。",
                "tool_name": tool.get("name"),
            }

        if structured.get("exists") is False or structured.get("found") is False:
            target = tool.get("arguments") or {}
            return {
                "event_id": event_id,
                "stage": "tool_call",
                "severity": "warning",
                "category": "resource_missing",
                "title": f"{name} 未找到目标资源",
                "cause": _anomaly_text(target, "工具确认目标资源不存在"),
                "impact": "后续步骤缺少目标资源，诊断结论应基于该缺失状态。",
                "tool_name": tool.get("name"),
            }

        if (
            name == "read_config"
            and structured.get("content") == ""
            and structured.get("size_bytes") == 0
        ):
            path = (tool.get("arguments") or {}).get("path")
            cause = f"{path} 未返回任何配置内容" if path else "目标配置未返回任何内容"
            return {
                "event_id": event_id,
                "stage": "tool_call",
                "severity": "warning",
                "category": "empty_evidence",
                "title": "未读取到目标配置",
                "cause": cause,
                "impact": "无法从该配置获得有效证据，目标文件可能为空或不存在。",
                "tool_name": tool.get("name"),
            }

    if isinstance(result, str):
        exit_code_match = _EXIT_CODE_RE.search(result)
        exit_code = int(exit_code_match.group(1)) if exit_code_match else 0
        if exit_code != 0 or _FAILURE_TEXT_RE.search(result):
            return {
                "event_id": event_id,
                "stage": "tool_call",
                "severity": "error",
                "category": "tool_error",
                "title": f"{name} 返回异常",
                "cause": result[:320],
                "impact": "该步骤未按预期完成，应优先检查命令输出和运行环境。",
                "tool_name": tool.get("name"),
            }
    return None


def _collect_anomalies(
    trace_id: str,
    tools: list[dict[str, Any]],
    scans: list[dict[str, Any]],
    privileged: list[dict[str, Any]],
    trace_status: str,
) -> list[dict[str, Any]]:
    anomalies = [item for tool in tools if (item := _tool_anomaly(trace_id, tool))]
    blocked_scans = [item for item in scans if item.get("allowed") is False]
    if blocked_scans:
        anomalies.append(
            {
                "event_id": f"{trace_id}:guardrails",
                "stage": "security_check",
                "severity": "error",
                "category": "security_block",
                "title": "安全校验阻止了任务",
                "cause": f"{len(blocked_scans)} 项安全检查未通过",
                "impact": "相关操作未进入执行阶段，系统状态未被该操作修改。",
            }
        )

    for index, record in enumerate(privileged):
        outcome = str(record.get("outcome") or "unknown").strip().lower()
        if outcome in {"approved", "succeeded", "success"}:
            continue
        severity = "error" if outcome in {"error", "failed", "failure"} else "warning"
        action = record.get("action_name") or "系统操作"
        cause = record.get("error") or record.get("stderr") or record.get("reason")
        anomalies.append(
            {
                "event_id": f"{trace_id}:privileged:{record.get('id') or index}",
                "stage": "approval",
                "severity": severity,
                "category": "privileged_action",
                "title": f"特权操作未完成 · {action}",
                "cause": _anomaly_text(cause, f"审批或执行结果为 {outcome}"),
                "impact": "高权限操作没有成功完成，系统保留了审批与执行证据。",
            }
        )

    if trace_status == "interrupted":
        anomalies.append(
            {
                "event_id": f"{trace_id}:complete",
                "stage": "completed",
                "severity": "warning",
                "category": "trace_interrupted",
                "title": "任务记录意外中断",
                "cause": "没有找到完整的任务结束事件",
                "impact": "该任务的最终结果和部分后续事件可能未被完整记录。",
            }
        )
    return anomalies


def _privileged_for_turn(
    records: list[dict[str, Any]],
    *,
    session_key: str,
    started_at: datetime | None,
    next_started_at: datetime | None,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for record in records:
        if record.get("session_key") != session_key:
            continue
        raw_time = record.get("requested_at") or record.get("timestamp")
        try:
            occurred_at = datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            occurred_at = None
        if started_at is not None and occurred_at is not None:
            lower = started_at - timedelta(seconds=5)
            upper = (next_started_at or (started_at + timedelta(hours=2))) + timedelta(seconds=5)
            if occurred_at < lower or occurred_at >= upper:
                continue
        matched.append(record)
    return matched


def _trace_summary(
    *,
    trace_id: str,
    session_key: str,
    session_title: str,
    actor_username: str,
    records: list[dict[str, Any]],
    user_message: dict[str, Any] | None,
    privileged: list[dict[str, Any]],
) -> dict[str, Any]:
    user_event = next((item for item in records if item.get("event") == "user"), {})
    user_input = user_event.get("text") or (user_message or {}).get("content") or "未记录用户输入"
    if not isinstance(user_input, str):
        user_input = json.dumps(user_input, ensure_ascii=False, default=str)
    started_at = _parse_datetime((user_message or {}).get("timestamp"))
    end_event = next((item for item in reversed(records) if item.get("event") == "turn_end"), None)
    latency_ms = int((end_event or {}).get("latency_ms") or 0)
    completed_at = started_at + timedelta(milliseconds=latency_ms) if started_at and latency_ms else None
    tools = _tool_calls(records)
    scans = _scan_records(records)
    blocked_scans = [item for item in scans if item.get("allowed") is False]
    ops_shield_hits = [
        item
        for item in tools
        if "opsshield" in str(item.get("result") or "").lower()
    ]
    trace_status = "completed" if end_event is not None else "interrupted"
    anomalies = _collect_anomalies(trace_id, tools, scans, privileged, trace_status)
    security_event_count = len(scans) + len(ops_shield_hits) + len(privileged)
    if privileged or blocked_scans or ops_shield_hits:
        risk_level = "high"
    elif anomalies:
        risk_level = "medium"
    else:
        risk_level = "low"
    return {
        "trace_id": trace_id,
        "session_key": session_key,
        "session_title": session_title,
        "actor_username": actor_username,
        "user_input": _redact(user_input),
        "started_at": _iso(started_at),
        "completed_at": _iso(completed_at),
        "duration_ms": latency_ms,
        "status": trace_status,
        "risk_level": risk_level,
        "tool_count": len(tools),
        "security_event_count": security_event_count,
        "anomaly_count": len(anomalies),
        "integrity": "complete" if user_event and end_event is not None else "partial",
    }


def _timeline(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    privileged: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    timeline.append(
        {
            "id": f"{summary['trace_id']}:received",
            "sequence": 1,
            "stage": "received",
            "status": "success",
            "title": "接收指令",
            "summary": summary["user_input"],
            "timestamp": summary.get("started_at"),
            "details": {"session_key": summary["session_key"]},
        }
    )

    reasoning = _join_stream(records, "reasoning_delta")
    reasoning_seq = min(
        (int(item.get("turn_seq") or 0) for item in records if item.get("event") == "reasoning_delta"),
        default=2,
    )
    if reasoning:
        timeline.append(
            {
                "id": f"{summary['trace_id']}:reasoning",
                "sequence": reasoning_seq,
                "stage": "reasoning",
                "status": "success",
                "title": "推理与决策",
                "summary": _redact(reasoning[:320]),
                "details": {"reasoning_summary": _redact(reasoning)},
            }
        )

    tools = _tool_calls(records)
    for index, tool in enumerate(tools):
        error = tool.get("error")
        result = tool.get("result")
        anomaly = _tool_anomaly(summary["trace_id"], tool)
        timeline.append(
            {
                "id": f"{summary['trace_id']}:tool:{tool['call_id']}",
                "sequence": tool["sequence"] or 10 + index,
                "stage": "tool_call",
                "status": anomaly["severity"] if anomaly else "success",
                "title": _tool_title(tool["name"]),
                "summary": (
                    _redact(anomaly["cause"])
                    if anomaly
                    else str(error or result or "工具调用已完成")[:320]
                ),
                "tool_name": tool["name"],
                "details": _redact(
                    {
                        "call_id": tool["call_id"],
                        "arguments": tool["arguments"],
                        "result": result,
                        "error": error,
                    }
                ),
            }
        )

    scans = _scan_records(records)
    if scans:
        blocked = sum(1 for item in scans if item.get("allowed") is False)
        disabled = sum(1 for item in scans if item.get("reason") == "disabled")
        summary_text = f"完成 {len(scans)} 项输入安全检查"
        if blocked:
            summary_text += f"，阻止 {blocked} 项"
        elif disabled:
            summary_text += f"，其中 {disabled} 项检测器未启用"
        timeline.append(
            {
                "id": f"{summary['trace_id']}:guardrails",
                "sequence": min(int(item.get("sequence") or 0) for item in scans),
                "stage": "security_check",
                "status": "error" if blocked else ("warning" if disabled else "success"),
                "title": "Prompt Guardrails",
                "summary": summary_text,
                "details": {"scans": _redact(scans)},
            }
        )

    for index, record in enumerate(privileged):
        outcome = str(record.get("outcome") or "unknown")
        timeline.append(
            {
                "id": f"{summary['trace_id']}:privileged:{record.get('id') or index}",
                "sequence": 9000 + index,
                "stage": "approval",
                "status": "success" if outcome in {"approved", "succeeded"} else "warning",
                "title": f"特权审批 · {record.get('action_name') or '系统操作'}",
                "summary": f"审批与执行结果：{outcome}",
                "timestamp": datetime.fromtimestamp(
                    float(record.get("requested_at") or record.get("timestamp") or 0),
                    tz=timezone.utc,
                ).isoformat(),
                "details": _redact(record),
            }
        )

    answer = _join_stream(records, "delta")
    if answer:
        timeline.append(
            {
                "id": f"{summary['trace_id']}:answer",
                "sequence": max(
                    (int(item.get("turn_seq") or 0) for item in records if item.get("event") == "delta"),
                    default=9500,
                ),
                "stage": "result",
                "status": "success",
                "title": "最终报告",
                "summary": _redact(answer[:320]),
                "details": {"final_report": _redact(answer)},
            }
        )

    timeline.append(
        {
            "id": f"{summary['trace_id']}:complete",
            "sequence": 10000,
            "stage": "completed",
            "status": "success" if summary["status"] == "completed" else "warning",
            "title": "任务完成" if summary["status"] == "completed" else "任务记录中断",
            "summary": (
                f"总耗时 {summary['duration_ms']} ms"
                if summary["duration_ms"]
                else "没有记录任务结束事件"
            ),
            "timestamp": summary.get("completed_at"),
            "details": {
                "tool_count": summary["tool_count"],
                "security_event_count": summary["security_event_count"],
                "integrity": summary["integrity"],
            },
        }
    )
    return sorted(timeline, key=lambda item: (int(item.get("sequence") or 0), item["id"]))


def _session_traces(
    session_manager: Any,
    session_row: dict[str, Any],
    privileged_records: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]]:
    session_key = str(session_row.get("key") or "").strip()
    if not session_key:
        return []
    payload = session_manager.read_session_file(session_key)
    user_messages = _session_user_messages(payload)
    lines = read_transcript_lines(session_key)
    grouped = _group_turns(lines)
    session_title = str(session_row.get("title") or session_row.get("preview") or session_key)
    actor_username = str(session_row.get("actor_username") or "未知用户")
    traces: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    for index, (trace_id, records) in enumerate(grouped):
        user_message = user_messages[index] if index < len(user_messages) else None
        started_at = _parse_datetime((user_message or {}).get("timestamp"))
        next_started_at = None
        if index + 1 < len(user_messages):
            next_started_at = _parse_datetime(user_messages[index + 1].get("timestamp"))
        privileged = _privileged_for_turn(
            privileged_records,
            session_key=session_key,
            started_at=started_at,
            next_started_at=next_started_at,
        )
        summary = _trace_summary(
            trace_id=trace_id,
            session_key=session_key,
            session_title=session_title,
            actor_username=actor_username,
            records=records,
            user_message=user_message,
            privileged=privileged,
        )
        traces.append((summary, records, privileged))
    return traces


def list_audit_traces(
    session_manager: Any,
    session_rows: list[dict[str, Any]],
    privileged_records: list[dict[str, Any]],
    *,
    limit: int = 200,
) -> dict[str, Any]:
    traces: list[dict[str, Any]] = []
    for row in session_rows:
        traces.extend(item[0] for item in _session_traces(session_manager, row, privileged_records))
    traces.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    traces = traces[: max(1, min(limit, 500))]
    return {
        "traces": traces,
        "summary": {
            "total": len(traces),
            "completed": sum(1 for item in traces if item["status"] == "completed"),
            "high_risk": sum(1 for item in traces if item["risk_level"] == "high"),
            "security_events": sum(int(item["security_event_count"]) for item in traces),
        },
    }


def get_audit_trace(
    session_manager: Any,
    session_row: dict[str, Any],
    trace_id: str,
    privileged_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for summary, records, privileged in _session_traces(
        session_manager,
        session_row,
        privileged_records,
    ):
        if summary["trace_id"] != trace_id:
            continue
        tools = _tool_calls(records)
        scans = _scan_records(records)
        anomalies = _collect_anomalies(
            trace_id,
            tools,
            scans,
            privileged,
            summary["status"],
        )
        return {
            **summary,
            "timeline": _timeline(summary, records, privileged),
            "anomalies": _redact(anomalies),
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "schema_version": 1,
        }
    return None
