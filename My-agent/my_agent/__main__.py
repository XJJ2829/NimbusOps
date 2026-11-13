"""Command line entry point for My-agent."""

from __future__ import annotations

import argparse
from importlib.util import find_spec
from pathlib import Path

import uvicorn

from my_agent.config.loader import load_config, save_config, set_config_path
from my_agent.security.runtime_identity import (
    RuntimeIdentityError,
    enforce_startup_identity,
)
from my_agent.server.app import create_app


def _default_config_path() -> Path:
    return Path.home() / ".my-agent" / "config.json"


def _require_websocket_runtime() -> None:
    if find_spec("websockets") is not None or find_spec("wsproto") is not None:
        return
    raise SystemExit(
        "My-agent requires WebSocket support, but no WebSocket runtime is installed.\n"
        "Install the project dependencies in the active virtual environment:\n"
        '  python -m pip install -e "."'
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="my-agent")
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="Start the B/S server.")
    serve.add_argument("--config", type=Path, default=_default_config_path())
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="Enable uvicorn reload.")

    init_config = subcommands.add_parser("init-config", help="Create a default config file.")
    init_config.add_argument("--config", type=Path, default=_default_config_path())
    init_config.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "init-config":
        config_path = args.config.expanduser()
        set_config_path(config_path)
        if config_path.exists() and not args.force:
            print(f"Config already exists: {config_path}")
            return
        save_config(load_config(config_path), config_path)
        print(f"Created config: {config_path}")
        return

    config_path = args.config.expanduser()
    set_config_path(config_path)
    config = load_config(config_path)
    try:
        enforce_startup_identity(config.security.runtime_identity)
    except RuntimeIdentityError as exc:
        raise SystemExit(str(exc)) from exc
    _require_websocket_runtime()
    app = create_app(config_path)
    uvicorn.run(
        app,
        host=args.host or config.gateway.host,
        port=args.port or config.gateway.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
