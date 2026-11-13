"""Pydantic models for HostOpsBench.

These types are the *contract* between the four moving parts of a run:

* the **Scenario** file (declarative: environment, fault, ground truth, scoring);
* the **Agent** under test (produces an :class:`AgentResult`);
* the **Controller** (produces the :class:`TimelineEvent` trace and probe results);
* the **Scorer** (turns all of the above into an :class:`EpisodeResult`).

Nothing here imports ``mcp_servers``; the benchmark treats the system under test
as a black box reached only over MCP.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warn", "critical"]
Outcome = Literal["recovered", "not_recovered", "regressed", "inconclusive", "not_applicable"]
ScenarioKind = Literal["diagnosis", "closed_loop"]


# --------------------------------------------------------------------------- #
# Scenario definition (loaded from YAML)
# --------------------------------------------------------------------------- #
class ProbeSpec(BaseModel):
    """A single machine-checkable predicate over a tool/resource result.

    ``call`` names an MCP tool (or ``resource:<uri>``); ``args`` are its
    arguments; ``expect`` is a JSONPath-lite predicate evaluated against the
    returned JSON. Probes are used both for the steady-state baseline and to
    confirm a fault actually took effect before the agent is invoked.
    """

    id: str
    description: str = ""
    call: str
    args: dict[str, Any] = Field(default_factory=dict)
    path: str
    op: Literal["gt", "ge", "lt", "le", "eq", "ne", "contains", "not_contains", "truthy", "falsy"]
    value: Any = None


class FaultSpec(BaseModel):
    """Which fault injector to run and with what parameters."""

    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class WorkloadSpec(BaseModel):
    enabled: bool = False
    request_rate: float = 5.0
    write_kb: int = 128
    baseline_s: float = 3.0
    fault_window_s: float = 4.0
    require_latency_ratio: float = 0.0


class RootCauseTruth(BaseModel):
    code: str
    entity: str
    aliases: list[str] = Field(default_factory=list)


class EvidenceTruth(BaseModel):
    """A signal the agent is expected to have grounded its conclusion on."""

    id: str
    description: str
    call: str
    path: str
    op: Literal["gt", "ge", "lt", "le", "eq", "ne", "contains", "truthy", "falsy"]
    value: Any = None
    required: bool = True


class GroundTruth(BaseModel):
    detected: bool = True
    root_causes: list[RootCauseTruth]
    affected_entities: list[str] = Field(default_factory=list)
    propagation_chain: list[list[str]] = Field(default_factory=list)
    required_evidence: list[EvidenceTruth] = Field(default_factory=list)
    acceptable_remediation: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
    diagnosis: float = 0.0
    remediation: float = 0.0
    safety: float = 0.0
    evidence: float = 0.0
    efficiency: float = 0.0

    def normalized(self) -> ScoringWeights:
        total = self.diagnosis + self.remediation + self.safety + self.evidence + self.efficiency
        if total <= 0:
            raise ValueError("scoring weights must sum to a positive number")
        return ScoringWeights(
            diagnosis=self.diagnosis / total,
            remediation=self.remediation / total,
            safety=self.safety / total,
            evidence=self.evidence / total,
            efficiency=self.efficiency / total,
        )


class InteractiveSpec(BaseModel):
    """Optional overrides for the interactive, human-driven session scorer.

    When omitted, the interactive scorer derives sensible defaults from the
    scenario ``kind`` (``remediation`` for ``closed_loop``, ``diagnosis`` for
    read-only scenarios) and reuses ``recovery_probes`` as the resolved-state
    goal. Provide this block to convert a read-only scenario into a fixable one
    (set ``mode: remediation`` and list ``resolved_probes``) or to protect data
    with ``invariant_probes``.
    """

    mode: Literal["remediation", "diagnosis"] | None = None
    resolved_probes: list[ProbeSpec] = Field(default_factory=list)
    invariant_probes: list[ProbeSpec] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)


class Scenario(BaseModel):
    api_version: str = Field(alias="api_version")
    kind: ScenarioKind
    id: str
    title: str
    version: str = "0.1.0"
    description: str = ""
    symptom: str = ""
    agent: str
    required_capabilities: list[str] = Field(default_factory=list)
    tool_budget: int = 40
    time_budget_s: float = 300.0
    workload: WorkloadSpec = Field(default_factory=WorkloadSpec)
    baseline_probes: list[ProbeSpec] = Field(default_factory=list)
    fault: FaultSpec
    fault_effect_probes: list[ProbeSpec] = Field(default_factory=list)
    recovery_probes: list[ProbeSpec] = Field(default_factory=list)
    ground_truth: GroundTruth
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    scoring: ScoringWeights
    interactive: InteractiveSpec | None = None

    model_config = {"populate_by_name": True}


# --------------------------------------------------------------------------- #
# Runtime trace + agent output
# --------------------------------------------------------------------------- #
class TimelineEvent(BaseModel):
    """One recorded interaction on the MCP (or CLI) timeline."""

    seq: int
    t_offset_s: float
    channel: Literal["mcp", "cli"] = "mcp"
    kind: Literal["call", "read_resource", "get_prompt"] = "call"
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    status: str | None = None
    error: str | None = None
    duration_ms: float = 0.0


class EvidenceClaim(BaseModel):
    """An observation the agent explicitly cites to support a conclusion."""

    id: str
    source_call: str
    claim: str
    value: Any = None


class ReportedRootCause(BaseModel):
    code: str
    entity: str
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)


class RemediationReport(BaseModel):
    attempted: bool = False
    actions: list[str] = Field(default_factory=list)
    outcome: Outcome = "not_applicable"


class AgentResult(BaseModel):
    detected: bool = False
    affected_entities: list[str] = Field(default_factory=list)
    root_causes: list[ReportedRootCause] = Field(default_factory=list)
    propagation_chain: list[list[str]] = Field(default_factory=list)
    evidence: list[EvidenceClaim] = Field(default_factory=list)
    remediation: RemediationReport = Field(default_factory=RemediationReport)
    limitations: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Scoring output
# --------------------------------------------------------------------------- #
class SafetyGate(BaseModel):
    passed: bool = True
    violations: list[str] = Field(default_factory=list)


class MetricBlock(BaseModel):
    score: float
    detail: dict[str, Any] = Field(default_factory=dict)


class EpisodeResult(BaseModel):
    scenario_id: str
    scenario_version: str
    kind: ScenarioKind
    agent: str
    seed: int
    started_at: str
    duration_s: float
    valid: bool = True
    invalid_reason: str | None = None
    baseline_ok: bool = False
    fault_effect_ok: bool = False
    safety: SafetyGate = Field(default_factory=SafetyGate)
    metrics: dict[str, MetricBlock] = Field(default_factory=dict)
    weighted_score: float = 0.0
    episode_score: float = 0.0
    grade: str = "F"
    capabilities: dict[str, Any] = Field(default_factory=dict)
    probe_results: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    workload_results: dict[str, Any] = Field(default_factory=dict)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    agent_result: AgentResult = Field(default_factory=AgentResult)
    notes: list[str] = Field(default_factory=list)
