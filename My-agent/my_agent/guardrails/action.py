"""Action Guardrails integrated with the NimbusOps agent loop.

The rescue parser, retry budgets, and prerequisite tracking are adapted from
forge-guardrails by Antoine Zambelli (MIT). NimbusOps keeps its own runner,
provider adapters, tool registry, and execution policy layers.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from my_agent.agent.tools.registry import ToolRegistry
from my_agent.config.schema import ActionGuardrailsConfig
from my_agent.providers.base import LLMResponse, ToolCallRequest


@dataclass(frozen=True, slots=True)
class GuardrailNudge:
    """Corrective message returned to the model without executing the action."""

    role: Literal["user", "tool"]
    content: str
    kind: str
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(slots=True)
class GuardrailCheck:
    """Decision produced before tools are dispatched."""

    action: Literal["execute", "final", "retry", "fatal"]
    tool_calls: list[ToolCallRequest] | None = None
    nudge: GuardrailNudge | None = None
    reason: str | None = None


class ActionGuardrailsRuntime:
    """Shared configuration and diagnostics for per-turn guardrail sessions."""

    def __init__(self, config: ActionGuardrailsConfig | None = None) -> None:
        self._config = config or ActionGuardrailsConfig()
        self._lock = threading.Lock()
        self._stats = {
            "rescued_tool_calls": 0,
            "blocked_invalid_calls": 0,
            "blocked_prerequisites": 0,
            "stopped_error_budgets": 0,
        }

    @property
    def config(self) -> ActionGuardrailsConfig:
        return self._config

    def configure(self, config: ActionGuardrailsConfig) -> None:
        self._config = config.model_copy(deep=True)

    def new_session(
        self,
        registry: ToolRegistry,
        *,
        session_key: str | None = None,
        automation_job_id: str | None = None,
    ) -> ActionGuardrailsSession:
        config = self._config.model_copy(deep=True)
        required_steps, required_steps_scope = self.resolve_required_steps(
            session_key=session_key,
            automation_job_id=automation_job_id,
            config=config,
        )
        return ActionGuardrailsSession(
            self,
            config,
            registry,
            required_steps=required_steps,
            required_steps_scope=required_steps_scope,
        )

    def resolve_required_steps(
        self,
        *,
        session_key: str | None = None,
        automation_job_id: str | None = None,
        config: ActionGuardrailsConfig | None = None,
    ) -> tuple[list[str], str]:
        """Select one required-step set using automation > session > global priority."""
        active = config or self._config
        if automation_job_id:
            for scope in active.required_step_scopes:
                if scope.scope_type == "automation" and scope.scope_id == automation_job_id:
                    return list(scope.steps), f"automation:{automation_job_id}"
        if session_key:
            for scope in active.required_step_scopes:
                if scope.scope_type == "session" and scope.scope_id == session_key:
                    return list(scope.steps), f"session:{session_key}"
        return list(active.required_steps), "global"

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + amount

    def payload(self, tool_names: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
        config = self._config
        return {
            "enabled": config.enabled,
            "config": config.model_dump(mode="json", by_alias=True),
            "capabilities": [
                {
                    "id": "response_validation",
                    "name": "工具调用校验",
                    "description": "在执行前校验工具名称、参数结构与工具 Schema。",
                    "enabled": config.enabled,
                },
                {
                    "id": "tool_call_rescue",
                    "name": "工具调用抢救",
                    "description": "从模型误写到普通文本中的 JSON、XML 和原生工具格式恢复调用。",
                    "enabled": config.enabled and config.rescue_tool_calls,
                },
                {
                    "id": "prerequisite_enforcement",
                    "name": "动作前置依赖",
                    "description": "要求高风险或后续动作先完成指定工具步骤，可按参数匹配。",
                    "enabled": config.enabled,
                },
                {
                    "id": "error_budget",
                    "name": "连续错误预算",
                    "description": "限制连续格式错误、参数错误和工具执行失败，避免无效循环。",
                    "enabled": config.enabled,
                },
            ],
            "available_tools": sorted(tool_names or []),
            "statistics": stats,
        }


class ActionGuardrailsSession:
    """Forge-style reliability state scoped to one AgentRunner turn."""

    def __init__(
        self,
        runtime: ActionGuardrailsRuntime,
        config: ActionGuardrailsConfig,
        registry: ToolRegistry,
        *,
        required_steps: list[str] | None = None,
        required_steps_scope: str = "global",
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.registry = registry
        self.required_steps = list(
            config.required_steps if required_steps is None else required_steps
        )
        self.required_steps_scope = required_steps_scope
        self.completed_steps: dict[str, None] = {}
        self.executed_tools: dict[str, list[dict[str, Any]]] = {}
        self.consecutive_response_retries = 0
        self.consecutive_tool_errors = 0
        self.consecutive_prerequisite_violations = 0

    def inspect(self, response: LLMResponse) -> GuardrailCheck:
        if not self.config.enabled or response.finish_reason == "error":
            return GuardrailCheck(
                action="execute" if response.should_execute_tools else "final",
                tool_calls=list(response.tool_calls),
            )
        if response.has_tool_calls and not response.should_execute_tools:
            return GuardrailCheck(action="final")

        tool_calls = list(response.tool_calls)
        if not tool_calls and self.config.rescue_tool_calls and response.content:
            tool_calls = _rescue_tool_calls(response.content, self.registry.tool_names)
            if tool_calls:
                self.runtime.increment("rescued_tool_calls", len(tool_calls))

        if tool_calls:
            return self._inspect_tool_calls(tool_calls)

        pending = self.pending_required_steps()
        if pending and response.finish_reason not in {"length", "content_filter", "refusal"}:
            return self._retry_response(
                GuardrailNudge(
                    role="user",
                    kind="required_steps",
                    content=(
                        "You cannot finish this turn yet. Complete one of the required "
                        f"tool steps first: {', '.join(pending)}."
                    ),
                )
            )

        self.consecutive_response_retries = 0
        return GuardrailCheck(action="final")

    def _inspect_tool_calls(self, calls: list[ToolCallRequest]) -> GuardrailCheck:
        normalized: list[ToolCallRequest] = []
        for call in calls:
            if not isinstance(call.arguments, dict):
                self.runtime.increment("blocked_invalid_calls")
                return self._retry_tool_call(
                    call,
                    (
                        f"Tool call to '{call.name}' had malformed arguments. "
                        "Arguments must be a JSON object."
                    ),
                )
            tool, params, error = self.registry.prepare_call(call.name, call.arguments)
            if error:
                self.runtime.increment("blocked_invalid_calls")
                return self._retry_tool_call(call, error.removeprefix("Error: "))
            if tool is None:
                self.runtime.increment("blocked_invalid_calls")
                return self._retry_tool_call(call, f"Tool '{call.name}' is unavailable.")
            normalized.append(
                ToolCallRequest(
                    id=call.id,
                    name=call.name,
                    arguments=params,
                    extra_content=call.extra_content,
                    provider_specific_fields=call.provider_specific_fields,
                    function_provider_specific_fields=call.function_provider_specific_fields,
                )
            )

        missing = self._missing_prerequisites(normalized)
        if missing is not None:
            call, prerequisites = missing
            self.consecutive_prerequisite_violations += 1
            self.runtime.increment("blocked_prerequisites")
            if (
                self.consecutive_prerequisite_violations
                > self.config.max_prerequisite_violations
            ):
                self.runtime.increment("stopped_error_budgets")
                return GuardrailCheck(
                    action="fatal",
                    reason=(
                        "Action Guardrails stopped the turn after repeated "
                        "prerequisite violations."
                    ),
                )
            return GuardrailCheck(
                action="retry",
                nudge=GuardrailNudge(
                    role="tool",
                    kind="prerequisite",
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=(
                        f"Tool '{call.name}' cannot run yet. First complete: "
                        f"{', '.join(prerequisites)}."
                    ),
                ),
            )

        self.consecutive_response_retries = 0
        self.consecutive_prerequisite_violations = 0
        return GuardrailCheck(action="execute", tool_calls=normalized)

    def _retry_response(self, nudge: GuardrailNudge) -> GuardrailCheck:
        self.consecutive_response_retries += 1
        if self.consecutive_response_retries > self.config.max_response_retries:
            self.runtime.increment("stopped_error_budgets")
            return GuardrailCheck(
                action="fatal",
                reason="Action Guardrails stopped the turn after repeated invalid responses.",
            )
        return GuardrailCheck(action="retry", nudge=nudge)

    def _retry_tool_call(self, call: ToolCallRequest, message: str) -> GuardrailCheck:
        self.consecutive_response_retries += 1
        if self.consecutive_response_retries > self.config.max_response_retries:
            self.runtime.increment("stopped_error_budgets")
            return GuardrailCheck(
                action="fatal",
                reason="Action Guardrails stopped the turn after repeated invalid tool calls.",
            )
        return GuardrailCheck(
            action="retry",
            nudge=GuardrailNudge(
                role="tool",
                kind="tool_validation",
                tool_call_id=call.id,
                tool_name=call.name,
                content=message,
            ),
        )

    def _missing_prerequisites(
        self,
        calls: list[ToolCallRequest],
    ) -> tuple[ToolCallRequest, list[str]] | None:
        for call in calls:
            definitions = self.config.tool_prerequisites.get(call.name, [])
            missing: list[str] = []
            for definition in definitions:
                if isinstance(definition, str):
                    prerequisite_name = definition
                    match_arg = None
                else:
                    prerequisite_name = definition.tool
                    match_arg = definition.match_arg
                prior = self.executed_tools.get(prerequisite_name, [])
                if not prior:
                    missing.append(prerequisite_name)
                    continue
                if match_arg and not any(
                    item.get(match_arg) == call.arguments.get(match_arg) for item in prior
                ):
                    missing.append(f"{prerequisite_name}({match_arg})")
            if missing:
                return call, missing
        return None

    def record_batch(
        self,
        calls: list[ToolCallRequest],
        events: list[dict[str, str]],
    ) -> str | None:
        had_error = False
        for call, event in zip(calls, events):
            if event.get("status") == "ok":
                self.completed_steps[call.name] = None
                self.executed_tools.setdefault(call.name, []).append(call.arguments)
            else:
                had_error = True

        if not had_error:
            self.consecutive_tool_errors = 0
            return None

        self.consecutive_tool_errors += 1
        if self.consecutive_tool_errors > self.config.max_tool_errors:
            self.runtime.increment("stopped_error_budgets")
            return "Action Guardrails stopped the turn after repeated tool execution errors."
        return None

    def pending_required_steps(self) -> list[str]:
        return [
            name for name in self.required_steps if name not in self.completed_steps
        ]


_THINK_TAG_RE = re.compile(
    r"\[THINK\].*?\[/THINK\]|<think>.*?</think>",
    re.DOTALL | re.IGNORECASE,
)
_REHEARSAL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\[ARGS\](\{.*\})", re.DOTALL)
_QWEN_FUNCTION_RE = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.DOTALL)
_QWEN_PARAMETER_RE = re.compile(
    r"<parameter=([^>\s]+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|$)",
    re.DOTALL,
)
_MISTRAL_BRACKET_RE = re.compile(r"\[TOOL_CALLS\]([A-Za-z_][A-Za-z0-9_]*)\s*(?=\{)")


def _rescue_tool_calls(text: str, available_tools: list[str]) -> list[ToolCallRequest]:
    cleaned = _THINK_TAG_RE.sub("", text).strip()
    if not cleaned:
        return []

    found = _extract_json_tool_calls(cleaned, available_tools)
    if not found:
        for match in _REHEARSAL_RE.finditer(cleaned):
            name, raw_args = match.group(1), match.group(2)
            if name not in available_tools:
                continue
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                continue
            if isinstance(args, dict):
                found.append(_tool_call(name, args))

    if not found:
        for match in _QWEN_FUNCTION_RE.finditer(cleaned):
            name = match.group(1).strip()
            if name not in available_tools:
                continue
            args: dict[str, str] = {}
            for parameter in _QWEN_PARAMETER_RE.finditer(match.group(2)):
                args[parameter.group(1).strip()] = parameter.group(2).strip("\n")
            found.append(_tool_call(name, args))

    if not found:
        for match in _MISTRAL_BRACKET_RE.finditer(cleaned):
            name = match.group(1)
            if name not in available_tools:
                continue
            raw = _balanced_object(cleaned, match.end())
            if raw is None:
                continue
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(args, dict):
                found.append(_tool_call(name, args))
    return found


def _extract_json_tool_calls(
    text: str,
    available_tools: list[str],
) -> list[ToolCallRequest]:
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "")
    found: list[ToolCallRequest] = []
    index = 0
    while index < len(cleaned):
        start = cleaned.find("{", index)
        if start < 0:
            break
        raw = _balanced_object(cleaned, start)
        if raw is None:
            index = start + 1
            continue
        index = start + len(raw)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("tool") or value.get("name")
        if name not in available_tools:
            continue
        args = value.get("args", value.get("arguments", {}))
        found.append(_tool_call(str(name), args))
    return found


def _balanced_object(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _tool_call(name: str, arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(
        id=f"guardrail_{uuid.uuid4().hex[:12]}",
        name=name,
        arguments=arguments,
    )
