"""Predictor orchestrator for the online precursor-to-replacement prediction
core (PMA-ONLINE-AGENT-plan.md §5.2-§5.6, §7).

``PrecursorPredictor`` composes a ``Repository`` (T08) with the pure
functions in ``policy.py`` (T04) to implement the full O2-O6 flow and emit a
structured prediction-log record per request. ``default_predictor()`` is the
serving-time factory: it returns ``None`` when the feature flag is off,
lazily imports the repository module (mirrors
``bq_analytics.queries.build_default_runner`` /
``pm_agent.workorders.service.default_history_provider``), and - unlike
those two - turns a repository/predictor construction failure (for example
``DefaultCredentialsError``) into a ``Predictor`` that always answers
``data_source_unavailable`` rather than raising or returning ``None``: a
disabled flag and a broken data source are different failure modes and must
not collapse onto the same "no predictor" signal downstream.

Import-time side effects are forbidden here (mirrors ``contracts.py``/
``settings.py``/``repository.py``): no ``google.auth``, no
``google.cloud.bigquery``, no ``google.cloud.logging`` and no
``pm_agent.prediction.repository``/``pm_agent.config`` at module scope.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pm_agent.prediction import policy
from pm_agent.prediction.contracts import (
    CurrentTac,
    DataSourceUnavailable,
    EmbeddingFailed,
    LastReplacement,
    LeadStats,
    Neighbour,
    NeighbourLeadSample,
    PrecursorEvidence,
    PredictionInput,
    PredictionReason,
    PredictionResult,
    Predictor,
    ProjectedWindow,
    Provenance,
    Recommendation,
    Repository,
)
from pm_agent.prediction.settings import PredictionSettings

__all__ = ["PrecursorPredictor", "default_predictor"]

# Matches Terraform's `telemetry_logs_filter` default (deployment/terraform/
# single-project/variables.tf): `labels.service_name="pma-agent"
# labels.type="agent_telemetry"`, plus the event label used to pick this
# record out from other agent telemetry (§5.6).
_LOG_LABELS: Mapping[str, str] = {
    "type": "agent_telemetry",
    "service_name": "pma-agent",
    "event": "pma_prediction",
}

_WO_NEIGHBOUR_EVIDENCE_LIMIT = 3

_stdlib_logger = logging.getLogger("pm_agent.prediction")

_PART_NUMBER_RE = re.compile(r"[^A-Z0-9]")


def _normalize_part_number(raw: str | None) -> str | None:
    """`re.sub(r"[^A-Z0-9]", "", x.upper())`, per §5.2/T08-T09 coordination.
    Blank input (or input that normalises to blank) is dropped, never bound
    as an empty-string "part number"."""
    if not raw:
        return None
    cleaned = _PART_NUMBER_RE.sub("", raw.upper())
    return cleaned or None


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_log_emitter(payload: Mapping[str, Any]) -> None:
    """Emit one structured prediction-log record (§5.6): real structured
    Cloud Logging when available, stdlib ``logging`` otherwise. Never raises
    - a logging failure must never break the prediction response, so the
    caller (``PrecursorPredictor._emit_log``) also wraps this in a
    try/except, but this function fails closed to the stdlib fallback on its
    own too."""
    labels = dict(payload.get("labels") or _LOG_LABELS)
    try:
        from google.cloud import logging as gcloud_logging

        client = gcloud_logging.Client()
        logger = client.logger("pma-agent")
        logger.log_struct(dict(payload), labels=labels)
    except Exception:
        _stdlib_logger.info(json.dumps(payload, default=str))


class PrecursorPredictor:
    """The `Predictor` (structurally satisfies
    ``pm_agent.prediction.contracts.Predictor``; no explicit subclassing).

    ``repository`` does all BigQuery I/O (T08); every decision here is made
    by the pure functions in ``policy.py`` (T04). ``log_fn``/``clock`` are
    swappable only for tests - production code always uses the real
    structured-logging emitter and wall-clock ``now()``.
    """

    def __init__(
        self,
        repository: Repository,
        settings: PredictionSettings,
        *,
        log_fn: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._log_fn = log_fn or _default_log_emitter
        self._clock = clock or _default_clock

    # -- Predictor Protocol ---------------------------------------------

    def predict(self, prediction_input: PredictionInput) -> PredictionResult:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        try:
            result = self._predict_inner(prediction_input)
        except DataSourceUnavailable as exc:
            result = PredictionResult.disabled(
                PredictionReason.DATA_SOURCE_UNAVAILABLE,
                workorder_id=prediction_input.wo_id,
                aircraft=prediction_input.aircraft_reg,
                analysis_as_of=prediction_input.analysis_as_of,
                mode=prediction_input.mode,
                detail=exc.detail,
            )
        except EmbeddingFailed as exc:
            # `embed_query` raises this both for a wrong-dimension embedding
            # (`detail` starts with "dim:") and for any other non-empty
            # AI.EMBED status - §6.3 splits these into two reasons.
            reason = (
                PredictionReason.EMBEDDING_INCOMPATIBLE
                if (exc.detail or "").startswith("dim:")
                else PredictionReason.EMBEDDING_FAILED
            )
            result = PredictionResult.disabled(
                reason,
                workorder_id=prediction_input.wo_id,
                aircraft=prediction_input.aircraft_reg,
                analysis_as_of=prediction_input.analysis_as_of,
                mode=prediction_input.mode,
                detail=exc.detail,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._emit_log(request_id, prediction_input, result, latency_ms)
        return result

    # -- O1-O6 orchestration ----------------------------------------------

    def _predict_inner(self, pi: PredictionInput) -> PredictionResult:
        normalized_part_numbers = tuple(
            pn for pn in (_normalize_part_number(p) for p in pi.part_numbers) if pn
        )
        normalized_header_pn = _normalize_part_number(pi.header_part_number)
        normalized_position = policy.normalize_position(pi.position)
        wo_text = (pi.pma_wo_text or "").strip()

        # O1 early reject (§5.1): nothing to gate on at all.
        if not wo_text and not normalized_part_numbers:
            return PredictionResult.disabled(
                PredictionReason.EMPTY_TEXT,
                workorder_id=pi.wo_id,
                aircraft=pi.aircraft_reg,
                analysis_as_of=pi.analysis_as_of,
                mode=pi.mode,
            )

        # Build-consistency check, fail closed (§5.6): every other curated
        # read below is only trustworthy if this passes.
        build_info = self._repository.curated_build_info()
        build_detail = policy.check_build(
            build_info, max_spread_s=self._settings.build_max_spread_s
        )
        if build_detail is not None:
            raise DataSourceUnavailable(
                "curated build is inconsistent", detail=build_detail
            )

        focus_components = self._repository.focus_components()
        exclude = list(pi.exclude_wo_uuids)

        # O2 scope gate, two-phase per policy.py's module docstring.
        stats_cache: dict[str, LeadStats] = {}
        embedding: list[float] | None = None
        anchor_neighbours_used: Sequence[Neighbour] = ()

        gate = policy.resolve_scope(
            part_numbers=normalized_part_numbers,
            position=normalized_position,
            header_part_number=normalized_header_pn,
            focus_components=focus_components,
        )
        if not gate.final:
            if gate.needs_stats_for:
                lead_sample_counts: dict[str, int] = {}
                for key in gate.needs_stats_for:
                    stats = self._repository.lead_time_stats(
                        key, pi.analysis_as_of, exclude
                    )
                    stats_cache[key] = stats
                    lead_sample_counts[key] = stats.n
                gate = policy.resolve_scope(
                    part_numbers=normalized_part_numbers,
                    position=normalized_position,
                    header_part_number=normalized_header_pn,
                    focus_components=focus_components,
                    lead_sample_counts=lead_sample_counts,
                )
            elif gate.needs_vote:
                # O3 (§5.3): embed the WO text and vote over anchor
                # neighbours. Reached only when wo_text is non-blank - the
                # empty_text early reject above already handled the only
                # case where both text and part numbers are empty.
                embedding = self._repository.embed_query(wo_text)
                anchor_neighbours_used = self._repository.anchor_neighbours(
                    embedding,
                    pi.analysis_as_of,
                    exclude,
                    self._settings.anchor_k,
                    self._settings.anchor_sim_threshold,
                )
                vote = policy.vote_component(
                    anchor_neighbours_used,
                    min_support=self._settings.min_vote_support,
                    min_share=self._settings.min_vote_share,
                    normalisation=self._settings.vote_normalisation,
                )
                gate = policy.resolve_scope(
                    part_numbers=normalized_part_numbers,
                    position=normalized_position,
                    header_part_number=normalized_header_pn,
                    focus_components=focus_components,
                    vote=vote,
                )
            else:  # pragma: no cover - resolve_scope always sets one of these
                raise AssertionError(
                    "resolve_scope returned a non-final GateResult with "
                    "neither needs_stats_for nor needs_vote"
                )

        # O4 lead-time stats for the winning component (§5.4), reusing a
        # stats lookup already made while resolving the gate when possible.
        stats: LeadStats | None = None
        if gate.passed and gate.component_key is not None:
            stats = stats_cache.get(gate.component_key)
            if stats is None:
                stats = self._repository.lead_time_stats(
                    gate.component_key, pi.analysis_as_of, exclude
                )
                stats_cache[gate.component_key] = stats

        # O5 decision + interval (§5.5).
        decision = policy.decide(
            gate,
            stats,
            min_sample=self._settings.min_sample,
            max_replacement_interval_share=self._settings.max_replacement_interval_share,
            show_supporting_interval=self._settings.show_supporting_interval,
        )

        # O6 evidence (§5.6): only gathered once a component is actually
        # matched - an unmatched/out-of-scope gate has nothing to show.
        precursor_evidence_rows: Sequence[PrecursorEvidence] = ()
        wo_neighbours_used: Sequence[Neighbour] = ()
        if gate.passed and gate.component_key is not None:
            precursor_evidence_rows = self._repository.precursor_evidence(
                gate.component_key,
                pi.analysis_as_of,
                exclude,
                [n.wo_uuid for n in anchor_neighbours_used],
                self._settings.evidence_limit,
            )
            if wo_text:
                if embedding is None:
                    embedding = self._repository.embed_query(wo_text)
                wo_neighbours_used = self._repository.wo_neighbours(
                    embedding,
                    pi.analysis_as_of,
                    exclude,
                    self._settings.wo_neighbour_k,
                    self._settings.wo_neighbour_sim_threshold,
                )
        evidence = policy.build_evidence(
            anchor_neighbours=anchor_neighbours_used,
            precursor_evidence=precursor_evidence_rows,
            wo_neighbours=wo_neighbours_used,
            anchor_limit=self._settings.evidence_limit,
            precursor_limit=self._settings.evidence_limit,
            wo_limit=_WO_NEIGHBOUR_EVIDENCE_LIMIT,
        )

        # current_tac (§5.1): context only, resolved regardless of the gate
        # outcome. Skips the latest-closing-WO lookup when aircraft_reg is
        # missing - the WO is still predicted, just without that source.
        current_tac = self._resolve_current_tac(pi)

        # Option B projected window (approved 2026-09-24, overrides plan OQ1
        # / BIGQUERY-AGENT-plan §8.5 "no absolute due TAC" only for this
        # clearly-labelled fleet-pattern window): only attempted once row 4
        # of `decide()` actually produced a labelled `supporting_interval`,
        # and only when there is a component/aircraft to anchor it to. A
        # failure of this one extra query must never fail the whole
        # prediction - it is caught and turned into a limitation instead.
        projected_window: ProjectedWindow | None = None
        extra_limitations: list[str] = []
        if (
            decision.supporting_interval is not None
            and self._settings.show_projected_window
            and gate.component_key is not None
            and pi.aircraft_reg
        ):
            exclude_uuid = pi.exclude_wo_uuids[0] if pi.exclude_wo_uuids else ""
            # A TAC observed after the cut-off (a user-supplied TAC in a past
            # `historical_replay`) must not drive `cycles_since`/`position`.
            window_tac = (
                current_tac
                if current_tac is None
                or current_tac.observed_at is None
                or current_tac.observed_at <= pi.analysis_as_of
                else None
            )
            try:
                last: LastReplacement | None = self._repository.last_replacement(
                    gate.component_key, pi.aircraft_reg, pi.analysis_as_of, exclude_uuid
                )
                if last is not None:
                    projected_window = policy.project_window(
                        decision.supporting_interval, last, window_tac
                    )
            except Exception as exc:  # this one query must never fail predict()
                detail = getattr(exc, "detail", None) or str(exc)
                _stdlib_logger.warning(
                    "projected_window lookup failed: %s: %s", type(exc).__name__, detail
                )
                projected_window = None
                extra_limitations.append("projected_window_unavailable")
            else:
                if last is None:
                    extra_limitations.append("no_prior_replacement_on_aircraft")
                elif projected_window is not None:
                    extra_limitations.append("projected_window_not_a_forecast")

        # Condensed recommendation (approved 2026-09-24, USER REQUEST: base the
        # recommendation on `wo_embeddings` similar-work-order matching -
        # description in the work order provided plus its action text - and
        # `fct_lead_time_samples`). Independent of the gate outcome: even an
        # out-of-scope/no-match WO can still get a `similar_workorders`
        # recommendation from its own text. Uses a wider, separate neighbour
        # search (`rec_neighbour_k`/`rec_neighbour_min_sim`) from O6's
        # evidence call. Never fails `predict()` - any failure here is caught
        # and turned into the `recommendation_unavailable` limitation.
        recommendation: Recommendation | None = None
        if self._settings.show_recommendation:
            try:
                if wo_text and embedding is None:
                    embedding = self._repository.embed_query(wo_text)
                rec_wo_neighbours: Sequence[Neighbour] = ()
                neighbour_lead_samples: Sequence[NeighbourLeadSample] = ()
                if embedding is not None:
                    rec_wo_neighbours = self._repository.wo_neighbours(
                        embedding,
                        pi.analysis_as_of,
                        exclude,
                        self._settings.rec_neighbour_k,
                        self._settings.rec_neighbour_min_sim,
                    )
                    if rec_wo_neighbours:
                        exclude_uuid = (
                            pi.exclude_wo_uuids[0] if pi.exclude_wo_uuids else ""
                        )
                        neighbour_lead_samples = (
                            self._repository.neighbour_lead_samples(
                                [n.wo_uuid for n in rec_wo_neighbours if n.wo_uuid],
                                pi.analysis_as_of,
                                exclude_uuid,
                            )
                        )
                # `last_replacement` anchors both the `component_history` and
                # `fleet_replacement_interval` TAC projections - fetched fresh
                # here (rather than reusing Option B's own fetch above, which
                # only runs for the `supporting_interval` row) so the
                # `component_history` basis also gets an anchor; the query
                # runner's cache makes a repeat of the same lookup cheap
                # (see repository.py's module docstring).
                rec_last_replacement: LastReplacement | None = None
                if gate.component_key is not None and pi.aircraft_reg:
                    exclude_uuid = pi.exclude_wo_uuids[0] if pi.exclude_wo_uuids else ""
                    rec_last_replacement = self._repository.last_replacement(
                        gate.component_key,
                        pi.aircraft_reg,
                        pi.analysis_as_of,
                        exclude_uuid,
                    )
                # Same leakage guard as Option B's `window_tac`: a TAC
                # observed after the cut-off never becomes the reference TAC.
                rec_tac = (
                    current_tac
                    if current_tac is None
                    or current_tac.observed_at is None
                    or current_tac.observed_at <= pi.analysis_as_of
                    else None
                )
                recommendation = policy.build_recommendation(
                    gate=gate,
                    decision=decision,
                    stats=stats,
                    last_replacement=rec_last_replacement,
                    current_tac=rec_tac,
                    neighbour_lead_samples=neighbour_lead_samples,
                    wo_neighbours=rec_wo_neighbours,
                    settings=self._settings,
                    focus_components=focus_components,
                )
            except Exception as exc:  # this feature must never fail predict()
                detail = getattr(exc, "detail", None) or str(exc)
                _stdlib_logger.warning(
                    "recommendation lookup failed: %s: %s", type(exc).__name__, detail
                )
                recommendation = None
                extra_limitations.append("recommendation_unavailable")
            else:
                if recommendation is not None:
                    extra_limitations.append("recommendation_heuristic_not_calibrated")

        limitations = list(
            policy.limitations(
                mode=pi.mode,
                # `BuildInfo` (frozen, T04) carries no embedding-endpoint label
                # field, and no task in this run adds one - always treated as
                # absent so `embedding_provenance_unverified` is always surfaced
                # rather than silently assumed true.
                embedding_endpoint_label_present=False,
                current_tac=current_tac,
                query_includes_action_text=pi.query_includes_action_text,
            )
        )
        limitations.extend(extra_limitations)

        provenance = Provenance(
            curated_dataset=self._settings.curated_dataset,
            curated_tables_created=build_info.table_creation_times,
            embedding_endpoint=self._settings.embedding_endpoint,
            settings_hash=self._settings.settings_hash,
            bq_job_ids=tuple(j.job_id for j in self._repository.job_log if j.job_id),
        )

        return PredictionResult(
            decision=decision.decision,
            reason=decision.reason,
            aircraft=pi.aircraft_reg,
            workorder_id=pi.wo_id,
            analysis_as_of=pi.analysis_as_of,
            mode=pi.mode,
            component_key=gate.component_key,
            part_number=gate.part_number,
            position=gate.position,
            candidates=gate.candidates,
            match=gate.match,
            interval=decision.interval,
            supporting_interval=decision.supporting_interval,
            projected_window=projected_window,
            recommendation=recommendation,
            evidence_support=decision.evidence_support,
            current_tac=current_tac,
            evidence=tuple(evidence),
            missing=decision.missing,
            limitations=tuple(limitations),
            provenance=provenance,
            reason_detail=gate.reason_detail,
        )

    def _resolve_current_tac(self, pi: PredictionInput) -> CurrentTac | None:
        latest_closing: CurrentTac | None = None
        latest_closing_max_tac_before_cutoff: int | None = None
        if pi.aircraft_reg:
            # §5.7: always exclude the uploaded WO's own uuid from its own
            # "latest closed work order" lookup.
            exclude_uuid = pi.exclude_wo_uuids[0] if pi.exclude_wo_uuids else ""
            latest_closing = self._repository.latest_closing_tac(
                pi.aircraft_reg, pi.analysis_as_of, exclude_uuid
            )
            # Protocol-external companion (see repository.py's module
            # docstring, "max_tac_before_cutoff side channel") - not part of
            # the frozen `Repository` Protocol, called the same way T08
            # documented it.
            latest_closing_max_tac_before_cutoff = (
                self._repository.latest_closing_max_tac_before_cutoff(
                    pi.aircraft_reg, pi.analysis_as_of, exclude_uuid
                )
            )
        return policy.resolve_current_tac(
            user_supplied_tac=pi.user_supplied_tac,
            user_supplied_observed_at=pi.user_supplied_tac_observed_at,
            now=self._clock(),
            issue_tac=pi.issue_tac,
            issue_date=pi.issue_date,
            analysis_as_of=pi.analysis_as_of,
            latest_closing=latest_closing,
            latest_closing_max_tac_before_cutoff=latest_closing_max_tac_before_cutoff,
            tac_stale_days=self._settings.tac_stale_days,
        )

    # -- prediction log (§5.6) -------------------------------------------

    def _emit_log(
        self,
        request_id: str,
        pi: PredictionInput,
        result: PredictionResult,
        latency_ms: int,
    ) -> None:
        """Build and emit the structured prediction-log record. Never lets a
        logging failure surface to the caller - the prediction response
        already went out (or is about to)."""
        try:
            pma_dict = result.to_dict()
            provenance = pma_dict.get("provenance") or {}
            try:
                job_log = self._repository.job_log
            except Exception:
                job_log = []
            payload: dict[str, Any] = {
                "request_id": request_id,
                "wo_id": pi.wo_id,
                # No raw WO text in the log (§5.6) - only its hash, so a
                # record can be correlated back to a specific WO body
                # without carrying maintenance-sensitive free text.
                "wo_text_sha256": (
                    hashlib.sha256(pi.pma_wo_text.encode("utf-8")).hexdigest()
                    if pi.pma_wo_text
                    else None
                ),
                "pma": pma_dict,
                "table_creation_times": provenance.get("curated_tables_created", {}),
                "embedding_endpoint": provenance.get(
                    "embedding_endpoint", self._settings.embedding_endpoint
                ),
                "settings_hash": provenance.get(
                    "settings_hash", self._settings.settings_hash
                ),
                "bq_job_ids": provenance.get("bq_job_ids")
                or [j.job_id for j in job_log if j.job_id],
                "total_bytes_billed": sum((j.total_bytes_billed or 0) for j in job_log),
                "latency_ms": latency_ms,
                "labels": dict(_LOG_LABELS),
            }
            self._log_fn(payload)
        except Exception:
            pass


class _ConstructionFailedPredictor:
    """Stand-in `Predictor` returned by `default_predictor()` when building
    the real repository/predictor fails (§7's T09 row). Deliberately
    distinct from returning `None`: `None` means "feature flag off", this
    means "the flag is on but the data source is unusable right now" -
    callers must be able to tell those apart."""

    def __init__(self, detail: str) -> None:
        self._detail = detail

    def predict(self, prediction_input: PredictionInput) -> PredictionResult:
        return PredictionResult.disabled(
            PredictionReason.DATA_SOURCE_UNAVAILABLE,
            workorder_id=prediction_input.wo_id,
            aircraft=prediction_input.aircraft_reg,
            analysis_as_of=prediction_input.analysis_as_of,
            mode=prediction_input.mode,
            detail=self._detail,
        )


def default_predictor() -> Predictor | None:
    """Serving-time factory (§7). Returns `None` when
    `PMA_PREDICTION_ENABLED` is unset/false; otherwise lazily imports
    `pm_agent.prediction.repository` (never at module scope here) and builds
    the live repository. A construction failure - bad credentials, an
    unreachable project, an invalid settings combination - never raises and
    never falls back to `None` (that would be indistinguishable from the
    flag being off): it returns a predictor that always answers
    `data_source_unavailable`, carrying only the exception type name as
    detail (never `str(exc)`, which can carry credential-adjacent text).
    """
    settings = PredictionSettings.from_env()
    if not settings.prediction_enabled:
        return None
    try:
        from pm_agent.prediction.repository import build_default_repository

        repository = build_default_repository(settings)
    except Exception as exc:
        return _ConstructionFailedPredictor(
            detail=f"construction_failed:{type(exc).__name__}"
        )
    return PrecursorPredictor(repository, settings)
