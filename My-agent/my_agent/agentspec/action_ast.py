"""Action AST models for OpsShield predicate evaluation."""

from __future__ import annotations

import ast
import json
import shlex
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal

ActionAstKind = Literal["shell", "python", "structured", "text", "unknown"]


@dataclass(frozen=True, slots=True)
class ShellRedirection:
    operator: str
    target: str | None = None


@dataclass(frozen=True, slots=True)
class ShellCommand:
    words: tuple[str, ...]
    redirections: tuple[ShellRedirection, ...] = ()
    children: tuple["ShellScript", ...] = ()

    @property
    def program(self) -> str | None:
        for word in self.words:
            if _is_assignment(word):
                continue
            return word
        return None

    @property
    def arguments(self) -> tuple[str, ...]:
        arguments: list[str] = []
        program_seen = False
        for word in self.words:
            if not program_seen and _is_assignment(word):
                continue
            if not program_seen:
                program_seen = True
                continue
            arguments.append(word)
        return tuple(arguments)


@dataclass(frozen=True, slots=True)
class ShellScript:
    commands: tuple[ShellCommand, ...]


@dataclass(frozen=True, slots=True)
class ActionAst:
    action_name: str
    kind: ActionAstKind
    raw: Any
    tree: ShellScript | ast.AST | dict[str, Any] | str | None
    parse_error: str | None = None


_SHELL_SEPARATORS = frozenset({";", "\n", "&&", "||", "|", "|&", "&", ";;", ";&", ";;&"})
_REDIRECTION_OPERATORS = frozenset({">", ">>", "<", "<<", "<<<", "<>", ">&", "<&", "&>", "&>>"})
_PUNCTUATION = tuple(
    sorted(
        _SHELL_SEPARATORS | _REDIRECTION_OPERATORS | {"(", ")"},
        key=len,
        reverse=True,
    )
)

_DIRECT_DESTRUCTIVE_PROGRAMS = frozenset(
    {
        "dd",
        "diskutil",
        "fdisk",
        "halt",
        "parted",
        "poweroff",
        "reboot",
        "rm",
        "rmdir",
        "shred",
        "shutdown",
        "sudo",
        "truncate",
        "unlink",
        "wipefs",
    }
)
_SHELL_INTERPRETERS = frozenset({"ash", "bash", "dash", "ksh", "sh", "zsh"})
_COMMAND_WRAPPERS = frozenset({"builtin", "command", "env", "exec", "nice", "nohup", "timeout"})
_WRAPPER_OPTIONS_WITH_VALUE = frozenset(
    {
        "--argv0",
        "--chdir",
        "--default-signal",
        "--ignore-signal",
        "--kill-after",
        "--preserve-status",
        "--signal",
        "--unset",
        "-a",
        "-C",
        "-k",
        "-n",
        "-s",
        "-u",
    }
)
_XARGS_OPTIONS_WITH_VALUE = frozenset(
    {
        "--arg-file",
        "--delimiter",
        "--max-args",
        "--max-chars",
        "--max-procs",
        "--replace",
        "-a",
        "-d",
        "-E",
        "-I",
        "-L",
        "-n",
        "-P",
        "-s",
    }
)
_BLOCK_DEVICE_PREFIXES = (
    "/dev/disk",
    "/dev/hd",
    "/dev/mmcblk",
    "/dev/nvme",
    "/dev/rdisk",
    "/dev/sd",
    "/dev/vd",
)

_PYTHON_DESTRUCTIVE_CALLS = frozenset(
    {
        "os.remove",
        "os.removedirs",
        "os.rmdir",
        "os.unlink",
        "pathlib.Path.rmdir",
        "pathlib.Path.unlink",
        "shutil.rmtree",
    }
)
_PYTHON_SHELL_CALLS = frozenset(
    {
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.popen",
        "os.system",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.run",
    }
)


def parse_action_ast(action_name: str, tool_input: Any) -> ActionAst:
    """Return the structured action representation visible to Predicates."""
    if isinstance(tool_input, dict):
        shell_source = _first_string(tool_input, ("command", "cmd"))
        if shell_source is not None:
            return _parse_shell_action(action_name, tool_input, shell_source)

        python_source = _first_string(tool_input, ("code", "script"))
        if python_source is not None:
            return _parse_python_action(action_name, tool_input, python_source)

        return ActionAst(
            action_name=action_name,
            kind="structured",
            raw=tool_input,
            tree=dict(tool_input),
        )

    if isinstance(tool_input, str):
        if action_name.lower() in {"pythonrepl", "python", "python_repl"}:
            return _parse_python_action(action_name, tool_input, tool_input)
        if action_name.lower() in {"exec", "shell", "terminal"}:
            return _parse_shell_action(action_name, tool_input, tool_input)
        return ActionAst(action_name=action_name, kind="text", raw=tool_input, tree=tool_input)

    return ActionAst(action_name=action_name, kind="unknown", raw=tool_input, tree=None)


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _parse_shell_action(action_name: str, raw: Any, source: str) -> ActionAst:
    try:
        return ActionAst(
            action_name=action_name,
            kind="shell",
            raw=raw,
            tree=parse_shell_source(source),
        )
    except ValueError as exc:
        return ActionAst(
            action_name=action_name,
            kind="shell",
            raw=raw,
            tree=None,
            parse_error=str(exc),
        )


