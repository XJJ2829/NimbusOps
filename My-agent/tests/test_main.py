from __future__ import annotations

import pytest

from my_agent import __main__ as cli


def test_websocket_runtime_accepts_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "find_spec",
        lambda name: object() if name == "websockets" else None,
    )

    cli._require_websocket_runtime()


def test_websocket_runtime_accepts_wsproto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "find_spec",
        lambda name: object() if name == "wsproto" else None,
    )

    cli._require_websocket_runtime()


def test_websocket_runtime_rejects_missing_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "find_spec", lambda _name: None)

    with pytest.raises(SystemExit, match="python -m pip install -e"):
        cli._require_websocket_runtime()
