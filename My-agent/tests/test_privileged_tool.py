from __future__ import annotations

from types import SimpleNamespace

from my_agent.agent.tools.privileged import PrivilegedActionTool
from my_agent.agent.tools.registry import ToolRegistry


class FakePrivilegedRuntime:
    def __init__(self, actions: list[dict]) -> None:
        self.config = SimpleNamespace(enabled=True)
        self.registered_actions = actions


def test_privileged_tool_exposes_registered_action_names() -> None:
    runtime = FakePrivilegedRuntime(
        [
            {
                "name": "flush_dns_cache",
                "label": "刷新 DNS 缓存",
                "description": "向 macOS mDNSResponder 发送 HUP 信号。",
                "parameters": {},
            }
        ]
    )

    tool = PrivilegedActionTool(runtime)  # type: ignore[arg-type]
    schema = tool.to_schema()["function"]

    assert schema["parameters"]["properties"]["action"]["enum"] == ["flush_dns_cache"]
    assert "flush_dns_cache" in schema["description"]
    assert "Parameters: none" in schema["description"]


def test_registry_refreshes_dynamic_privileged_tool_definition() -> None:
    runtime = FakePrivilegedRuntime(
        [{"name": "flush_dns_cache", "parameters": {}}]
    )
    registry = ToolRegistry()
    registry.register(PrivilegedActionTool(runtime))  # type: ignore[arg-type]

    first = registry.get_definitions()[0]["function"]["parameters"]["properties"]["action"]
    runtime.registered_actions = [
        {
            "name": "service_restart",
            "parameters": {
                "service": {
                    "type": "string",
                    "required": True,
                    "enum": ["nginx.service"],
                }
            },
        }
    ]
    second_schema = registry.get_definitions()[0]["function"]
    second = second_schema["parameters"]["properties"]["action"]

    assert first["enum"] == ["flush_dns_cache"]
    assert second["enum"] == ["service_restart"]
    assert "service (string, required" in second_schema["description"]
