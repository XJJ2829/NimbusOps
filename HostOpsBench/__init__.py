"""HostOpsBench: a host-operations Agent benchmark for the Operations Agent MCP.

The package is intentionally *separate* from ``mcp_servers`` (the system under
test) and from the CLI apps: it only drives the running gateway over MCP,
injects reversible user-space faults, records the tool-call timeline and scores
the episode against a machine-checkable ground truth.
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
