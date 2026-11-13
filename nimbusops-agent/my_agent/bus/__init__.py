"""Message bus module for decoupled channel-agent communication."""

from my_agent.bus.events import InboundMessage, OutboundMessage
from my_agent.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
