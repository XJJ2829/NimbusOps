"""OpsShield rule language and enforcement runtime."""

from __future__ import annotations

import sys

from . import spec_lang as _spec_lang

# Upstream rule.py imports ``spec_lang`` as a top-level package.
# Keep that file untouched while hosting the parser under ``my_agent``.
sys.modules.setdefault("spec_lang", _spec_lang)

from .predicates import (
    current_action_ast,
    compile_predicate_source,
    predicate_catalog,
    predicate_table,
    register_predicate,
    unregister_predicate,
)
from .rule import Rule
from .runtime import AgentSpecRuntime, EnforcementDecision, PolicyRule

__all__ = [
    "AgentSpecRuntime",
    "EnforcementDecision",
    "PolicyRule",
    "Rule",
    "predicate_table",
    "current_action_ast",
    "predicate_catalog",
    "register_predicate",
    "unregister_predicate",
    "compile_predicate_source",
]
