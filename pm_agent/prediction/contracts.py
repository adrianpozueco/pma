"""Typed data contracts for the online precursor-to-replacement prediction
core: inputs/outputs, reasons, exceptions and the ``Repository``/``Predictor``
seams (PMA-ONLINE-AGENT-plan.md §6, §7).

Import-time side effects are forbidden here: no ``google.auth``, no
``pm_agent.config``, no BigQuery client construction. Every dataclass is
frozen/slotted so a constructed value cannot drift between the point it is
built and the point it is serialized with :meth:`PredictionResult.to_dict`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

__all__ = [
    "BuildInfo",
    "ComponentCandidate",
    "ComponentMatch",
    "CurrentTac",
    "DataSourceUnavailable",
    "EmbeddingFailed",
    "EvidenceSupport",
    "FocusComponent",
    "IntervalStats",
    "JobInfo",
    "LastReplacement",
    "LeadStats",
    "Neighbour",
    "NeighbourLeadSample",
    "PrecursorEvidence",
    "PredictedReplacement",
    "PredictionDecision",
    "PredictionError",
    "PredictionInput",
    "PredictionReason",
    "PredictionResult",
    "Predictor",
    "ProjectedWindow",
    "Provenance",
    "Recommendation",
    "RecommendationConfidence",
    "Repository",
]

CONTRACT_VERSION = "pma-online-v1"


class PredictionReason(StrEnum):
    """The `reason` enum, §6.3. Values are the wire strings verbatim."""

    EMPTY_TEXT = "empty_text"
    EMBEDDING_FAILED = "embedding_failed"
    EMBEDDING_INCOMPATIBLE = "embedding_incompatible"
    NO_CONFIDENT_COMPONENT_MATCH = "no_confident_component_match"
    AMBIGUOUS_POSITION = "ambiguous_position"
    COMPONENT_NOT_IN_FOCUS_SET = "component_not_in_focus_set"
    POSITION_NOT_IN_FOCUS_SET = "position_not_in_focus_set"
    AIRCRAFT_TYPE_NOT_IN_SCOPE = "aircraft_type_not_in_scope"
    NO_LEAD_TIME_SAMPLES = "no_lead_time_samples"
    INSUFFICIENT_SAMPLES = "insufficient_samples"
    SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT = "samples_not_symptom_to_replacement"
    PREDICTION_DISABLED = "prediction_disabled"
    DATA_SOURCE_UNAVAILABLE = "data_source_unavailable"


class PredictionDecision(StrEnum):
    NO_RELIABLE_PREDICTION = "no_reliable_prediction"
    OUT_OF_SCOPE = "out_of_scope"
    HISTORICAL_INTERVAL = "historical_interval"


# §6.3 table: reason -> (decision, missing[]). `historical_interval` has no
# reason and is built directly by `policy.decide` row 5, not looked up here.
_DECISION_BY_REASON: dict[PredictionReason, PredictionDecision] = {
    PredictionReason.EMPTY_TEXT: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.EMBEDDING_FAILED: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.EMBEDDING_INCOMPATIBLE: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.NO_CONFIDENT_COMPONENT_MATCH: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.AMBIGUOUS_POSITION: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.COMPONENT_NOT_IN_FOCUS_SET: PredictionDecision.OUT_OF_SCOPE,
    PredictionReason.POSITION_NOT_IN_FOCUS_SET: PredictionDecision.OUT_OF_SCOPE,
    PredictionReason.AIRCRAFT_TYPE_NOT_IN_SCOPE: PredictionDecision.OUT_OF_SCOPE,
    PredictionReason.NO_LEAD_TIME_SAMPLES: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.INSUFFICIENT_SAMPLES: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.PREDICTION_DISABLED: PredictionDecision.NO_RELIABLE_PREDICTION,
    PredictionReason.DATA_SOURCE_UNAVAILABLE: PredictionDecision.NO_RELIABLE_PREDICTION,
}

_MISSING_BY_REASON: dict[PredictionReason, tuple[str, ...]] = {
    PredictionReason.EMPTY_TEXT: ("workorder_text",),
    PredictionReason.EMBEDDING_FAILED: ("query_embedding",),
    PredictionReason.EMBEDDING_INCOMPATIBLE: ("compatible_embedding_space",),
    PredictionReason.NO_CONFIDENT_COMPONENT_MATCH: ("component_match",),
    PredictionReason.AMBIGUOUS_POSITION: ("component_position",),
    PredictionReason.COMPONENT_NOT_IN_FOCUS_SET: (),
    PredictionReason.POSITION_NOT_IN_FOCUS_SET: (),
    PredictionReason.AIRCRAFT_TYPE_NOT_IN_SCOPE: (),
    PredictionReason.NO_LEAD_TIME_SAMPLES: ("lead_time_samples_for_component",),
    PredictionReason.INSUFFICIENT_SAMPLES: ("min_sample_size",),
    PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT: (
        "symptom_to_replacement_samples",
    ),
    PredictionReason.PREDICTION_DISABLED: ("feature_flag",),
    PredictionReason.DATA_SOURCE_UNAVAILABLE: ("bigquery_access",),
}


class PredictionError(Exception):
    """Base class for prediction-path errors raised by a `Repository`."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class DataSourceUnavailable(PredictionError):
    """BigQuery access failed, timed out, or the curated build is
    inconsistent (§5.6 "build consistency, fail closed")."""


class EmbeddingFailed(PredictionError):
    """`AI.EMBED` returned a non-empty status, the wrong dimension, or the
    endpoint does not match `settings.embedding_endpoint`."""


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PredictionInput:
    """Normalised O1 output (§5.1), independent of XML/parser details so the
    prediction core stays testable with plain values.

    `part_numbers` is every normalised part number the WO carries (header,
    required parts, part_keys, component-change on/off numbers);
    `header_part_number` is kept separately for the out-of-scope rule
    (§5.2 step 2).
    """

    wo_id: str
    mode: str
    analysis_as_of: datetime
    aircraft_reg: str | None
    ata_chapter: str | None
    position: str | None
    part_numbers: tuple[str, ...]
    header_part_number: str | None
    pma_wo_text: str
    exclude_wo_uuids: tuple[str, ...] = ()
    user_supplied_tac: int | None = None
    user_supplied_tac_observed_at: datetime | None = None
    issue_tac: int | None = None
    issue_date: datetime | None = None
    # Condensed recommendation (approved 2026-09-24): true when `pma_wo_text`
    # was built with `include_actions=True` in `historical_replay` mode (the
    # `query_include_actions` setting), so `policy.limitations` can flag that
    # the query text carries as-of-filtered action text a genuinely-new WO
    # would not yet have. Always False in `new_work_order` mode, which
    # already includes action text unconditionally.
    query_includes_action_text: bool = False


# --------------------------------------------------------------------------
# Repository-shaped value objects (§7 Repository Protocol return types)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FocusComponent:
    """One `pma_focus_components.sql` row."""

    component_key: str
    part_number: str
    position: str | None
    part_key: str
    part_aliases: tuple[str, ...]
    freq_rank: int
    replacement_count: int
    aircraft_with_replacement: int


@dataclass(frozen=True, slots=True)
class Neighbour:
    """One scored neighbour row, shared shape for anchor neighbours
    (`pma_anchor_neighbours.sql`) and WO neighbours (`pma_wo_neighbours.sql`).

    Anchor-only fields (`component_key`, `replacement_tac`, `replacement_date`)
    and WO-only fields (`ata_chapter`, `tac`, `closing_date`) are optional so
    one type serves both §5.3 templates without a union.
    """

    wo_uuid: str
    wo_id: str | None
    aircraft_reg: str | None
    sim: float
    snippet: str
    component_key: str | None = None
    replacement_tac: int | None = None
    replacement_date: date | None = None
    ata_chapter: str | None = None
    tac: int | None = None
    closing_date: date | None = None


@dataclass(frozen=True, slots=True)
class LeadStats:
    """`pma_lead_time_stats.sql`'s single row. `n=0` maps NULL aggregates
    (never the runner's `NO_MATCH` status - see §5.4)."""

    component_key: str
    n: int
    aircraft_n: int = 0
    replacement_n: int = 0
    lead_min: int | None = None
    lead_max: int | None = None
    lead_p50: float | None = None
    lead_p90: float | None = None
    lead_p95: float | None = None
    lead_mean: float | None = None
    lead_sd: float | None = None
    cv: float | None = None
    replacement_interval_share: float | None = None
    chained_sample_n: int = 0
    replacement_wo_uuids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NeighbourLeadSample:
    """One `pma_neighbour_lead_samples.sql` row (condensed recommendation,
    approved 2026-09-24, USER REQUEST: base the recommendation on
    `wo_embeddings` similar-work-order neighbours and `fct_lead_time_samples`).
    A `fct_lead_time_samples` row whose `precursor_wo_uuid` is one of the
    recommendation's own `wo_neighbours` (`rec_neighbour_k`/
    `rec_neighbour_min_sim`), grouped by `component_key` in
    `policy.aggregate_neighbour_lead_samples` to build the `similar_workorders`
    recommendation basis. `sim` is deliberately not a field here - it lives on
    the `Neighbour` the `precursor_wo_uuid` came from, joined back in by the
    aggregator, so this stays a thin mirror of the SQL row."""

    component_key: str
    precursor_wo_uuid: str
    aircraft_reg: str | None
    lead_cycles: int | None


