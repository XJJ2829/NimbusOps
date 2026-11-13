"""Agent core module.

Keep package initialization light so embedders can import focused runtime
pieces such as ``my_agent.agent.runner`` without loading memory, skills,
channels, or other optional product-layer dependencies.
"""

from importlib import import_module

_LAZY_EXPORTS = {
    "AgentHook": ("my_agent.agent.hook", "AgentHook"),
    "AgentHookContext": ("my_agent.agent.hook", "AgentHookContext"),
    "AgentRunHookContext": ("my_agent.agent.hook", "AgentRunHookContext"),
    "CompositeHook": ("my_agent.agent.hook", "CompositeHook"),
    "AgentLoop": ("my_agent.agent.loop", "AgentLoop"),
    "ContextBuilder": ("my_agent.agent.context", "ContextBuilder"),
    "MemoryStore": ("my_agent.agent.memory", "MemoryStore"),
    "SkillsLoader": ("my_agent.agent.skills", "SkillsLoader"),
    "SubagentManager": ("my_agent.agent.subagent", "SubagentManager"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


__all__ = list(_LAZY_EXPORTS)
