"""Fault injector base types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..capabilities import Capabilities
from ..sandbox import Sandbox


@dataclass
class FaultContext:
    sandbox: Sandbox
    caps: Capabilities
    params: dict[str, Any] = field(default_factory=dict)
    seed: int = 0


class Fault:
    """Base class: subclasses inject a bounded, reversible fault."""

    type = "base"

    def __init__(self, ctx: FaultContext) -> None:
        self.ctx = ctx
        self.artifacts: dict[str, Any] = {}

    def inject(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def revert(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError
