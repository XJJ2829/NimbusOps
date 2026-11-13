"""OpsShield enforcement engine integrated with the My-agent runtime."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener
from loguru import logger

from my_agent.agentspec.action_ast import parse_action_ast
from my_agent.agentspec.predicates import (
    action_ast_context,
    compile_predicate_source,
    predicate_catalog,
    predicate_table,
    register_predicate,
    unregister_predicate,
)
from my_agent.agentspec.rule import Rule
from my_agent.agentspec.spec_lang.AgentSpecLexer import AgentSpecLexer
from my_agent.agentspec.spec_lang.AgentSpecParser import AgentSpecParser

DecisionName = Literal[
    "allow",
    "stop",
    "user_inspection",
    "invoke_action",
    "skip",
    "llm_self_reflect",
]
InspectionNotifier = Callable[[dict[str, Any]], Awaitable[None]]


class AgentSpecError(ValueError):
    """Base error for invalid OpsShield policy state."""


class AgentSpecBlocked(RuntimeError):
    """Raised when enforcement stops a tool action before execution."""


class AgentSpecUserRejected(RuntimeError):
    """Raised when a user rejects an inspected action."""

    def __init__(self, message: str, *, action_name: str, rule_id: str) -> None:
        super().__init__(message)
        self.action_name = action_name
        self.rule_id = rule_id


class _RaisingErrorListener(ErrorListener):
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):  # noqa: N802
        raise AgentSpecError(f"Syntax error at line {line}, column {column}: {msg}")


@dataclass(slots=True)
class PredicateExpression:
    name: str | None = None
    literal: bool | None = None
    negate: bool = False


@dataclass(slots=True)
class PolicyRule:
    rule: Rule
    predicates: list[PredicateExpression]
    enforcement: DecisionName
    replacement_action: dict[str, Any] | None = None
    source: str = "custom"
    path: Path | None = None
    enabled: bool = True

    @property
    def id(self) -> str:
        return self.rule.id

    @property
    def event(self) -> str:
        return self.rule.event

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "predicates": [
                ("!" if predicate.negate else "")
                + (
                    predicate.name
                    if predicate.name is not None
                    else str(bool(predicate.literal)).lower()
                )
                for predicate in self.predicates
            ],
            "enforcement": self.enforcement,
            "replacement_action": self.replacement_action,
            "source": self.source,
            "enabled": self.enabled,
            "filename": self.path.name if self.path else None,
            "raw": self.rule.raw,
        }


@dataclass(slots=True)
class EnforcementDecision:
    decision: DecisionName
    rule_id: str | None = None
    replacement_action: dict[str, Any] | None = None
    rationale: str | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"decision": self.decision}
        if self.rule_id:
            result["rule_id"] = self.rule_id
        if self.replacement_action is not None:
            result["replacement_action"] = self.replacement_action
        if self.rationale:
            result["rationale"] = self.rationale
        return result


@dataclass(slots=True)
class PendingInspection:
    id: str
    session_key: str
    rule_id: str
    action: dict[str, Any]
    created_at: float
    future: asyncio.Future[bool] = field(repr=False)

    def payload(self) -> dict[str, Any]:
        return {
            "inspection_id": self.id,
            "session_key": self.session_key,
            "rule_id": self.rule_id,
            "action": self.action,
            "created_at": self.created_at,
        }


class AgentSpecRuntime:
    """Load OpsShield policies, evaluate actions, and coordinate inspection."""

    def __init__(
        self,
        *,
        builtin_rules_dir: Path | None = None,
        rules_dir: Path | None = None,
        enabled: bool = True,
        inspection_timeout: float = 300.0,
        fail_closed: bool = True,
    ) -> None:
        self.builtin_rules_dir = builtin_rules_dir or Path(__file__).with_name("rules")
        self.rules_dir = rules_dir
        self.predicates_dir = rules_dir.parent / "predicates" if rules_dir else None
        self.enabled = enabled
        self.inspection_timeout = inspection_timeout
        self.fail_closed = fail_closed
        self.rules: list[PolicyRule] = []
        self.errors: list[dict[str, str]] = []
        self._disabled_ids: set[str] = set()
        self._notifiers: dict[str, InspectionNotifier] = {}
        self._pending: dict[str, PendingInspection] = {}
        self._custom_predicate_names: set[str] = set()
        self._predicate_errors: list[dict[str, str]] = []

    def configure(
        self,
        *,
        rules_dir: Path,
        enabled: bool = True,
        inspection_timeout: float = 300.0,
        fail_closed: bool = True,
    ) -> None:
        self.rules_dir = rules_dir
        self.predicates_dir = rules_dir.parent / "predicates"
        self.enabled = enabled
        self.inspection_timeout = inspection_timeout
        self.fail_closed = fail_closed
        self.reload()

    @property
    def state_path(self) -> Path | None:
        return self.rules_dir.parent / "state.json" if self.rules_dir else None

    def _load_state(self) -> None:
        self._disabled_ids = set()
        path = self.state_path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._disabled_ids = {
                str(item) for item in payload.get("disabled_rule_ids", []) if str(item).strip()
            }
        except (OSError, ValueError, TypeError):
            logger.exception("Failed to read OpsShield state from {}", path)

    def _save_state(self) -> None:
        path = self.state_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"disabled_rule_ids": sorted(self._disabled_ids)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def reload(self) -> list[PolicyRule]:
        self._load_state()
        self._reload_custom_predicates()
        loaded: list[PolicyRule] = []
        errors: list[dict[str, str]] = list(self._predicate_errors)
        sources: list[tuple[str, Path]] = [("builtin", self.builtin_rules_dir)]
        if self.rules_dir is not None:
            self.rules_dir.mkdir(parents=True, exist_ok=True)
            sources.append(("custom", self.rules_dir))
        seen_ids: set[str] = set()
        for source, directory in sources:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.rule")):
                try:
                    for policy in self.parse_text(path.read_text(encoding="utf-8"), source=source, path=path):
                        if policy.id in seen_ids:
                            raise AgentSpecError(f"Duplicate rule id: {policy.id}")
                        policy.enabled = policy.id not in self._disabled_ids
                        seen_ids.add(policy.id)
                        loaded.append(policy)
                except (AgentSpecError, OSError) as exc:
                    errors.append({"file": str(path), "message": str(exc)})
                    logger.warning("OpsShield rule {} was not loaded: {}", path, exc)
        self.rules = loaded
        self.errors = errors
        return loaded

    def _reload_custom_predicates(self) -> None:
        for name in self._custom_predicate_names:
            unregister_predicate(name)
        self._custom_predicate_names.clear()
        self._predicate_errors = []
        if self.predicates_dir is None:
            return
        self.predicates_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.predicates_dir.glob("*.py")):
            try:
                source = path.read_text(encoding="utf-8")
                name, predicate = compile_predicate_source(source, source_path=path)
                if name in predicate_table:
                    raise ValueError(f"Predicate name already registered: {name}")
                register_predicate(
                    name,
                    predicate,
                    source_kind="custom",
                    source_path=str(path),
                    source=source.strip(),
                )
                self._custom_predicate_names.add(name)
            except (OSError, ValueError) as exc:
                self._predicate_errors.append({"file": str(path), "message": str(exc)})
                logger.warning("OpsShield Predicate {} was not loaded: {}", path, exc)

    def parse_text(
        self,
        text: str,
        *,
        source: str = "custom",
        path: Path | None = None,
    ) -> list[PolicyRule]:
        stream = InputStream(text)
        lexer = AgentSpecLexer(stream)
        tokens = CommonTokenStream(lexer)
        tokens.fill()
        # The upstream README requires grammar regeneration for each custom
        # predicate. Remapping registered identifiers keeps the original ANTLR
        # grammar/parser authoritative while supporting runtime registration.
        for token in tokens.tokens:
            # Upstream declares ANY and Rule.triggered() supports it, but the
            # generated event rule only accepts IDENTIFIER. Preserve the
            # upstream wildcard semantics without modifying rule.py or the DSL.
            if token.type == AgentSpecLexer.ANY:
                token.type = AgentSpecLexer.IDENTIFIER
            elif token.type == AgentSpecLexer.IDENTIFIER and token.text in predicate_table:
                token.type = AgentSpecLexer.PREDICATE
        tokens.seek(0)
        parser = AgentSpecParser(tokens)
        parser.removeErrorListeners()
        parser.addErrorListener(_RaisingErrorListener())
        tree = parser.program()
        contexts = list(tree.rule_())
        if not contexts:
            raise AgentSpecError("OpsShield file does not contain a rule")
        policies: list[PolicyRule] = []
        for context in contexts:
            rule_id = context.ruleClause().IDENTIFIER().getText()
            event = context.triggerClause().event().getText()
            predicates = [
                self._predicate_expression(item)
                for item in context.checkClause().predicate()
            ]
            enforcement_contexts = list(context.enforceClause().enforcement())
            if len(enforcement_contexts) != 1:
                raise AgentSpecError(
                    f"Rule {rule_id!r} must contain exactly one enforcement"
                )
            enforcement_context = enforcement_contexts[0]
            replacement = None
            if enforcement_context.actionInvoke() is not None:
                enforcement: DecisionName = "invoke_action"
                replacement = self._replacement_action(enforcement_context.actionInvoke())
            else:
                enforcement_text = enforcement_context.getText()
                enforcement = (
                    "llm_self_reflect"
                    if enforcement_text == "llm_self_examine"
                    else enforcement_text
                )
                if enforcement not in {
                    "none",
                    "skip",
                    "stop",
                    "user_inspection",
                    "llm_self_reflect",
                }:
                    raise AgentSpecError(f"Unsupported enforcement: {enforcement_text}")
                if enforcement == "none":
                    enforcement = "allow"
            policies.append(
                PolicyRule(
                    rule=Rule(id=rule_id, event=event, raw=text),
                    predicates=predicates,
                    enforcement=enforcement,
                    replacement_action=replacement,
                    source=source,
                    path=path,
                )
            )
        return policies

    def _predicate_expression(self, context: Any) -> PredicateExpression:
        if context.TRUE() is not None:
            return PredicateExpression(literal=True)
        if context.FALSE() is not None:
            return PredicateExpression(literal=False)
        if context.NOT() is not None:
            child = self._predicate_expression(context.predicate())
            child.negate = not child.negate
            return child
        if context.PREDICATE() is not None:
            return PredicateExpression(name=context.PREDICATE().getText())
        predicate_func = context.predicate_func()
        if predicate_func is not None:
            return PredicateExpression(name=predicate_func.IDENTIFIER().getText())
        raise AgentSpecError(f"Unsupported predicate expression: {context.getText()}")

    @staticmethod
    def _replacement_action(context: Any) -> dict[str, Any]:
        action_input: dict[str, Any] = {}
        for item in context.kvPair():
            key = json.loads(item.STRING().getText())
            value_context = item.value()
            raw = value_context.getText()
            if value_context.STRING() is not None:
                value: Any = json.loads(raw)
            elif value_context.number() is not None:
                value = float(raw) if "." in raw else int(raw)
            else:
                value = raw
            action_input[key] = value
        return {
            "name": context.IDENTIFIER().getText(),
            "input": action_input,
        }

    @staticmethod
    def _tool_text(tool_input: Any) -> str:
        if isinstance(tool_input, str):
            return tool_input
        if isinstance(tool_input, dict):
            for key in ("code", "command", "input", "script"):
                value = tool_input.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
        return str(tool_input)

    def evaluate(
        self,
        *,
        action_name: str,
        user_input: Any,
        tool_input: Any,
        history: list[Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            return EnforcementDecision("allow").payload()
        input_text = self._tool_text(tool_input)
        parsed_action = parse_action_ast(action_name, tool_input)
        with action_ast_context(parsed_action):
            for policy in self.rules:
                if not policy.enabled or not policy.rule.triggered(action_name, input_text):
                    continue
                try:
                    matched = all(
                        self._evaluate_predicate(item, user_input, tool_input, history)
                        for item in policy.predicates
                    )
                except Exception as exc:
                    if not self.fail_closed:
                        logger.exception("OpsShield predicate failed open for rule {}", policy.id)
                        continue
                    return EnforcementDecision(
                        "stop",
                        rule_id=policy.id,
                        rationale=f"Predicate evaluation failed: {exc}",
                    ).payload()
                if not matched:
                    continue
                return EnforcementDecision(
                    policy.enforcement,
                    rule_id=policy.id,
                    replacement_action=policy.replacement_action,
                    rationale=f"OpsShield rule {policy.id} matched {action_name}",
                ).payload()
        return EnforcementDecision("allow").payload()

    @staticmethod
    def _evaluate_predicate(
        expression: PredicateExpression,
        user_input: Any,
        tool_input: Any,
        history: list[Any],
    ) -> bool:
        if expression.literal is not None:
            result = expression.literal
        else:
            if expression.name not in predicate_table:
                raise AgentSpecError(f"Predicate is not registered: {expression.name}")
            result = bool(predicate_table[expression.name](user_input, tool_input, history))
        return not result if expression.negate else result

    def bind_notifier(self, session_key: str, notifier: InspectionNotifier) -> None:
        self._notifiers[session_key] = notifier

    def unbind_notifier(self, session_key: str, notifier: InspectionNotifier) -> None:
        if self._notifiers.get(session_key) is notifier:
            self._notifiers.pop(session_key, None)

    async def ask_human(
        self,
        *,
        session_key: str,
        rule_id: str,
        action: dict[str, Any],
    ) -> bool:
        notifier = self._notifiers.get(session_key)
        if notifier is None:
            if self.fail_closed:
                return False
            return True
        loop = asyncio.get_running_loop()
        inspection = PendingInspection(
            id=uuid.uuid4().hex,
            session_key=session_key,
            rule_id=rule_id,
            action=action,
            created_at=time.time(),
            future=loop.create_future(),
        )
        self._pending[inspection.id] = inspection
        try:
            await notifier(inspection.payload())
            return await asyncio.wait_for(
                inspection.future,
                timeout=max(1.0, self.inspection_timeout),
            )
        except asyncio.TimeoutError:
            return False
        except Exception:
            logger.exception(
                "OpsShield user inspection failed closed for rule {}",
                rule_id,
            )
            return False
        finally:
            self._pending.pop(inspection.id, None)

    def resolve_inspection(self, inspection_id: str, approved: bool) -> bool:
        inspection = self._pending.get(inspection_id)
        if inspection is None or inspection.future.done():
            return False
        inspection.future.set_result(bool(approved))
        return True

    def pending_inspections(self) -> list[dict[str, Any]]:
        return [item.payload() for item in self._pending.values()]

    def set_enabled(self, rule_id: str, enabled: bool) -> None:
        if enabled:
            self._disabled_ids.discard(rule_id)
        else:
            self._disabled_ids.add(rule_id)
        self._save_state()
        self.reload()

    def save_rule(self, text: str, *, filename: str | None = None) -> PolicyRule:
        if self.rules_dir is None:
            raise AgentSpecError("Custom OpsShield rule directory is not configured")
        parsed = self.parse_text(text)
        if len(parsed) != 1:
            raise AgentSpecError("Save one OpsShield rule per file")
        policy = parsed[0]
        existing = next(
            (item for item in self.rules if item.id == policy.id and item.source == "builtin"),
            None,
        )
        if existing is not None:
            raise AgentSpecError(f"Built-in rule id cannot be replaced: {policy.id}")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", filename or policy.id).strip(".-")
        if not safe_name:
            safe_name = policy.id
        if not safe_name.endswith(".rule"):
            safe_name += ".rule"
        path = self.rules_dir / safe_name
        duplicate = next(
            (
                item
                for item in self.rules
                if item.id == policy.id and item.path is not None and item.path != path
            ),
            None,
        )
        if duplicate is not None:
            raise AgentSpecError(f"Rule id already exists: {policy.id}")
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        self.reload()
        return next(item for item in self.rules if item.path == path)

    def update_rule(self, rule_id: str, text: str) -> PolicyRule:
        current = next(
            (item for item in self.rules if item.id == rule_id and item.source == "custom"),
            None,
        )
        if current is None or current.path is None:
            raise AgentSpecError("Custom rule not found")
        parsed = self.parse_text(text)
        if len(parsed) != 1:
            raise AgentSpecError("Save one OpsShield rule per file")
        if parsed[0].id != rule_id:
            raise AgentSpecError("Rule id cannot be changed while editing an existing rule")
        return self.save_rule(text, filename=current.path.name)

    def delete_rule(self, rule_id: str) -> bool:
        policy = next(
            (item for item in self.rules if item.id == rule_id and item.source == "custom"),
            None,
        )
        if policy is None or policy.path is None:
            return False
        policy.path.unlink(missing_ok=True)
        self._disabled_ids.discard(rule_id)
        self._save_state()
        self.reload()
        return True

    def save_predicate(self, source: str, *, expected_name: str | None = None) -> str:
        if self.predicates_dir is None:
            raise AgentSpecError("Custom OpsShield Predicate directory is not configured")
        self.predicates_dir.mkdir(parents=True, exist_ok=True)
        try:
            name, _ = compile_predicate_source(source, expected_name=expected_name)
        except ValueError as exc:
            raise AgentSpecError(str(exc)) from exc
        if expected_name is None and name in predicate_table:
            raise AgentSpecError(f"Predicate name already exists: {name}")
        path = self.predicates_dir / f"{name}.py"
        path.write_text(source.rstrip() + "\n", encoding="utf-8")
        self.reload()
        if not any(item["name"] == name for item in predicate_catalog()):
            error = next(
                (item["message"] for item in self._predicate_errors if item["file"] == str(path)),
                "Predicate failed to register",
            )
            raise AgentSpecError(error)
        return name

    def delete_predicate(self, name: str) -> bool:
        if self.predicates_dir is None or name not in self._custom_predicate_names:
            return False
        referenced_by = [
            policy.id
            for policy in self.rules
            if any(expression.name == name for expression in policy.predicates)
        ]
        if referenced_by:
            raise AgentSpecError(
                f"Predicate is still referenced by rules: {', '.join(referenced_by)}"
            )
        path = self.predicates_dir / f"{name}.py"
        path.unlink(missing_ok=True)
        self.reload()
        return True

    def payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rules": [item.payload() for item in self.rules],
            "errors": list(self.errors),
            "registered_predicates": sorted(predicate_table),
            "predicates": predicate_catalog(),
            "pending_inspections": self.pending_inspections(),
            "trigger_naming": "Trigger must exactly match the runtime tool name.",
        }
