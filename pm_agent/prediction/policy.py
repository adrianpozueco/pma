"""Pure decision functions for the online precursor-to-replacement
prediction core (PMA-ONLINE-AGENT-plan.md §5.2-§5.6).

Every function here is a pure function over plain values and the frozen
dataclasses in :mod:`pm_agent.prediction.contracts`: no I/O, no BigQuery, no
clock reads (the caller always passes `analysis_as_of`/`now` explicitly).
This is what makes `tests/unit/test_prediction_policy.py` able to cover
every branch with fakes only.

**Two-phase scope gate.** The plan's Repository Protocol (§7) does I/O for
lead-time stats and for the embedding+anchor-kNN vote, and `policy.decide`
is written as `decide(scope, vote, stats)`. To keep `policy.py` pure while
still letting `resolve_scope` implement all of §5.2's rules, this module
uses a two-phase call convention instead:

1. `resolve_scope(...)` is called once with what the orchestrator (T09) has
   before any BigQuery call. If it can decide immediately (rules 1a/1b/2),
   it returns a *final* :class:`GateResult`. If it needs more data first, it
   returns a *non-final* result: `needs_stats_for` (rule 1c: run
   `lead_time_stats` for each listed component key) or `needs_vote=True`
   (rule 3: embed the WO text, fetch anchor neighbours, run
   `vote_component`).
2. The orchestrator does that I/O and calls `resolve_scope(...)` again,
   this time passing `lead_sample_counts` or `vote`, to get the final
   result.

`decide(gate, stats, ...)` then only needs `gate` (which already folds the
vote outcome, satisfying the plan's three-argument decision inputs) and
`stats`, and raises `ValueError` if `gate` is not final. This is a
deliberate, documented deviation from the plan's literal
`policy.decide(scope, vote, stats)` signature - see the T04 task report.

**`resolve_current_tac` and the Repository's `latest_closing_tac`.** The
frozen `Repository.latest_closing_tac(...) -> CurrentTac | None` Protocol
method returns an already-shaped `CurrentTac`. To keep `stale`/
`counter_inconsistent` computation pure and unit-testable here (§8.1: "
`current_tac` chain incl. `counter_inconsistent`") rather than buried in
BigQuery-calling code, `resolve_current_tac` treats the repository's return
value as a *raw candidate*: only `.value`/`.observed_at` are trusted, and
`stale`/`counter_inconsistent` are recomputed here from
`latest_closing_max_tac_before_cutoff` (the SQL row's own
`max_tac_before_cutoff` column, which the repository must also pass
through). T08's repository implementation should either call this same
function to build the `CurrentTac` it returns, or pass the raw scalar
straight through here - either way, this module owns the one true
implementation of the rule.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from pm_agent.prediction.contracts import (
    ComponentCandidate,
    ComponentMatch,
    CurrentTac,
    EvidenceSupport,
    FocusComponent,
    IntervalStats,
    LastReplacement,
    LeadStats,
    Neighbour,
    NeighbourLeadSample,
    PrecursorEvidence,
    PredictedReplacement,
    PredictionDecision,
    PredictionReason,
    ProjectedWindow,
    Recommendation,
    RecommendationConfidence,
)
from pm_agent.prediction.settings import POSITION_ALIASES, PredictionSettings

__all__ = [
    "REQUIRED_BUILD_TABLES",
    "ComponentLeadAgg",
    "Decision",
    "GateResult",
    "VoteResult",
    "aggregate_neighbour_lead_samples",
    "build_evidence",
    "build_recommendation",
    "check_build",
    "decide",
    "limitations",
    "normalize_position",
    "project_window",
    "resolve_current_tac",
    "resolve_scope",
    "vote_component",
]

# The 7 curated tables the build-consistency check (§5.6) requires.
# `fct_replacement_events` is the anchor: nothing else may be older than it,
# but (2026-09-24) it is excluded from the creation-time spread check itself
# - see `check_build`'s docstring - since it is rebuilt on its own schedule.
REQUIRED_BUILD_TABLES: tuple[str, ...] = (
    "dim_focus_components",
    "fct_replacement_events",
    "dim_reference_set",
    "replacement_anchor_embeddings",
    "wo_embeddings",
    "adjudicated_precursors",
    "fct_lead_time_samples",
)

_INTERVAL_UNIT = "aircraft_flight_cycles"
_HISTORICAL_BASIS = "closing_tac_to_closing_tac_interval"
_SUPPORTING_BASIS = "consecutive_replacement_closing_tac_interval"
_SUPPORTING_LABEL = (
    "Observed interval between consecutive replacements (closing TAC to "
    "closing TAC). Not a forecast."
)


def normalize_position(raw: str | None) -> str | None:
    """Normalise a narrative position string (§5.1): `UPPER(TRIM())`, then
    `settings.POSITION_ALIASES`. Values with no alias entry pass through
    unchanged (for example `RHD`, `LWR RH`, `FWD`), so they legitimately
    fail to match any focus position later. `None`/blank stays `None`."""
    if raw is None:
        return None
    cleaned = raw.strip().upper()
    if not cleaned:
        return None
    return POSITION_ALIASES.get(cleaned, cleaned)


# --------------------------------------------------------------------------
# O2: scope gate (§5.2)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of `resolve_scope`. `final=False` means the caller must do
    more I/O (see module docstring) and call again."""

    final: bool
    passed: bool = False
    decision: PredictionDecision | None = None
    reason: PredictionReason | None = None
    component_key: str | None = None
    part_number: str | None = None
    position: str | None = None
    gate_basis: str | None = None
    candidates: tuple[ComponentCandidate, ...] = ()
    match: ComponentMatch | None = None
    reason_detail: Mapping[str, Any] | None = None
    needs_stats_for: tuple[str, ...] = ()
    needs_vote: bool = False