@dataclass(frozen=True, slots=True)
class PrecursorEvidence:
    """One `pma_precursor_evidence.sql` row."""

    component_key: str
    aircraft_reg: str | None
    precursor_wo_id: str | None
    precursor_wo_uuid: str
    precursor_tac: int | None
    replacement_wo_uuid: str
    replacement_tac: int | None
    replacement_date: date | None
    lead_cycles: int | None
    sim: float | None
    reason: str | None
    precursor_snippet: str | None
    is_neighbour_hit: bool = False


@dataclass(frozen=True, slots=True)
class CurrentTac:
    """`current_tac` (§5.1, §6.2). Context only, never added to an interval."""

    value: int
    source: str
    observed_at: datetime | None
    stale: bool = False
    counter_inconsistent: bool = False


@dataclass(frozen=True, slots=True)
class LastReplacement:
    """`pma_last_replacement.sql`'s single row (Option B projected window,
    approved 2026-09-24 - overrides plan OQ1 / BIGQUERY-AGENT-plan §8.5 "no
    absolute due TAC" only for this clearly-labelled fleet-pattern window).
    `None` from `Repository.last_replacement` means "no eligible prior
    replacement of this component on this aircraft", not a failure."""

    tac: int
    replacement_date: date | None
    wo_id: str | None
    wo_uuid: str | None


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """`pma_curated_build_info.sql` rows, one per required curated table."""

    table_creation_times: Mapping[str, datetime]


@dataclass(frozen=True, slots=True)
class JobInfo:
    """One BigQuery job's id and billed bytes, collected for the prediction
    log (§5.6) and the 200 MB per-`predict()` acceptance check (§8.2 item 7)."""

    job_id: str | None
    total_bytes_billed: int | None


# --------------------------------------------------------------------------
# Output-shaped value objects (§6.2/§6.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComponentCandidate:
    """One `component.candidates[]` entry (§6.3 `ambiguous_position` /
    `no_lead_time_samples` fallback contracts): a focus key considered but
    not selected, with its sample count when known."""

    component_key: str
    n: int | None = None


@dataclass(frozen=True, slots=True)
class ComponentMatch:
    """`component.match` (§5.5): `gate_basis` always present, `vote` only
    when the gate basis is `anchor_vote`."""

    gate_basis: str
    vote_support: int | None = None
    vote_share: float | None = None
    vote_top_sim: float | None = None


@dataclass(frozen=True, slots=True)
class IntervalStats:
    """A labelled numeric interval (`interval` or `supporting_interval`,
    §5.5/§6.2). `calibrated` is always `False` (§8.5/OQ1: no confidence
    tiers). `label` is only set on `supporting_interval` (§6.5 wording)."""

    basis: str
    unit: str
    p50: float
    p90: float
    min: int
    max: int
    n: int
    calibrated: bool = False
    label: str | None = None
    aircraft: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectedWindow:
    """Option B projected replacement window (approved 2026-09-24): the
    labelled `supporting_interval` (consecutive-replacement fleet pattern)
    projected forward from this aircraft's own last replacement of the same
    component. Deliberately labelled `calibrated=False` - it is a fleet
    pattern, not a validated forecast, and does not change `decision`/
    `reason` (still `no_reliable_prediction` / `samples_not_symptom_to_replacement`).
    `cycles_since_last_replacement`/`position` are `None` when there is no
    usable current TAC, or the current TAC is `counter_inconsistent`.
    The p50/p90 come from the adjudicated `fct_lead_time_samples` for the
    component (mostly, not only, replacement-to-replacement intervals, and
    bounded by the precursor search window), not from every consecutive
    replacement gap in `fct_replacement_events`."""

    last_replacement_tac: int
    last_replacement_date: date | None
    last_replacement_wo_id: str | None
    tac_p50: int
    tac_p90: int
    cycles_since_last_replacement: int | None
    position: str | None
    basis: str = "last_replacement_plus_fleet_replacement_interval"
    unit: str = "aircraft_flight_cycles"
    calibrated: bool = False
    label: str = "Fleet pattern, not a forecast"


