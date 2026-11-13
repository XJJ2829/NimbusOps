"""Agent adapter protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..mcp_client import RecordingMCP
from ..models import AgentResult


@dataclass
class AgentContext:
    scenario_id: str
    title: str
    incident: str
    sandbox: Path
    artifacts: dict[str, Any] = field(default_factory=dict)


class Agent(Protocol):
    name: str

    def run(self, mcp: RecordingMCP, context: AgentContext) -> AgentResult: ...
