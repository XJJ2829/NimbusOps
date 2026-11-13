from my_agent.agent.tools.base import Tool
from my_agent.agent.tools.registry import ToolRegistry
from my_agent.config.schema import ActionGuardrailsConfig
from my_agent.guardrails import ActionGuardrailsRuntime
from my_agent.providers.base import LLMResponse, ToolCallRequest


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
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, **kwargs):
        return kwargs["text"]


class InspectTool(EchoTool):
    @property
    def name(self) -> str:
        return "inspect"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(InspectTool())
    return registry


def test_rescues_text_tool_call_and_validates_arguments():
    runtime = ActionGuardrailsRuntime()
    session = runtime.new_session(_registry())

    rescued = session.inspect(
        LLMResponse(content='{"tool":"echo","args":{"text":"hello"}}')
    )
    invalid = session.inspect(
        LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCallRequest(id="bad", name="echo", arguments={}),
            ],
        )
    )

    assert rescued.action == "execute"
    assert rescued.tool_calls is not None
    assert rescued.tool_calls[0].name == "echo"
    assert invalid.action == "retry"
    assert runtime.payload()["statistics"]["rescued_tool_calls"] == 1
    assert runtime.payload()["statistics"]["blocked_invalid_calls"] == 1


def test_enforces_argument_aware_prerequisite():
    runtime = ActionGuardrailsRuntime(
        ActionGuardrailsConfig(
            tool_prerequisites={
                "echo": [{"tool": "inspect", "matchArg": "text"}],
            },
        )
    )
    session = runtime.new_session(_registry())
    echo_call = ToolCallRequest(
        id="echo-1",
        name="echo",
        arguments={"text": "target"},
    )

    blocked = session.inspect(
        LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[echo_call],
        )
    )
    session.record_batch(
        [
            ToolCallRequest(
                id="inspect-1",
                name="inspect",
                arguments={"text": "target"},
            )
        ],
        [{"name": "inspect", "status": "ok", "detail": "ok"}],
    )
    allowed = session.inspect(
        LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[echo_call],
        )
    )

    assert blocked.action == "retry"
    assert allowed.action == "execute"


def test_stops_after_tool_error_budget_is_exceeded():
    runtime = ActionGuardrailsRuntime(ActionGuardrailsConfig(max_tool_errors=1))
    session = runtime.new_session(_registry())
    call = ToolCallRequest(id="echo-1", name="echo", arguments={"text": "x"})
    event = [{"name": "echo", "status": "error", "detail": "failed"}]

    assert session.record_batch([call], event) is None
    assert session.record_batch([call], event) is not None
    assert runtime.payload()["statistics"]["stopped_error_budgets"] == 1


def test_required_steps_use_automation_session_global_priority():
    runtime = ActionGuardrailsRuntime(
        ActionGuardrailsConfig(
            required_steps=["echo"],
            required_step_scopes=[
                {
                    "scopeType": "session",
                    "scopeId": "websocket:incident",
                    "steps": ["inspect"],
                },
                {
                    "scopeType": "automation",
                    "scopeId": "daily-check",
                    "steps": ["echo", "inspect"],
                },
            ],
        )
    )

    automation = runtime.new_session(
        _registry(),
        session_key="websocket:incident",
        automation_job_id="daily-check",
    )
    conversation = runtime.new_session(
        _registry(),
        session_key="websocket:incident",
    )
    global_scope = runtime.new_session(
        _registry(),
        session_key="websocket:other",
    )

    assert automation.pending_required_steps() == ["echo", "inspect"]
    assert automation.required_steps_scope == "automation:daily-check"
    assert conversation.pending_required_steps() == ["inspect"]
    assert conversation.required_steps_scope == "session:websocket:incident"
    assert global_scope.pending_required_steps() == ["echo"]
    assert global_scope.required_steps_scope == "global"


def test_required_step_scope_does_not_stack_with_global_steps():
    runtime = ActionGuardrailsRuntime(
        ActionGuardrailsConfig(
            required_steps=["echo"],
            required_step_scopes=[
                {
                    "scopeType": "session",
                    "scopeId": "websocket:incident",
                    "steps": ["inspect"],
                },
            ],
        )
    )
    session = runtime.new_session(
        _registry(),
        session_key="websocket:incident",
    )

    session.record_batch(
        [ToolCallRequest(id="inspect-1", name="inspect", arguments={"text": "ok"})],
        [{"name": "inspect", "status": "ok", "detail": "ok"}],
    )

    assert session.pending_required_steps() == []