@dataclass(frozen=True, slots=True)
class PredictedReplacement:
    """`recommendation.predicted_replacement` (condensed recommendation,
    approved 2026-09-24): lead-cycle percentiles from the chosen basis'
    samples, plus those same percentiles projected onto an absolute TAC
    anchor (the WO's own `current_tac` for `similar_workorders`/
    `component_history`, the aircraft's own `last_replacement.tac` for
    `fleet_replacement_interval` - see `policy.build_recommendation`). Any
    field may be `None`: `lead_tac_p95`/`tac_p95` when the underlying sample
    is too small for `PERCENTILE_CONT(..., 0.95)` to have run, and every
    `tac_*` field when no TAC anchor was available to project onto."""

    lead_tac_p50: int | None
    lead_tac_p90: int | None
    lead_tac_p95: int | None
    tac_p50: int | None
    tac_p90: int | None
    tac_p95: int | None


@dataclass(frozen=True, slots=True)
class RecommendationConfidence:
    """`recommendation.confidence` (condensed recommendation, approved
    2026-09-24): a heuristic label, explicitly not a calibrated probability
    (§8.5/OQ1's "no confidence tiers" is overridden only for this
    clearly-labelled block, per the same 2026-09-24 approval as Option B)."""

    level: str  # "low" | "medium" | "high"
    similarity: float | None
    sample_size: int
    cv: float | None


@dataclass(frozen=True, slots=True)
class Recommendation:
    """`pma.recommendation` (condensed recommendation, approved 2026-09-24,
    USER REQUEST: base it on `wo_embeddings` neighbour matching and
    `fct_lead_time_samples`). Built only by `policy.build_recommendation`
    from already-fetched `Repository` data - no I/O here. Never changes
    `PredictionResult.decision`/`reason`: this is an additional, clearly
    heuristic block layered on top of whatever the base O2-O5 flow decided,
    exactly like Option B's `projected_window`. `to_dict()` (via
    `PredictionResult.to_dict`) produces the exact dict shape
    `pm_agent/workorders/chat.py`'s `_recommendation_block_lines` and
    `_recommendation_json_block` already render - see
    `tests/unit/test_workorder_analysis.py`'s `_recommendation()` fixture for
    the byte-for-byte contract."""

    component_key: str
    basis: str  # "similar_workorders" | "component_history" | "fleet_replacement_interval"
    predicted_replacement: PredictedReplacement
    confidence: RecommendationConfidence
    evidence: tuple[Mapping[str, Any], ...]
    action: str  # "recommend_inspection_or_part_planning" | "monitor"
    reference_tac: int | None = None
    reference_tac_source: str | None = None
    label: str = "Heuristic estimate, not a calibrated forecast"


@dataclass(frozen=True, slots=True)
class EvidenceSupport:
    """`evidence_support` (§5.5): always present once stats ran."""

    sample_size: int
    independent_aircraft: int | None = None
    distinct_replacement_events: int | None = None
    chained_sample_n: int | None = None
    cv: float | None = None
    replacement_interval_share: float | None = None