def _alias_set(fc: FocusComponent) -> frozenset[str]:
    return frozenset({fc.part_key, *fc.part_aliases})


def _split_component_key(component_key: str) -> tuple[str, str | None]:
    part, _, position = component_key.partition("|")
    return part, (position or None)


def resolve_scope(
    *,
    part_numbers: Sequence[str],
    position: str | None,
    header_part_number: str | None,
    focus_components: Sequence[FocusComponent],
    lead_sample_counts: Mapping[str, int] | None = None,
    vote: VoteResult | None = None,
) -> GateResult:
    """Implements §5.2's gate, first match wins. `part_numbers`,
    `position` and `header_part_number` must already be normalised
    (`normalize_position`, `re.sub(r"[^A-Z0-9]", "", x.upper())`)."""
    part_number_set = frozenset(part_numbers)
    matches = [fc for fc in focus_components if _alias_set(fc) & part_number_set]

    if matches:
        return _resolve_exact_match(
            matches, position=position, lead_sample_counts=lead_sample_counts
        )

    if header_part_number:
        return GateResult(
            final=True,
            passed=False,
            decision=PredictionDecision.OUT_OF_SCOPE,
            reason=PredictionReason.COMPONENT_NOT_IN_FOCUS_SET,
            reason_detail={
                "part_numbers": list(part_numbers),
                "focus_components": [fc.component_key for fc in focus_components],
            },
        )

    # Rule 3: symptom-only WO, no focus part number anywhere - vote required.
    if vote is None:
        return GateResult(final=False, needs_vote=True)
    return _resolve_vote(
        vote, part_numbers=part_numbers, focus_components=focus_components
    )


def _resolve_exact_match(
    matches: Sequence[FocusComponent],
    *,
    position: str | None,
    lead_sample_counts: Mapping[str, int] | None,
) -> GateResult:
    if position is not None:
        with_position = [fc for fc in matches if fc.position == position]
        if len(with_position) == 1:
            fc = with_position[0]
            return GateResult(
                final=True,
                passed=True,
                decision=PredictionDecision.HISTORICAL_INTERVAL,
                component_key=fc.component_key,
                part_number=fc.part_number,
                position=fc.position,
                gate_basis="exact_pn_position",
                match=ComponentMatch(gate_basis="exact_pn_position"),
            )
        if not with_position:
            return GateResult(
                final=True,
                passed=False,
                decision=PredictionDecision.OUT_OF_SCOPE,
                reason=PredictionReason.POSITION_NOT_IN_FOCUS_SET,
            )
        # len > 1 is not reachable with well-formed focus data (one row per
        # component_key), but fail closed rather than guess a winner.
        return GateResult(
            final=True,
            passed=False,
            decision=PredictionDecision.NO_RELIABLE_PREDICTION,
            reason=PredictionReason.AMBIGUOUS_POSITION,
            candidates=tuple(
                ComponentCandidate(component_key=fc.component_key)
                for fc in with_position
            ),
        )

    # Position missing (rule 1c). The vote never picks between positions:
    # this always ends in no_lead_time_samples or ambiguous_position.
    if lead_sample_counts is None:
        return GateResult(
            final=False,
            needs_stats_for=tuple(fc.component_key for fc in matches),
            gate_basis="exact_pn",
        )
    candidates = tuple(
        ComponentCandidate(
            component_key=fc.component_key,
            n=lead_sample_counts.get(fc.component_key, 0),
        )
        for fc in matches
    )
    if all((c.n or 0) == 0 for c in candidates):
        return GateResult(
            final=True,
            passed=False,
            decision=PredictionDecision.NO_RELIABLE_PREDICTION,
            reason=PredictionReason.NO_LEAD_TIME_SAMPLES,
            gate_basis="exact_pn",
            candidates=candidates,
        )
    return GateResult(
        final=True,
        passed=False,
        decision=PredictionDecision.NO_RELIABLE_PREDICTION,
        reason=PredictionReason.AMBIGUOUS_POSITION,
        gate_basis="exact_pn",
        candidates=candidates,
    )


def _resolve_vote(
    vote: VoteResult,
    *,
    part_numbers: Sequence[str],
    focus_components: Sequence[FocusComponent],
) -> GateResult:
    if vote.passed and vote.winner_component_key is not None:
        winner_part, winner_position = _split_component_key(vote.winner_component_key)
        sibling_positions = {
            fc.position
            for fc in focus_components
            if fc.part_key == winner_part or fc.part_number == winner_part
        }
        if len(sibling_positions) > 1:
            return GateResult(
                final=True,
                passed=False,
                decision=PredictionDecision.NO_RELIABLE_PREDICTION,
                reason=PredictionReason.AMBIGUOUS_POSITION,
                candidates=tuple(
                    ComponentCandidate(component_key=fc.component_key)
                    for fc in focus_components
                    if fc.part_key == winner_part or fc.part_number == winner_part
                ),
            )
        return GateResult(
            final=True,
            passed=True,
            decision=PredictionDecision.HISTORICAL_INTERVAL,
            component_key=vote.winner_component_key,
            part_number=winner_part,
            position=winner_position,
            gate_basis="anchor_vote",
            match=ComponentMatch(
                gate_basis="anchor_vote",
                vote_support=vote.support,
                vote_share=vote.share,
                vote_top_sim=vote.top_sim,
            ),
        )
    if part_numbers:
        return GateResult(
            final=True,
            passed=False,
            decision=PredictionDecision.OUT_OF_SCOPE,
            reason=PredictionReason.COMPONENT_NOT_IN_FOCUS_SET,
        )
    return GateResult(
        final=True,
        passed=False,
        decision=PredictionDecision.NO_RELIABLE_PREDICTION,
        reason=PredictionReason.NO_CONFIDENT_COMPONENT_MATCH,
    )


