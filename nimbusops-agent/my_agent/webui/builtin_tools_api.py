"""Read-only WebUI payload for tools registered in the active agent runtime."""

from __future__ import annotations

from typing import Any

from my_agent.agent.tools.registry import ToolRegistry


_EXCLUDED_TOOL_NAMES = {"run_cli_app"}


def builtin_tools_payload(registry: ToolRegistry) -> dict[str, Any]:
    """Return the active non-MCP tools using their runtime schemas."""
    tools: list[dict[str, Any]] = []

    for schema in registry.get_definitions():
        function = schema.get("function")
        if not isinstance(function, dict):
            continue

        name = function.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("mcp_")
            or name in _EXCLUDED_TOOL_NAMES
        ):
            continue

        tool = registry.get(name)
        if tool is None:
            continue

        parameter_schema = function.get("parameters")
        if not isinstance(parameter_schema, dict):
            parameter_schema = {}
        properties = parameter_schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        required_values = parameter_schema.get("required")
        required = {
            value
            for value in required_values
            if isinstance(value, str)
        } if isinstance(required_values, list) else set()

        parameters: list[dict[str, Any]] = []
        for parameter_name, raw_schema in properties.items():
            if not isinstance(parameter_name, str):
                continue
            property_schema = raw_schema if isinstance(raw_schema, dict) else {}
            description = property_schema.get("description")
            parameters.append(
                {
                    "name": parameter_name,
                    "description": description if isinstance(description, str) else "",
                    "required": parameter_name in required,
                    "schema": property_schema,
                }
            )

        module_name = tool.__class__.__module__.rsplit(".", 1)[-1]
        description = function.get("description")
        tools.append(
            {
                "name": name,
                "display_name": name,
                "category": module_name.replace("_", " ").title(),
                "description": description if isinstance(description, str) else "",
                "available": True,
                "source": "builtin",
                "read_only": bool(tool.read_only),
                "parameters": parameters,
                "schema": schema,
            }
        )

    return {"tools": tools, "count": len(tools)}
