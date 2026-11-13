import pytest

from my_agent.agent.tools.base import Tool
from my_agent.agent.tools.registry import ToolRegistry


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo text."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs):
        return kwargs["text"] * int(kwargs.get("count", 1))


@pytest.mark.asyncio
async def test_registry_registers_casts_and_executes_tool():
    registry = ToolRegistry()
    registry.register(EchoTool())

    assert registry.has("echo")
    assert registry.tool_names == ["echo"]
    assert await registry.execute("echo", {"text": "ha", "count": "2"}) == "haha"


@pytest.mark.asyncio
async def test_registry_returns_actionable_error_for_unknown_tool():
    registry = ToolRegistry()

    result = await registry.execute("missing", {})

    assert "Tool 'missing' not found" in result
    assert "Analyze the error" in result
