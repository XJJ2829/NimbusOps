"""A blocking, trace-recording MCP client used by the benchmark.

It is adapted from the test harness' ``SyncMCP`` but every interaction is
appended to a shared :class:`~benchmark.models.TimelineEvent` list so the scorer
can reason about the *whole* tool-call sequence, not just the final answer.

The recorder is deliberately the only way the agent reaches the gateway: the
agent is handed a :class:`RecordingMCP`, never the fault injector or the
controller, so it cannot peek at ground truth.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .models import TimelineEvent


def wait_until_up(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code in (200, 400, 406):
                return
        except Exception as exc:  # noqa: BLE001 - retry until it boots
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"gateway did not come up at {url}: {last}")


class RecordingMCP:
    """Synchronous MCP client that records a timeline of every interaction."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.timeline: list[TimelineEvent] = []
        self._t0 = time.monotonic()
        self._seq = 0

    def _run(self, fn: Any) -> Any:
        async def main() -> Any:
            async with streamablehttp_client(self.url) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return await fn(s)

        return asyncio.run(main())

    def _record(
        self,
        kind: str,
        name: str,
        args: dict[str, Any],
        started: float,
        result: Any,
        error: str | None,
    ) -> None:
        self._seq += 1
        status = None
        if isinstance(result, dict):
            candidate = result.get("status")
            if isinstance(candidate, str):
                status = candidate
        self.timeline.append(
            TimelineEvent(
                seq=self._seq,
                t_offset_s=round(started - self._t0, 3),
                channel="mcp",
                kind=kind,  # type: ignore[arg-type]
                name=name,
                args=args,
                ok=error is None,
                status=status,
                error=error,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
        )

    @staticmethod
    def _decode(text: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        args = args or {}
        started = time.monotonic()
        error: str | None = None
        parsed: Any = None
        try:
            res = self._run(lambda s: s.call_tool(tool, args))
            text = res.content[0].text if res.content else "{}"
            parsed = self._decode(text)
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed event
            error = f"{type(exc).__name__}: {exc}"
        self._record("call", tool, args, started, parsed, error)
        if error is not None:
            raise RuntimeError(error)
        return parsed

    def read_resource(self, uri: str) -> Any:
        started = time.monotonic()
        error: str | None = None
        parsed: Any = None
        try:
            res = self._run(lambda s: s.read_resource(uri))
            text = res.contents[0].text if res.contents else "{}"
            parsed = self._decode(text)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        self._record("read_resource", uri, {}, started, parsed, error)
        if error is not None:
            raise RuntimeError(error)
        return parsed

    def tools(self) -> set[str]:
        res = self._run(lambda s: s.list_tools())
        return {t.name for t in res.tools}