# --------------------------------------------------------------------------
# O3 vote (§5.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoteResult:
    passed: bool
    winner_component_key: str | None = None
    support: int = 0
    share: float = 0.0
    top_sim: float = 0.0
    scores: Mapping[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.scores is None:
            object.__setattr__(self, "scores", {})


def vote_component(
    neighbours: Sequence[Neighbour],
    *,
    min_support: int,
    min_share: float,
    normalisation: str = "none",
) -> VoteResult:
    """§5.3's anchor-neighbour vote. `neighbours` must already be filtered
    to `sim >= anchor_sim_threshold` (the repository's `min_sim` param);
    this function only aggregates and thresholds."""
    support: dict[str, int] = {}
    raw_score: dict[str, float] = {}
    top_sim: dict[str, float] = {}
    for n in neighbours:
        if not n.component_key:
            continue
        support[n.component_key] = support.get(n.component_key, 0) + 1
        raw_score[n.component_key] = raw_score.get(n.component_key, 0.0) + n.sim
        top_sim[n.component_key] = max(top_sim.get(n.component_key, 0.0), n.sim)

    if not support:
        return VoteResult(passed=False)

    scores: dict[str, float] = {}
    for key, total in raw_score.items():
        if normalisation == "sqrt":
            scores[key] = total / math.sqrt(support[key])
        else:
            scores[key] = total
    total_score = sum(scores.values())
    shares = {
        key: (score / total_score if total_score > 0 else 0.0)
        for key, score in scores.items()
    }

    winner = max(shares, key=lambda key: shares[key])
    passed = support[winner] >= min_support and shares[winner] >= min_share
    return VoteResult(
        passed=passed,
        winner_component_key=winner if passed else None,
        support=support[winner],
        share=shares[winner],
        top_sim=top_sim[winner],
        scores=scores,
    )


# --------------------------------------------------------------------------
# O5: decision and interval (§5.5)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    decision: PredictionDecision
    reason: PredictionReason | None = None
    interval: IntervalStats | None = None
    supporting_interval: IntervalStats | None = None
    evidence_support: EvidenceSupport | None = None
    missing: tuple[str, ...] = ()


def _evidence_support(stats: LeadStats) -> EvidenceSupport:
    return EvidenceSupport(
        sample_size=stats.n,
        independent_aircraft=stats.aircraft_n,
        distinct_replacement_events=stats.replacement_n,
        chained_sample_n=stats.chained_sample_n,
        cv=stats.cv,
        replacement_interval_share=stats.replacement_interval_share,
    )


def decide(
    gate: GateResult,
    stats: LeadStats | None,
    *,
    min_sample: int,
    max_replacement_interval_share: float,
    show_supporting_interval: bool,
) -> Decision:
    """§5.5, first match wins. `gate` must be final (from `resolve_scope`);
    raises `ValueError` otherwise."""
    if not gate.final:
        raise ValueError("resolve_scope must return a final GateResult before decide()")

    # Row 1: gate did not pass.
    if not gate.passed:
        assert gate.decision is not None
        return Decision(
            decision=gate.decision, reason=gate.reason, missing=_missing_for(gate)
        )

    # Row 2: no samples at all.
    if stats is None or stats.n == 0:
        return Decision(
            decision=PredictionDecision.NO_RELIABLE_PREDICTION,
            reason=PredictionReason.NO_LEAD_TIME_SAMPLES,
        )

    # Row 3: below the display-rule minimum.
    if stats.n < min_sample:
        return Decision(
            decision=PredictionDecision.NO_RELIABLE_PREDICTION,
            reason=PredictionReason.INSUFFICIENT_SAMPLES,
            evidence_support=_evidence_support(stats),
        )

    # Row 4: samples are mostly replacement-to-replacement, not symptom-to-
    # replacement.
    share = stats.replacement_interval_share or 0.0
    if share > max_replacement_interval_share:
        supporting_interval = None
        if (
            show_supporting_interval
            and stats.lead_p50 is not None
            and stats.lead_p90 is not None
        ):
            supporting_interval = IntervalStats(
                basis=_SUPPORTING_BASIS,
                unit=_INTERVAL_UNIT,
                p50=stats.lead_p50,
                p90=stats.lead_p90,
                min=stats.lead_min or 0,
                max=stats.lead_max or 0,
                n=stats.n,
                calibrated=False,
                label=_SUPPORTING_LABEL,
                aircraft=stats.aircraft_n,
            )
        return Decision(
            decision=PredictionDecision.NO_RELIABLE_PREDICTION,
            reason=PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT,
            supporting_interval=supporting_interval,
            evidence_support=_evidence_support(stats),
        )

    # Row 5: historical interval.
    assert stats.lead_p50 is not None and stats.lead_p90 is not None
    interval = IntervalStats(
        basis=_HISTORICAL_BASIS,
        unit=_INTERVAL_UNIT,
        p50=stats.lead_p50,
        p90=stats.lead_p90,
        min=stats.lead_min or 0,
        max=stats.lead_max or 0,
        n=stats.n,
        calibrated=False,
        aircraft=stats.aircraft_n,
    )
    return Decision(
        decision=PredictionDecision.HISTORICAL_INTERVAL,
        reason=None,
        interval=interval,
        evidence_support=_evidence_support(stats),
    )


def round_cycles(value: float) -> int:
    """Round a cycle count half-up (380.5 -> 381), like BigQuery `ROUND`,
    not Python's banker's rounding. `PERCENTILE_CONT` yields `.5` for even
    n; `project_window` and the chat renderer share this helper so window
    TACs and rendered p50/p90 agree with each other and with SQL."""
    return math.floor(value + 0.5)


def project_window(
    supporting: IntervalStats,
    last: LastReplacement | None,
    current_tac: CurrentTac | None,
) -> ProjectedWindow | None:
    """Option B projected window (approved 2026-09-24): project the labelled
    `supporting_interval` forward from this aircraft's own `last_replacement`
    of the same component. Pure/I/O-free, mirrors `decide()`'s style. `None`
    when there is no prior replacement to anchor from; `cycles_since_last_
    replacement`/`position` are `None` when there is no usable current TAC."""
    if last is None:
        return None
    p50_r = round_cycles(supporting.p50)
    p90_r = round_cycles(supporting.p90)
    tac_p50 = last.tac + p50_r
    tac_p90 = last.tac + p90_r
    cycles_since: int | None = None
    if (
        current_tac is not None
        and not current_tac.counter_inconsistent
        and current_tac.value >= last.tac
    ):
        cycles_since = current_tac.value - last.tac
    position: str | None = None
    if cycles_since is not None:
        if cycles_since < p50_r:
            position = "before_p50"
        elif cycles_since <= p90_r:
            position = "between_p50_p90"
        else:
            position = "past_p90"
    return ProjectedWindow(
        last_replacement_tac=last.tac,
        last_replacement_date=last.replacement_date,
        last_replacement_wo_id=last.wo_id,
        tac_p50=tac_p50,
        tac_p90=tac_p90,
        cycles_since_last_replacement=cycles_since,
        position=position,
    )


# --------------------------------------------------------------------------
# Condensed recommendation (approved 2026-09-24, USER REQUEST: base the
# recommendation on `wo_embeddings` similar-work-order matching - description
# in the work order provided plus its action text - and `fct_lead_time_
# samples`). Pure/I/O-free, mirrors `project_window`'s style.
# --------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile, matching `numpy`'s default method
    and BigQuery's `PERCENTILE_CONT` (so a Python-side aggregate agrees with
    the SQL-side percentiles elsewhere in this module). `sorted_values` must
    already be sorted ascending and non-empty."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = p * (n - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_values[lo]
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