def _parse_python_action(action_name: str, raw: Any, source: str) -> ActionAst:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        location = f"line {exc.lineno}, column {exc.offset}" if exc.lineno else "unknown location"
        return ActionAst(
            action_name=action_name,
            kind="python",
            raw=raw,
            tree=None,
            parse_error=f"Syntax error at {location}: {exc.msg}",
        )
    return ActionAst(action_name=action_name, kind="python", raw=raw, tree=tree)


def parse_shell_source(source: str) -> ShellScript:
    """Parse shell-like tool input into a small command AST."""
    lexer = shlex.shlex(source, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.commenters = "#"
    lexer.wordchars += "${}"
    raw_tokens = list(lexer)
    tokens: list[str] = []
    for token in raw_tokens:
        tokens.extend(_split_punctuation(token))
    script, index = _parse_shell_tokens(tokens, 0, stop_at_parenthesis=False)
    if index != len(tokens):
        raise ValueError("unexpected token after shell parse")
    return script


def _split_punctuation(token: str) -> list[str]:
    if not token or any(character not in "();<>|&\n" for character in token):
        return [token]

    parts: list[str] = []
    index = 0
    while index < len(token):
        match = next(
            (item for item in _PUNCTUATION if token.startswith(item, index)),
            None,
        )
        if match is None:
            parts.append(token[index])
            index += 1
            continue
        parts.append(match)
        index += len(match)
    return parts


def _parse_shell_tokens(
    tokens: list[str],
    index: int,
    *,
    stop_at_parenthesis: bool,
) -> tuple[ShellScript, int]:
    commands: list[ShellCommand] = []
    words: list[str] = []
    redirections: list[ShellRedirection] = []
    children: list[ShellScript] = []

    def flush() -> None:
        if not words and not redirections and not children:
            return
        commands.append(
            ShellCommand(
                words=tuple(words),
                redirections=tuple(redirections),
                children=tuple(children),
            )
        )
        words.clear()
        redirections.clear()
        children.clear()

    while index < len(tokens):
        token = tokens[index]
        if token == ")":
            if not stop_at_parenthesis:
                raise ValueError("unexpected closing parenthesis")
            flush()
            return ShellScript(tuple(commands)), index + 1
        if token == "(":
            child, index = _parse_shell_tokens(tokens, index + 1, stop_at_parenthesis=True)
            children.append(child)
            continue
        if token in _SHELL_SEPARATORS:
            flush()
            index += 1
            continue
        if token in _REDIRECTION_OPERATORS:
            if words and words[-1].isdigit():
                words.pop()
            target = None
            if index + 1 < len(tokens):
                candidate = tokens[index + 1]
                if candidate not in _SHELL_SEPARATORS | _REDIRECTION_OPERATORS | {"(", ")"}:
                    target = candidate
                    index += 1
            redirections.append(ShellRedirection(operator=token, target=target))
            index += 1
            continue
        words.append(token)
        index += 1

    if stop_at_parenthesis:
        raise ValueError("unclosed parenthesis")
    flush()
    return ShellScript(tuple(commands)), index


def _is_assignment(word: str) -> bool:
    name, separator, _ = word.partition("=")
    return bool(
        separator
        and name
        and (name[0].isalpha() or name[0] == "_")
        and all(character.isalnum() or character == "_" for character in name)
    )


def action_ast_is_destructive(action: ActionAst) -> bool:
    """Evaluate destructive behavior from an already parsed action AST."""
    if action.kind in {"shell", "python"} and action.parse_error:
        return True
    if action.kind == "shell" and isinstance(action.tree, ShellScript):
        return shell_ast_is_destructive(action.tree)
    if action.kind == "python" and isinstance(action.tree, ast.AST):
        return python_ast_is_destructive(action.tree)
    return False


def shell_ast_is_destructive(
    script: ShellScript,
    variables: dict[str, str] | None = None,
) -> bool:
    local_variables = dict(variables or {})
    for command in script.commands:
        if _shell_command_is_destructive(command, local_variables):
            return True
        _record_shell_assignments(command, local_variables)
    return False


def _shell_command_is_destructive(
    command: ShellCommand,
    variables: dict[str, str] | None = None,
) -> bool:
    variables = variables or {}
    if any(shell_ast_is_destructive(child, variables) for child in command.children):
        return True
    if any(_redirection_is_destructive(item) for item in command.redirections):
        return True

    program = _program_name(command.program, variables)
    if program is None:
        return False
    arguments = list(command.arguments)

    if program in _DIRECT_DESTRUCTIVE_PROGRAMS or program.startswith("mkfs."):
        return True
    if program in _COMMAND_WRAPPERS:
        nested = _unwrap_command(arguments)
        return bool(nested and _shell_words_are_destructive(nested))
    if program in _SHELL_INTERPRETERS:
        script_source = _option_value(arguments, "-c")
        return bool(script_source and _shell_source_is_destructive(script_source))
    if program == "eval":
        return bool(arguments and _shell_source_is_destructive(" ".join(arguments)))
    if program == "find":
        return _find_is_destructive(arguments)
    if program == "xargs":
        nested = _xargs_command(arguments)
        return bool(nested and _shell_words_are_destructive(nested))
    if program == "git":
        return _git_is_destructive(arguments)
    if program in {"docker", "podman"}:
        return _container_cli_is_destructive(arguments)
    if program == "kubectl":
        return _kubectl_is_destructive(arguments)
    if program in {"python", "python3"} or program.startswith(("python2.", "python3.")):
        code = _option_value(arguments, "-c")
        return bool(code and _python_source_is_destructive(code))
    return False


def _shell_words_are_destructive(words: list[str]) -> bool:
    return _shell_command_is_destructive(ShellCommand(words=tuple(words)))


def _shell_source_is_destructive(source: str) -> bool:
    try:
        return shell_ast_is_destructive(parse_shell_source(source))
    except ValueError:
        return True


def _python_source_is_destructive(source: str) -> bool:
    try:
        return python_ast_is_destructive(ast.parse(source))
    except SyntaxError:
        return True


def _redirection_is_destructive(redirection: ShellRedirection) -> bool:
    if redirection.operator not in {">", ">>", "<>", ">&", "&>", "&>>"}:
        return False
    return bool(redirection.target and redirection.target.startswith(_BLOCK_DEVICE_PREFIXES))


def _program_name(value: str | None, variables: dict[str, str] | None = None) -> str | None:
    if not value:
        return None
    variables = variables or {}
    if value.startswith("${") and value.endswith("}"):
        value = variables.get(value[2:-1], value)
    elif value.startswith("$"):
        value = variables.get(value[1:], value)
    if any(marker in value for marker in ("$", "`")):
        return None
    return PurePath(value).name.lower()


def _record_shell_assignments(command: ShellCommand, variables: dict[str, str]) -> None:
    if command.program is not None or command.redirections or command.children:
        return
    for word in command.words:
        name, _, value = word.partition("=")
        if not _is_assignment(word) or any(marker in value for marker in ("$", "`")):
            continue
        variables[name] = value


def _unwrap_command(arguments: list[str]) -> list[str]:
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            return arguments[index + 1 :]
        if _is_assignment(value):
            index += 1
            continue
        if not value.startswith("-") or value == "-":
            return arguments[index:]
        option = value.split("=", 1)[0]
        index += 1
        if option in _WRAPPER_OPTIONS_WITH_VALUE and "=" not in value:
            index += 1
    return []


def _option_value(arguments: list[str], option: str) -> str | None:
    for index, value in enumerate(arguments):
        if value == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(option) and value != option:
            return value[len(option) :]
    return None


def _find_is_destructive(arguments: list[str]) -> bool:
    for index, value in enumerate(arguments):
        if value == "-delete":
            return True
        if value in {"-exec", "-execdir", "-ok", "-okdir"} and index + 1 < len(arguments):
            nested: list[str] = []
            for item in arguments[index + 1 :]:
                if item in {";", "+"}:
                    break
                nested.append(item)
            if nested and _shell_words_are_destructive(nested):
                return True
    return False


def _xargs_command(arguments: list[str]) -> list[str]:
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            return arguments[index + 1 :]
        if not value.startswith("-") or value == "-":
            return arguments[index:]
        option = value.split("=", 1)[0]
        index += 1
        if option in _XARGS_OPTIONS_WITH_VALUE and "=" not in value:
            index += 1
    return []


def _git_is_destructive(arguments: list[str]) -> bool:
    if not arguments:
        return False
    if arguments[0] == "clean":
        return any("f" in value.lstrip("-") for value in arguments[1:] if value.startswith("-"))
    if arguments[0] == "reset":
        return "--hard" in arguments[1:]
    return False


def _container_cli_is_destructive(arguments: list[str]) -> bool:
    if not arguments:
        return False
    if arguments[0] in {"rm", "rmi"}:
        return True
    return (
        len(arguments) >= 2
        and arguments[0] in {"container", "image", "system", "volume"}
        and arguments[1] == "prune"
    )


def _kubectl_is_destructive(arguments: list[str]) -> bool:
    verbs = [value.lower() for value in arguments if value and not value.startswith("-")]
    return bool(verbs and verbs[0] in {"delete", "drain"})


def python_ast_is_destructive(tree: ast.AST) -> bool:
    visitor = _PythonDestructiveVisitor()
    visitor.visit(tree)
    return visitor.destructive


@dataclass(slots=True)
class _PythonDestructiveVisitor(ast.NodeVisitor):
    aliases: dict[str, str] = field(default_factory=dict)
    constants: dict[str, Any] = field(default_factory=dict)
    destructive: bool = False

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module is None:
            return
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        value = self._literal(node.value)
        qualified = self._qualified_name(node.value)
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if value is not None:
                self.constants[target.id] = value
            elif qualified is not None:
                self.aliases[target.id] = qualified
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if isinstance(node.target, ast.Name) and node.value is not None:
            value = self._literal(node.value)
            if value is not None:
                self.constants[node.target.id] = value
        if node.value is not None:
            self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if self.destructive:
            return
        qualified = self._qualified_name(node.func)
        if qualified in _PYTHON_DESTRUCTIVE_CALLS or self._is_destructive_path_method(qualified):
            self.destructive = True
            return
        if qualified in _PYTHON_SHELL_CALLS and self._shell_call_is_destructive(qualified, node):
            self.destructive = True
            return
        if qualified in {"eval", "exec"} and node.args:
            source = self._literal(node.args[0])
            if isinstance(source, str) and _python_source_is_destructive(source):
                self.destructive = True
                return
        self.generic_visit(node)

    @staticmethod
    def _is_destructive_path_method(qualified: str | None) -> bool:
        return bool(qualified and qualified.rsplit(".", 1)[-1] in {"rmdir", "unlink"})

    def _shell_call_is_destructive(self, qualified: str, node: ast.Call) -> bool:
        if not node.args:
            return qualified in {"os.popen", "os.system"}
        value = self._literal(node.args[0])
        if qualified.endswith("_exec"):
            words = [self._literal(argument) for argument in node.args]
            if words and all(isinstance(item, str) for item in words):
                return _shell_words_are_destructive([str(item) for item in words])
            return False
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return _shell_words_are_destructive(list(value))
        if isinstance(value, str):
            return _shell_source_is_destructive(value)
        return qualified in {"os.popen", "os.system"}

    def _qualified_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = self._qualified_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        if isinstance(node, ast.Call):
            function = self._qualified_name(node.func)
            if function == "getattr" and len(node.args) >= 2:
                parent = self._qualified_name(node.args[0])
                attribute = self._literal(node.args[1])
                if parent and isinstance(attribute, str):
                    return f"{parent}.{attribute}"
            if function == "__import__" and node.args:
                module = self._literal(node.args[0])
                if isinstance(module, str):
                    return module
            return function
        return None

    def _literal(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return self.constants.get(node.id)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [self._literal(item) for item in node.elts]
            if any(value is None for value in values):
                return None
            return values if isinstance(node, ast.List) else tuple(values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._literal(node.left)
            right = self._literal(node.right)
            if isinstance(left, type(right)) and isinstance(left, (str, list, tuple)):
                return left + right
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    return None
                parts.append(value.value)
            return "".join(parts)
        return None


def action_ast_to_jsonable(action: ActionAst) -> dict[str, Any]:
    """Serialize an ActionAst for debugging and API payloads."""
    tree: Any
    if isinstance(action.tree, ShellScript):
        tree = _shell_script_to_jsonable(action.tree)
    elif isinstance(action.tree, ast.AST):
        tree = ast.dump(action.tree, include_attributes=False)
    elif isinstance(action.tree, dict):
        tree = json.loads(json.dumps(action.tree, ensure_ascii=False, default=str))
    else:
        tree = action.tree
    return {
        "action_name": action.action_name,
        "kind": action.kind,
        "tree": tree,
        "parse_error": action.parse_error,
    }


def _shell_script_to_jsonable(script: ShellScript) -> dict[str, Any]:
    return {
        "commands": [
            {
                "program": command.program,
                "arguments": list(command.arguments),
                "words": list(command.words),
                "redirections": [
                    {"operator": item.operator, "target": item.target}
                    for item in command.redirections
                ],
                "children": [
                    _shell_script_to_jsonable(child)
                    for child in command.children
                ],
            }
            for command in script.commands
        ]
    }
