"""Agent entry point for administrator-approved root actions."""

from __future__ import annotations

import json
from typing import Any

from my_agent.agent.tools.base import Tool
from my_agent.agent.tools.context import current_request_session_key
from my_agent.agent.tools.schema import ObjectSchema, StringSchema, tool_parameters_schema
from my_agent.security.privileged import PrivilegedActionRuntime


class PrivilegedActionTool(Tool):
    """Request one root-owned allowlisted action after role-aware approval."""

    _scopes = {"core"}

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.privileged_runtime is not None and ctx.privileged_runtime.config.enabled

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(ctx.privileged_runtime)

    def __init__(self, runtime: PrivilegedActionRuntime) -> None:
        self.runtime = runtime

    @property
    def name(self) -> str:
        return "privileged_action"

    @property
    def definition_cacheable(self) -> bool:
        return False

    def _action_lines(self) -> list[str]:
        lines: list[str] = []
        for action in self.runtime.registered_actions:
            name = str(action.get("name") or "").strip()
            if not name:
                continue
            label = str(action.get("label") or name).strip()
            description = str(action.get("description") or "").strip()
            parameters = action.get("parameters")
            parameter_text = "none"
            if isinstance(parameters, dict) and parameters:
                parts: list[str] = []
                for parameter_name, schema in parameters.items():
                    schema = schema if isinstance(schema, dict) else {}
                    kind = str(schema.get("type") or "string")
                    required = "required" if schema.get("required") is True else "optional"
                    enum = schema.get("enum")
                    allowed = f", allowed={enum}" if isinstance(enum, list) and enum else ""
                    parts.append(f"{parameter_name} ({kind}, {required}{allowed})")
                parameter_text = "; ".join(parts)
            detail = f" - {label}"
            if description:
                detail += f": {description}"
            lines.append(f"- {name}{detail}. Parameters: {parameter_text}.")
        return lines

    @property
    def description(self) -> str:
        base = (
            "Request a single administrator-approved root operation from the independent "
            "NimbusOps privileged helper. Use this only after an ordinary exec operation "
            "fails because operating-system privileges are required. It accepts only action "
            "names and parameters registered in the root-owned policy; it never provides sudo, "
            "a root shell, or arbitrary command execution. Regular users are denied and told "
            "to contact an administrator. Never invent or translate an action name."
        )
        actions = self._action_lines()
        if not actions:
            return f"{base} No registered actions are currently available."
        return f"{base}\n\nCurrently registered actions:\n" + "\n".join(actions)

    @property
    def parameters(self) -> dict[str, Any]:
        action_names = [
            str(action.get("name")).strip()
            for action in self.runtime.registered_actions
            if str(action.get("name") or "").strip()
        ]
        action_description = (
            "Exact privileged action name registered by the root-owned NimbusOps policy. "
            "Use one of the names exposed by this schema; do not invent aliases."
        )
        return tool_parameters_schema(
            required=["action", "parameters", "reason"],
            action=StringSchema(
                action_description,
                enum=action_names or None,
            ),
            parameters=ObjectSchema(
                description=(
                    "Structured parameters for the selected registered action. See the tool "
                    "description for required fields. Use an empty object when it says none. "
                    "Arbitrary commands are not accepted."
                )
            ),
            reason=StringSchema(
                "Short user-facing reason explaining why this operation requires root.",
                min_length=3,
                max_length=500,
            ),
        )

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
        reason: str,
    ) -> str:
        session_key = current_request_session_key() or "default"
        result = await self.runtime.request(
            session_key=session_key,
            action_name=action.strip(),
            parameters=parameters,
            reason=reason,
        )
        output = {
            "action": action,
            "exit_code": result.get("exit_code"),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "duration_ms": result.get("duration_ms"),
        }
        return json.dumps(output, ensure_ascii=False, indent=2)