@dataclass(frozen=True, slots=True)
class ComponentLeadAgg:
    """One `component_key`'s aggregated `NeighbourLeadSample` rows, grouped
    by `aggregate_neighbour_lead_samples`."""

    component_key: str
    n: int
    aircraft_n: int
    mean_sim: float
    top_sim: float
    lead_p50: float
    lead_p90: float
    lead_p95: float
    cv: float | None
    wo_uuids: tuple[str, ...]


def aggregate_neighbour_lead_samples(
    samples: Sequence[NeighbourLeadSample],
    neighbour_sims: Mapping[str, float],
) -> dict[str, ComponentLeadAgg]:
    """Group `pma_neighbour_lead_samples.sql` rows by `component_key` to
    build the `similar_workorders` recommendation basis. `neighbour_sims`
    maps `precursor_wo_uuid -> sim` (from the `wo_neighbours` call that
    produced the `neighbour_wo_uuids` the SQL was filtered to); a sample
    whose neighbour is missing from the map contributes `sim=0.0` rather
    than being dropped, so lead-cycle coverage is never silently reduced by
    a bookkeeping gap. Rows with `lead_cycles is None` are skipped - they
    cannot contribute to a lead-time percentile."""
    grouped: dict[str, list[NeighbourLeadSample]] = {}
    for s in samples:
        if s.lead_cycles is None:
            continue
        grouped.setdefault(s.component_key, []).append(s)

    result: dict[str, ComponentLeadAgg] = {}
    for component_key, rows in grouped.items():
        leads = sorted(float(r.lead_cycles) for r in rows)  # type: ignore[arg-type]
        sims = [neighbour_sims.get(r.precursor_wo_uuid, 0.0) for r in rows]
        aircraft_n = len({r.aircraft_reg for r in rows if r.aircraft_reg})
        n = len(rows)
        cv: float | None = None
        if n > 1:
            mean = statistics.fmean(leads)
            if mean:
                cv = statistics.stdev(leads) / mean
        result[component_key] = ComponentLeadAgg(
            component_key=component_key,
            n=n,
            aircraft_n=aircraft_n,
            mean_sim=statistics.fmean(sims) if sims else 0.0,
            top_sim=max(sims) if sims else 0.0,
            lead_p50=_percentile(leads, 0.5),
            lead_p90=_percentile(leads, 0.9),
            lead_p95=_percentile(leads, 0.95),
            cv=cv,
            wo_uuids=tuple(r.precursor_wo_uuid for r in rows),
        )
    return result


