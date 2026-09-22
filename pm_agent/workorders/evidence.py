"""Shared per-turn evidence contracts for parallel BigQuery/IPC retrieval.

This module is the N1/P1 freeze point described in
``docs/next-agent-handover.md`` and ``docs/adk-parallel-evidence-plan.md``: one
typed, JSON-serializable :class:`EvidenceRequest` is resolved once per turn and
handed to every evidence branch (BigQuery tools, the IPC adapter, ...); each
branch returns exactly one :class:`SourceResult`. Only contracts and pure
(de)serialization/merge helpers live here — no BigQuery, IPC or ADK graph code.

Reused rather than duplicated: ``amos_data.parser.part_key`` for PN
normalization, and ``pm_agent.workorders.service.VALID_MODES`` /
``WorkOrderAnalysisError`` / ``_aware_timestamp`` for the same mode and
timestamp validation ``AnalysisInput`` already enforces.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any

from pm_agent.workorders.service import (
    VALID_MODES,
    WorkOrderAnalysisError,
    _aware_timestamp,
)

__all__ = [
    "AircraftContext",
    "ArtifactProvenance",
    "CounterValue",
    "EvidenceCounters",
    "EvidenceRequest",
    "ExclusionSet",
    "PartCandidate",
    "SourceResult",
    "SourceStatus",
    "build_join_payload",
    "merge_source_results",
]


class SourceStatus(StrEnum):
    """Outcome of one evidence branch. A ``str`` subclass so it round-trips
    through ``json.dumps`` as its plain value, e.g. ``"no_match"``."""

    SUCCESS = "success"  # Rows/excerpts were returned.
    NO_MATCH = "no_match"  # A successful query returned zero rows; NOT unavailable.
    UNAVAILABLE = "unavailable"  # Corpus/view/embeddings not deployed or loaded.
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    ERROR = "error"


def _to_jsonable(value: Any) -> Any:
    """Recursively coerce branch-supplied data into plain JSON-safe values."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_to_jsonable(item) for item in value]
        try:
            return sorted(items)
        except TypeError:  # Unorderable items still round-trip, just unsorted.
            return items
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"{type(value).__name__} is not JSON-serializable evidence data")


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    """Upload identity pinned the same way ``workorders/artifacts.py`` pins it.

    ``version`` 0 is a valid, real artifact version and must never be treated
    as falsy/missing; only ``is None`` distinguishes "no artifact".
    """

    filename: str
    version: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.filename:
            raise WorkOrderAnalysisError(
                "artifact filename must not be blank", code="invalid_artifact_reference"
            )
        if type(self.version) is not int or self.version < 0:
            raise WorkOrderAnalysisError(
                "artifact version must be a non-negative integer",
                code="invalid_artifact_reference",
            )
        if not self.sha256:
            raise WorkOrderAnalysisError(
                "artifact sha256 must not be blank", code="invalid_artifact_reference"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"filename": self.filename, "version": self.version, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ArtifactProvenance:
        return cls(
            filename=data["filename"], version=data["version"], sha256=data["sha256"]
        )


@dataclass(frozen=True, slots=True)
class PartCandidate:
    """One resolved target part, mirroring ``service._resolve_targets`` rows.

    ``part_key`` is the punctuation-stripped normalized form (see
    ``amos_data.parser.part_key``); ``part_number`` retains display formatting.
    """

    part_key: str
    part_number: str
    resolution_status: str = "resolved"
    roles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_key": self.part_key,
            "part_number": self.part_number,
            "resolution_status": self.resolution_status,
            "roles": list(self.roles),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PartCandidate:
        return cls(
            part_key=data["part_key"],
            part_number=data["part_number"],
            resolution_status=data.get("resolution_status", "resolved"),
            roles=tuple(data.get("roles", ())),
        )


@dataclass(frozen=True, slots=True)
class AircraftContext:
    """Aircraft/family/position context, all optional and independent."""

    aircraft_id: str | None = None
    family: str | None = None
    position: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "aircraft_id": self.aircraft_id,
            "family": self.family,
            "position": self.position,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AircraftContext:
        return cls(
            aircraft_id=data.get("aircraft_id"),
            family=data.get("family"),
            position=data.get("position"),
        )


@dataclass(frozen=True, slots=True)
class CounterValue:
    """One counter with the same value/source/observed_at/status shape as
    ``service._counter``, so callers can build these directly from
    ``AnalysisInput``/``analyze_xml`` output."""

    value: int | None
    source: str | None
    observed_at: str | None
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CounterValue:
        return cls(
            value=data.get("value"),
            source=data.get("source"),
            observed_at=data.get("observed_at"),
            status=data["status"],
        )


# The default counter state: a real, allowed state ("not supplied"), not an
# error. Missing current TAC must never block descriptive evidence retrieval.
_UNSET_COUNTER = CounterValue(value=None, source=None, observed_at=None, status="not_supplied")


@dataclass(frozen=True, slots=True)
class EvidenceCounters:
    """Issue, closing and supplied-current TAC, kept as distinct fields so a
    branch cannot confuse closing TAC (historical) with current TAC."""

    issue_tac: CounterValue = _UNSET_COUNTER
    closing_tac: CounterValue = _UNSET_COUNTER
    supplied_current_tac: CounterValue = _UNSET_COUNTER

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_tac": self.issue_tac.to_dict(),
            "closing_tac": self.closing_tac.to_dict(),
            "supplied_current_tac": self.supplied_current_tac.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceCounters:
        return cls(
            issue_tac=CounterValue.from_dict(data["issue_tac"])
            if data.get("issue_tac")
            else _UNSET_COUNTER,
            closing_tac=CounterValue.from_dict(data["closing_tac"])
            if data.get("closing_tac")
            else _UNSET_COUNTER,
            supplied_current_tac=CounterValue.from_dict(data["supplied_current_tac"])
            if data.get("supplied_current_tac")
            else _UNSET_COUNTER,
        )


@dataclass(frozen=True, slots=True)
class ExclusionSet:
    """Own-WO/self and future exclusions shared by every history branch."""

    workorder_ids: frozenset[str] = frozenset()
    record_ids: frozenset[str] = frozenset()
    text_hashes: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workorder_ids": sorted(self.workorder_ids),
            "record_ids": sorted(self.record_ids),
            "text_hashes": sorted(self.text_hashes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExclusionSet:
        return cls(
            workorder_ids=frozenset(data.get("workorder_ids", ())),
            record_ids=frozenset(data.get("record_ids", ())),
            text_hashes=frozenset(data.get("text_hashes", ())),
        )


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    """One per-turn request shared, unmodified, by every evidence branch.

    ``artifact`` is ``None`` for a combined maintenance question with no
    uploaded XML; every other field is always present, using the documented
    "not supplied"/empty states rather than ``None`` where a branch needs to
    tell "missing" apart from "empty result".
    """

    question: str
    input_mode: str
    analysis_as_of: str
    invocation_scope_id: str
    artifact: ArtifactProvenance | None = None
    selected_wo_id: str | None = None
    part_candidates: tuple[PartCandidate, ...] = ()
    aircraft: AircraftContext = field(default_factory=AircraftContext)
    available_symptoms: tuple[str, ...] = ()
    counters: EvidenceCounters = field(default_factory=EvidenceCounters)
    exclusions: ExclusionSet = field(default_factory=ExclusionSet)

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise WorkOrderAnalysisError(
                "question must not be blank", code="invalid_evidence_request"
            )
        if self.input_mode not in VALID_MODES:
            raise WorkOrderAnalysisError(
                "input_mode must be new_work_order or historical_replay",
                code="invalid_mode",
            )
        _aware_timestamp(self.analysis_as_of, "analysis_as_of")
        if not self.invocation_scope_id:
            raise WorkOrderAnalysisError(
                "invocation_scope_id must not be blank so one branch cannot "
                "reuse an earlier turn's evidence",
                code="invalid_evidence_request",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "input_mode": self.input_mode,
            "analysis_as_of": self.analysis_as_of,
            "invocation_scope_id": self.invocation_scope_id,
            "artifact": self.artifact.to_dict() if self.artifact is not None else None,
            "selected_wo_id": self.selected_wo_id,
            "part_candidates": [pc.to_dict() for pc in self.part_candidates],
            "aircraft": self.aircraft.to_dict(),
            "available_symptoms": list(self.available_symptoms),
            "counters": self.counters.to_dict(),
            "exclusions": self.exclusions.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceRequest:
        artifact = data.get("artifact")
        aircraft = data.get("aircraft")
        counters = data.get("counters")
        exclusions = data.get("exclusions")
        return cls(
            question=data["question"],
            input_mode=data["input_mode"],
            analysis_as_of=data["analysis_as_of"],
            invocation_scope_id=data["invocation_scope_id"],
            artifact=ArtifactProvenance.from_dict(artifact) if artifact else None,
            selected_wo_id=data.get("selected_wo_id"),
            part_candidates=tuple(
                PartCandidate.from_dict(pc) for pc in data.get("part_candidates", ())
            ),
            aircraft=AircraftContext.from_dict(aircraft) if aircraft else AircraftContext(),
            available_symptoms=tuple(data.get("available_symptoms", ())),
            counters=EvidenceCounters.from_dict(counters) if counters else EvidenceCounters(),
            exclusions=ExclusionSet.from_dict(exclusions) if exclusions else ExclusionSet(),
        )


@dataclass(frozen=True, slots=True)
class SourceResult:
    """One evidence branch's result. Every branch (BigQuery tool, IPC
    adapter, ...) returns exactly one of these; branches never share or
    mutate each other's instance.

    ``source`` identifies the branch/tool (e.g. ``"bigquery.get_workorder"``,
    ``"ipc_manual_retrieval"``) and is the join key in
    :func:`merge_source_results` — two results with the same ``source`` is a
    programming error, not a silent overwrite.
    """

    source: str
    status: SourceStatus
    records: tuple[Mapping[str, Any], ...] = ()
    source_ids: tuple[str, ...] = ()
    citations: tuple[Mapping[str, Any], ...] = ()
    executed_parameters: Mapping[str, Any] = field(default_factory=dict)
    counts: Mapping[str, int] = field(default_factory=dict)
    truncated: bool = False
    limit: int | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise WorkOrderAnalysisError(
                "source must not be blank", code="invalid_source_result"
            )
        if not isinstance(self.status, SourceStatus):
            # Accept the plain string form so from_dict/tool code can pass
            # either a SourceStatus or its value without extra ceremony.
            object.__setattr__(self, "status", SourceStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status.value,
            "records": [_to_jsonable(record) for record in self.records],
            "source_ids": list(self.source_ids),
            "citations": [_to_jsonable(citation) for citation in self.citations],
            "executed_parameters": _to_jsonable(dict(self.executed_parameters)),
            "counts": dict(self.counts),
            "truncated": self.truncated,
            "limit": self.limit,
            "error_detail": self.error_detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceResult:
        return cls(
            source=data["source"],
            status=SourceStatus(data["status"]),
            records=tuple(data.get("records", ())),
            source_ids=tuple(data.get("source_ids", ())),
            citations=tuple(data.get("citations", ())),
            executed_parameters=dict(data.get("executed_parameters") or {}),
            counts=dict(data.get("counts") or {}),
            truncated=bool(data.get("truncated", False)),
            limit=data.get("limit"),
            error_detail=data.get("error_detail"),
        )


def merge_source_results(results: Iterable[SourceResult]) -> dict[str, Any]:
    """Join per-branch results into one JSON-serializable dict keyed by
    ``source``.

    Raises ``ValueError`` if two results share a ``source`` key, so one branch
    can never silently clobber another's result at the join node.
    """
    merged: dict[str, Any] = {}
    for result in results:
        if result.source in merged:
            raise ValueError(
                f"duplicate evidence source result for {result.source!r}"
            )
        merged[result.source] = result.to_dict()
    return merged


def build_join_payload(
    request: EvidenceRequest, results: Iterable[SourceResult]
) -> dict[str, Any]:
    """Build the full join-node payload: the shared request plus every
    branch's result, ready to hand to the final-answer node as plain JSON."""
    return {"request": request.to_dict(), "sources": merge_source_results(results)}