@dataclass(frozen=True, slots=True)
class Provenance:
    """`provenance` (§5.6/§6.2)."""

    curated_dataset: str
    curated_tables_created: Mapping[str, datetime | None]
    embedding_endpoint: str
    settings_hash: str
    bq_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """The full `pma` block (§6.2/§6.3). Built only by
    `prediction/service.py`; `analyze_xml` maps it into the legacy response
    shape (§6.1) but never mutates it."""

    decision: PredictionDecision
    reason: PredictionReason | None
    aircraft: str | None
    workorder_id: str
    analysis_as_of: datetime
    mode: str
    component_key: str | None = None
    part_number: str | None = None
    position: str | None = None
    candidates: tuple[ComponentCandidate, ...] = ()
    match: ComponentMatch | None = None
    interval: IntervalStats | None = None
    supporting_interval: IntervalStats | None = None
    projected_window: ProjectedWindow | None = None
    recommendation: Recommendation | None = None
    evidence_support: EvidenceSupport | None = None
    current_tac: CurrentTac | None = None
    evidence: tuple[Mapping[str, Any], ...] = ()
    missing: tuple[str, ...] = ()
    action: str | None = None
    limitations: tuple[str, ...] = ()
    provenance: Provenance | None = None
    reason_detail: Mapping[str, Any] | None = None
    contract_version: str = CONTRACT_VERSION

    @classmethod
    def disabled(
        cls,
        reason: PredictionReason,
        *,
        workorder_id: str = "",
        aircraft: str | None = None,
        analysis_as_of: datetime | None = None,
        mode: str = "",
        detail: str | None = None,
    ) -> PredictionResult:
        """Build the minimal fallback result for `prediction_disabled` /
        `data_source_unavailable` (feature flag off, or repository/predictor
        construction failed - §6.1)."""
        reason_detail = {"detail": detail} if detail else None
        return cls(
            decision=_DECISION_BY_REASON[reason],
            reason=reason,
            aircraft=aircraft,
            workorder_id=workorder_id,
            analysis_as_of=analysis_as_of or datetime.min,
            mode=mode,
            missing=_MISSING_BY_REASON[reason],
            reason_detail=reason_detail,
        )

    def to_dict(self) -> dict[str, Any]:
        """Recursively convert to JSON-primitive-only types (§5.6): the
        result is stored in ADK session state and must survive
        `json.dumps`/`json.loads` round trips without a custom encoder."""
        result = _jsonable(self)
        assert isinstance(result, dict)
        return result


def _jsonable(value: Any) -> Any:
    """Recursively reduce dataclasses/enums/dates/mappings/sequences to JSON
    primitives (str, int, float, bool, None, list, dict). Mirrors
    `workorders/evidence.py`'s `_to_jsonable` helper; duplicated rather than
    imported so `pm_agent.prediction` stays free of that module's import
    graph and side effects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset, Sequence)) and not isinstance(
        value, (str, bytes)
    ):
        return [_jsonable(item) for item in value]
    return value


# --------------------------------------------------------------------------
# Protocols (§7)
# --------------------------------------------------------------------------


class Repository(Protocol):
    """The BigQuery-backed data-access seam. Implemented (does I/O) by T08's
    `pm_agent/prediction/repository.py`; reproduced here verbatim from §7 so
    every later task can code against a frozen shape.

    Errors are raised as `DataSourceUnavailable` / `EmbeddingFailed`, never
    returned as sentinel values, except `latest_closing_tac`, whose `None`
    return means "no eligible prior WO", not a failure.
    """

    def curated_build_info(self) -> BuildInfo: ...

    def focus_components(self) -> list[FocusComponent]: ...

    def embed_query(self, wo_text: str) -> list[float]: ...

    def anchor_neighbours(
        self,
        emb: list[float],
        as_of: datetime,
        exclude: list[str],
        k: int,
        min_sim: float,
    ) -> list[Neighbour]: ...

    def wo_neighbours(
        self,
        emb: list[float],
        as_of: datetime,
        exclude: list[str],
        k: int,
        min_sim: float,
    ) -> list[Neighbour]: ...

    def lead_time_stats(
        self, component_key: str, as_of: datetime, exclude: list[str]
    ) -> LeadStats: ...

    def precursor_evidence(
        self,
        component_key: str,
        as_of: datetime,
        exclude: list[str],
        neighbour_uuids: list[str],
        limit: int,
    ) -> list[PrecursorEvidence]: ...

    def latest_closing_tac(
        self, reg: str, as_of: datetime, exclude_uuid: str
    ) -> CurrentTac | None: ...

    def last_replacement(
        self, component_key: str, reg: str, as_of: datetime, exclude_uuid: str
    ) -> LastReplacement | None: ...  # Option B projected window (2026-09-24)

    def neighbour_lead_samples(
        self, uuids: list[str], as_of: datetime, exclude_uuid: str
    ) -> list[NeighbourLeadSample]: ...  # condensed recommendation (2026-09-24)

    @property
    def job_log(self) -> list[JobInfo]: ...  # job_id, total_bytes_billed per call


class Predictor(Protocol):
    """The orchestrator seam. Implemented by T09's `PrecursorPredictor`;
    `default_predictor()` returns `None` when `PMA_PREDICTION_ENABLED=false`."""

    def predict(self, prediction_input: PredictionInput) -> PredictionResult: ...