def _confidence_level(
    *,
    basis: str,
    n: int,
    cv: float | None,
    similarity: float | None,
    settings: PredictionSettings,
) -> str:
    """`low`/`medium`/`high` heuristic for `Recommendation.confidence.level`,
    basis-dependent (condensed recommendation spec item 8 - each basis has a
    different evidentiary strength, so a fixed sample-size floor is not
    enough on its own):

      high   - `similar_workorders` only: `n >= rec_min_sample_high`, spread
               `cv <= rec_max_cv` (or unknown) and mean similarity
               `>= rec_strong_sim` (or unknown).
      medium - `similar_workorders` with `n >= min_vote_support` (the same
               floor the weighted vote itself gates on), or
               `component_history` with `n >= rec_min_sample_high` and
               `cv <= rec_max_cv` (or unknown).
      low    - everything else: `fleet_replacement_interval` always (it has
               no per-component sample of its own), and any low-sample case.
    """
    if basis == "similar_workorders":
        if n < settings.min_vote_support:
            return "low"
        if (
            n >= settings.rec_min_sample_high
            and (cv is None or cv <= settings.rec_max_cv)
            and (similarity is None or similarity >= settings.rec_strong_sim)
        ):
            return "high"
        return "medium"
    if basis == "component_history":
        if n >= settings.rec_min_sample_high and (
            cv is None or cv <= settings.rec_max_cv
        ):
            return "medium"
        return "low"
    return "low"


def _recommendation_action(
    level: str, *, reference_tac: int | None, tac_p50: int | None
) -> str:
    """`monitor` unless confidence is above `low`, or unless `reference_tac`
    (the aircraft's own current TAC, context only) has already reached or
    passed the predicted `tac_p50` - the "already due" override that forces
    `recommend_inspection_or_part_planning` even at `low` confidence
    (condensed recommendation spec item 10)."""
    if reference_tac is not None and tac_p50 is not None and reference_tac >= tac_p50:
        return "recommend_inspection_or_part_planning"
    return "monitor" if level == "low" else "recommend_inspection_or_part_planning"


def _vote_similar_workorders(
    aggs: Mapping[str, ComponentLeadAgg],
    *,
    focus_components: Sequence[FocusComponent],
    min_support: int,
    min_share: float,
) -> ComponentLeadAgg | None:
    """Weighted-vote winner among the `similar_workorders` aggregates: each
    `component_key`'s weight is `mean_sim * n` (its samples' total
    similarity mass), highest wins. Gated by `min_support` (`n >=
    min_vote_support`) and `min_share` (winner's share of the total weight
    `>= min_vote_share`), mirroring `vote_component`'s anchor-neighbour vote
    gate. Rejects the winner (`None`) when its part number has more than one
    sibling engine position (`#1`/`#2`) among `focus_components` - the same
    safety rule `_resolve_vote` applies - since an aggregate over lead-time
    samples alone cannot disambiguate positions."""
    if not aggs:
        return None
    weights = {key: agg.mean_sim * agg.n for key, agg in aggs.items()}
    total = sum(weights.values())
    if total <= 0:
        return None
    winner_key = max(weights, key=lambda key: weights[key])
    winner = aggs[winner_key]
    share = weights[winner_key] / total
    if winner.n < min_support or share < min_share:
        return None
    winner_part, _ = _split_component_key(winner.component_key)
    sibling_positions = {
        fc.position
        for fc in focus_components
        if fc.part_key == winner_part or fc.part_number == winner_part
    }
    if len(sibling_positions) > 1:
        return None
    return winner


def _rec_evidence(
    agg: ComponentLeadAgg, wo_neighbours: Sequence[Neighbour], *, limit: int = 3
) -> tuple[Mapping[str, Any], ...]:
    """Up to `limit` `{"wo_id": ..., "sim": ...}` entries for the winning
    component's neighbour set, sorted by similarity desc. Looks the
    `precursor_wo_uuid` up against `wo_neighbours` (the same neighbour list
    `neighbour_sims` was built from) to recover a human-readable `wo_id`,
    falling back to the raw uuid when a neighbour's `wo_id` is unset."""
    by_uuid = {n.wo_uuid: n for n in wo_neighbours if n.wo_uuid}
    rows = []
    # A precursor can pair with more than one replacement (several sample
    # rows) - list each neighbour WO once.
    for uuid in dict.fromkeys(agg.wo_uuids):
        n = by_uuid.get(uuid)
        sim = n.sim if n is not None else 0.0
        wo_id = (n.wo_id if n is not None else None) or uuid
        rows.append((sim, {"wo_id": wo_id, "sim": sim}))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(row for _, row in rows[:limit])


def _top_wo_neighbour_evidence(
    wo_neighbours: Sequence[Neighbour], *, limit: int = 3
) -> tuple[Mapping[str, Any], ...]:
    """Up to `limit` `{"wo_id": ..., "sim": ...}` entries from the top
    `wo_neighbours` by similarity - the evidence fallback (spec item 9) for
    the `component_history`/`fleet_replacement_interval` bases, which have
    no winning `ComponentLeadAgg` of their own to draw evidence from."""
    rows = sorted(
        (n for n in wo_neighbours if n.wo_uuid), key=lambda n: n.sim, reverse=True
    )
    return tuple({"wo_id": n.wo_id or n.wo_uuid, "sim": n.sim} for n in rows[:limit])


def build_recommendation(
    *,
    gate: GateResult,
    decision: Decision,
    stats: LeadStats | None,
    last_replacement: LastReplacement | None,
    current_tac: CurrentTac | None,
    neighbour_lead_samples: Sequence[NeighbourLeadSample],
    wo_neighbours: Sequence[Neighbour],
    settings: PredictionSettings,
    focus_components: Sequence[FocusComponent] = (),
) -> Recommendation | None:
    """Condensed "when should this part potentially be replaced" recommendation
    (approved 2026-09-24, USER REQUEST: base it on `wo_embeddings` similar-
    work-order matching - description plus action text - joined against
    `fct_lead_time_samples`). Purely additive: never changes `decision`/
    `reason`, and returns `None` (never raises) when nothing supports a
    recommendation.

    Basis priority (first usable one wins):
      a) `similar_workorders`, exact match - the gate's own `exact_pn_
         position` component_key has a `neighbour_lead_samples` aggregate of
         its own with at least `min_vote_support` samples.
      b) `similar_workorders`, weighted vote - `_vote_similar_workorders`
         across every aggregated `component_key`, gated by `min_vote_support`
         /`min_vote_share` and the sibling-position safety rule. Skipped when
         the gate made an exact PN+position match (that component then goes
         straight to (c)/(d), never a different voted part).
         (a)/(b) both additionally require a usable `current_tac` (not
         `None`, not `counter_inconsistent`) to project a `tac_pX`; without
         one they fall through to (c)/(d) rather than publish a
         `predicted_replacement` with no TAC fields at all.
      c) `component_history`  - gate matched a component whose own
         `lead_time_stats` has `n >= min_sample`; projected from the same
         reference TAC (falls through to (d) without one).
      d) `fleet_replacement_interval` - the `SAMPLES_NOT_SYMPTOM_TO_
         REPLACEMENT` row's `supporting_interval`, anchored on
         `last_replacement` (same inputs `project_window`/Option B uses).
      e) else -> `None`.

    `reference_tac`/`reference_tac_source` come from `current_tac` (latest
    known, even if stale; ignored when `counter_inconsistent`). (a)-(c):
    `tac_pX = reference_tac + lead_tac_pX`; (d): `tac_pX = last_replacement
    .tac + round(pX)` and `lead_tac_pX = tac_pX - reference_tac`.
    """
    usable_tac = (
        current_tac
        if current_tac is not None and not current_tac.counter_inconsistent
        else None
    )
    reference_tac = round_cycles(usable_tac.value) if usable_tac is not None else None
    reference_tac_source = usable_tac.source if usable_tac is not None else None

    neighbour_sims = {n.wo_uuid: n.sim for n in wo_neighbours if n.wo_uuid}
    aggs = aggregate_neighbour_lead_samples(neighbour_lead_samples, neighbour_sims)

    def _lead_projection(
        p50: float | None, p90: float | None, p95: float | None
    ) -> tuple[int | None, ...] | None:
        """(a)/(b)/(c): `lead_tac_pX = round(pX)`, `tac_pX = reference_tac +
        lead_tac_pX` (so the identity holds exactly in the rendered ints).
        `None` when there is no reference TAC or no p50/p90 to project."""
        if reference_tac is None or p50 is None or p90 is None:
            return None
        leads = [round_cycles(v) if v is not None else None for v in (p50, p90, p95)]
        tacs = [reference_tac + v if v is not None else None for v in leads]
        return (*leads, *tacs)

    basis: str | None = None
    reco_component_key: str | None = None
    projection: tuple[int | None, ...] | None = None
    n = 0
    similarity: float | None = None
    cv: float | None = None
    evidence: tuple[Mapping[str, Any], ...] = ()

    # (a)/(b) similar_workorders.
    # The vote (b) only runs when the gate did NOT deterministically match a
    # component: an exact PN+position match with no matched samples of its
    # own goes to (c)/(d) for that same component rather than recommending a
    # different part voted from the text (e.g. a CABIN PN on a WO whose text
    # reads like the AFT fire loop).
    exact_gate = (
        gate.match is not None
        and gate.match.gate_basis == "exact_pn_position"
        and gate.component_key is not None
    )
    winner_agg: ComponentLeadAgg | None = None
    if exact_gate:
        winner_agg = aggs.get(gate.component_key)  # type: ignore[arg-type]
        # One or two matched samples collapse p50/p90/p95 into a single
        # value; below `min_vote_support` the component's own history (c)
        # is the better basis.
        if winner_agg is not None and winner_agg.n < settings.min_vote_support:
            winner_agg = None
    elif aggs:
        winner_agg = _vote_similar_workorders(
            aggs,
            focus_components=focus_components,
            min_support=settings.min_vote_support,
            min_share=settings.min_vote_share,
        )

    if winner_agg is not None:
        projection = _lead_projection(
            winner_agg.lead_p50, winner_agg.lead_p90, winner_agg.lead_p95
        )
        if projection is not None:
            basis = "similar_workorders"
            reco_component_key = winner_agg.component_key
            n = winner_agg.n
            cv = winner_agg.cv
            similarity = winner_agg.mean_sim
            evidence = _rec_evidence(winner_agg, wo_neighbours)

    top_sim = max((nb.sim for nb in wo_neighbours), default=None)

    # (c) component_history: the gate-matched component's own lead-time
    # stats (`pma_lead_time_stats.sql`), n >= min_sample, projected from the
    # same reference TAC as (a)/(b).
    if (
        basis is None
        and gate.component_key is not None
        and stats is not None
        and stats.n >= settings.min_sample
    ):
        projection = _lead_projection(stats.lead_p50, stats.lead_p90, stats.lead_p95)
        if projection is not None:
            basis = "component_history"
            reco_component_key = gate.component_key
            n = stats.n
            cv = stats.cv
            similarity = top_sim
            evidence = _top_wo_neighbour_evidence(wo_neighbours)

    # (d) fleet_replacement_interval: Option B's `supporting_interval`
    # anchored on the aircraft's own last replacement:
    # `tac_pX = last_replacement.tac + round(pX)`, `lead_tac_pX = tac_pX -
    # reference_tac` (negative when already past; `None` without a TAC).
    if (
        basis is None
        and gate.component_key is not None
        and decision.supporting_interval is not None
        and last_replacement is not None
    ):
        supporting = decision.supporting_interval
        p95 = stats.lead_p95 if stats is not None else None
        tacs = [
            round_cycles(last_replacement.tac) + round_cycles(v)
            if v is not None
            else None
            for v in (supporting.p50, supporting.p90, p95)
        ]
        leads = [
            t - reference_tac if t is not None and reference_tac is not None else None
            for t in tacs
        ]
        projection = (*leads, *tacs)
        basis = "fleet_replacement_interval"
        reco_component_key = gate.component_key
        n = supporting.n
        cv = stats.cv if stats is not None else None
        similarity = top_sim
        evidence = _top_wo_neighbour_evidence(wo_neighbours)

    if basis is None or reco_component_key is None or projection is None:
        return None

    lead_p50, lead_p90, lead_p95, tac_p50, tac_p90, tac_p95 = projection
    level = _confidence_level(
        basis=basis, n=n, cv=cv, similarity=similarity, settings=settings
    )
    action = _recommendation_action(level, reference_tac=reference_tac, tac_p50=tac_p50)
    return Recommendation(
        component_key=reco_component_key,
        basis=basis,
        predicted_replacement=PredictedReplacement(
            lead_tac_p50=lead_p50,
            lead_tac_p90=lead_p90,
            lead_tac_p95=lead_p95,
            tac_p50=tac_p50,
            tac_p90=tac_p90,
            tac_p95=tac_p95,
        ),
        confidence=RecommendationConfidence(
            level=level, similarity=similarity, sample_size=n, cv=cv
        ),
        evidence=evidence,
        action=action,
        reference_tac=reference_tac,
        reference_tac_source=reference_tac_source,
    )


_GATE_MISSING: dict[PredictionReason, tuple[str, ...]] = {
    PredictionReason.NO_CONFIDENT_COMPONENT_MATCH: ("component_match",),
    PredictionReason.AMBIGUOUS_POSITION: ("component_position",),
    PredictionReason.NO_LEAD_TIME_SAMPLES: ("lead_time_samples_for_component",),
}


def _missing_for(gate: GateResult) -> tuple[str, ...]:
    if gate.reason is None:
        return ()
    return _GATE_MISSING.get(gate.reason, ())


# --------------------------------------------------------------------------
# current_tac (§5.1)
# --------------------------------------------------------------------------


def resolve_current_tac(
    *,
    user_supplied_tac: int | None,
    user_supplied_observed_at: datetime | None,
    now: datetime,
    issue_tac: int | None,
    issue_date: datetime | None,
    analysis_as_of: datetime,
    latest_closing: CurrentTac | None,
    latest_closing_max_tac_before_cutoff: int | None = None,
    tac_stale_days: int,
) -> CurrentTac | None:
    """§5.1's 3-step `current_tac` chain. Never used in an interval - context
    only. `latest_closing` is treated as a raw candidate: only `.value`/
    `.observed_at` are trusted (see module docstring); `stale`/
    `counter_inconsistent` are (re)computed here."""
    if user_supplied_tac is not None and user_supplied_observed_at is not None:
        age = now - user_supplied_observed_at
        if age.total_seconds() <= 24 * 3600:
            return CurrentTac(
                value=user_supplied_tac,
                source="user_supplied",
                observed_at=user_supplied_observed_at,
                stale=False,
                counter_inconsistent=False,
            )

    if issue_tac is not None:
        stale = (
            issue_date is not None
            and (analysis_as_of - issue_date).days > tac_stale_days
        )
        return CurrentTac(
            value=issue_tac,
            source="workorder_issue_tac",
            observed_at=issue_date,
            stale=stale,
            counter_inconsistent=False,
        )

    if latest_closing is not None:
        stale = (
            latest_closing.observed_at is not None
            and (analysis_as_of - latest_closing.observed_at).days > tac_stale_days
        )
        counter_inconsistent = (
            latest_closing_max_tac_before_cutoff is not None
            and latest_closing.value < latest_closing_max_tac_before_cutoff
        )
        return replace(
            latest_closing,
            source="latest_closing_tac",
            stale=stale,
            counter_inconsistent=counter_inconsistent,
        )

    return None


# --------------------------------------------------------------------------
# Build consistency (§5.6)
# --------------------------------------------------------------------------


def check_build(build_info: Any, *, max_spread_s: int) -> str | None:
    """Fail-closed curated-build consistency check. Returns `None` when
    consistent, else a short detail string for `DataSourceUnavailable`.

    `fct_replacement_events` is excluded from the spread computation on
    purpose (2026-09-24 data update): it is the slowest-changing curated
    table and is legitimately rebuilt on its own schedule, independent of
    the other 6 "downstream" tables that get recomputed from it. A
    downstream-only rebuild (the common case) can therefore leave
    `fct_replacement_events`'s creation time far outside the downstream
    tables' own tight spread without indicating a half-finished build.
    `max_spread_s` still applies to the 6 downstream tables among
    themselves - that catches the case this check exists for, a CTAS
    rebuild that dies partway through and leaves some downstream tables
    stale relative to their siblings. The `stale_downstream` check keeps
    comparing every downstream table's creation time against the
    `fct_replacement_events` anchor unchanged: nothing downstream may be
    older than the anchor it was built from, however old that anchor is."""
    creation_times: Mapping[str, datetime] = build_info.table_creation_times
    missing = [t for t in REQUIRED_BUILD_TABLES if t not in creation_times]
    if missing:
        return f"curated_build_inconsistent:missing_tables:{','.join(sorted(missing))}"

    times = {t: creation_times[t] for t in REQUIRED_BUILD_TABLES}
    anchor = times["fct_replacement_events"]
    downstream_times = {
        t: ts for t, ts in times.items() if t != "fct_replacement_events"
    }

    spread = (
        max(downstream_times.values()) - min(downstream_times.values())
    ).total_seconds()
    if spread > max_spread_s:
        return f"curated_build_inconsistent:spread_s:{int(spread)}"

    stale_downstream = [t for t, ts in downstream_times.items() if ts < anchor]
    if stale_downstream:
        return f"curated_build_inconsistent:stale_downstream:{','.join(sorted(stale_downstream))}"

    return None


# --------------------------------------------------------------------------
# Evidence list (§5.6)
# --------------------------------------------------------------------------


def _unique_title(title: str, seen: set[str]) -> str:
    if title not in seen:
        seen.add(title)
        return title
    n = 2
    while f"{title} ({n})" in seen:
        n += 1
    unique = f"{title} ({n})"
    seen.add(unique)
    return unique


def build_evidence(
    *,
    anchor_neighbours: Sequence[Neighbour] = (),
    precursor_evidence: Sequence[PrecursorEvidence] = (),
    wo_neighbours: Sequence[Neighbour] = (),
    anchor_limit: int = 5,
    precursor_limit: int = 5,
    wo_limit: int = 3,
) -> list[dict[str, Any]]:
    """§5.6's evidence list: top anchor neighbours, top lead/precursor
    samples (neighbour hits first - already the SQL's own ordering), top WO
    neighbours. Titles are de-duplicated so the frontend can key on
    `title`."""
    seen: set[str] = set()
    evidence: list[dict[str, Any]] = []

    for n in anchor_neighbours[:anchor_limit]:
        date_str = (
            n.replacement_date.isoformat() if n.replacement_date else "unknown-date"
        )
        title = _unique_title(
            f"Replacement WO {n.wo_id or n.wo_uuid} ({date_str})", seen
        )
        evidence.append(
            {
                "kind": "similar_past_replacement",
                "title": title,
                "wo_id": n.wo_id,
                "date": n.replacement_date,
                "component_key": n.component_key,
                "sim": n.sim,
                "detail": n.snippet,
            }
        )

    for p in precursor_evidence[:precursor_limit]:
        lead = p.lead_cycles if p.lead_cycles is not None else "?"
        title = _unique_title(
            f"Earlier WO {p.precursor_wo_id or p.precursor_wo_uuid} -> replacement "
            f"({lead} cycles later)",
            seen,
        )
        evidence.append(
            {
                "kind": "adjudicated_precursor",
                "title": title,
                "wo_id": p.precursor_wo_id,
                "sim": p.sim,
                "lead_cycles": p.lead_cycles,
                "reason": p.reason,
                "detail": p.precursor_snippet,
            }
        )

    for n in wo_neighbours[:wo_limit]:
        title = _unique_title(f"Similar past work order {n.wo_id or n.wo_uuid}", seen)
        evidence.append(
            {
                "kind": "similar_past_workorder",
                "title": title,
                "wo_id": n.wo_id,
                "date": n.closing_date,
                "ata_chapter": n.ata_chapter,
                "sim": n.sim,
                "detail": n.snippet,
            }
        )

    return evidence


# --------------------------------------------------------------------------
# Limitations (§5.5)
# --------------------------------------------------------------------------

_ALWAYS_ON_LIMITATIONS: tuple[str, ...] = (
    "not_calibrated",
    "outcome_selected_sample",
    "no_installation_eligibility_check",
    "closing_tac_basis",
)


def limitations(
    *,
    mode: str,
    embedding_endpoint_label_present: bool,
    current_tac: CurrentTac | None,
    query_includes_action_text: bool = False,
) -> list[str]:
    """§5.5's always-on plus conditional limitations list.

    `query_includes_action_text` (condensed recommendation, approved
    2026-09-24): true when `PredictionInput.query_includes_action_text` is
    set, i.e. a `historical_replay` request's query text was built with
    as-of-filtered action text (the `query_include_actions` setting) - a
    genuinely new WO would not yet have that text, so it is flagged
    separately from the always-on `in_sample_curated_artifacts` limitation.
    """
    result = list(_ALWAYS_ON_LIMITATIONS)
    if mode == "historical_replay":
        result.append("in_sample_curated_artifacts")
        if query_includes_action_text:
            result.append("query_includes_action_text")
    if not embedding_endpoint_label_present:
        result.append("embedding_provenance_unverified")
    if current_tac is not None:
        if current_tac.stale:
            result.append("current_tac_stale")
        if current_tac.counter_inconsistent:
            result.append("counter_inconsistent")
    return result
