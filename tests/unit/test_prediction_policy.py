"""Unit tests for `pm_agent.prediction.policy` (PMA-ONLINE-AGENT-plan.md §8.1).

All fakes, no BigQuery, no I/O: every scenario is built directly from the
dataclasses in `pm_agent.prediction.contracts`, matching the live examples
quoted in §5.2-§5.6 of the plan (component keys, thresholds, sample sizes).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from pm_agent.prediction.contracts import (
    BuildInfo,
    ComponentMatch,
    CurrentTac,
    FocusComponent,
    IntervalStats,
    LastReplacement,
    LeadStats,
    Neighbour,
    NeighbourLeadSample,
    PrecursorEvidence,
    PredictionDecision,
    PredictionReason,
)
from pm_agent.prediction.policy import (
    Decision,
    GateResult,
    VoteResult,
    aggregate_neighbour_lead_samples,
    build_evidence,
    build_recommendation,
    check_build,
    decide,
    limitations,
    normalize_position,
    project_window,
    resolve_current_tac,
    resolve_scope,
    round_cycles,
    vote_component,
)
from pm_agent.prediction.settings import PredictionSettings

UTC = UTC


def _focus(
    part_number: str,
    position: str | None,
    *,
    aliases: tuple[str, ...] = (),
    part_key: str | None = None,
    freq_rank: int = 1,
) -> FocusComponent:
    key = part_key or "".__class__(part_number)
    component_key = f"{part_number}|{position}" if position else part_number
    return FocusComponent(
        component_key=component_key,
        part_number=part_number,
        position=position,
        part_key=key,
        part_aliases=aliases,
        freq_rank=freq_rank,
        replacement_count=10,
        aircraft_with_replacement=5,
    )


AFT_DETECTOR = _focus("473597-5", "AFT", part_key="4735975", aliases=("4735975",))
RH_LIGHT = _focus("45-0351-4", "RH", part_key="4503514", aliases=("4503514",))
FAN_1 = _focus("2085M31G03", "#1", part_key="2085M31G03", aliases=("2085M31G03",))
FAN_2 = _focus("2085M31G03", "#2", part_key="2085M31G03", aliases=("2085M31G03",))
BLADE_1 = _focus(
    "340-001-038-0",
    "#1",
    part_key="340-001-038-0",
    aliases=(
        "3400010380",
        "3400010360",
        "3400010280",
        "3400010260",
        "3400010390",
    ),
)
FOCUS_SET = (AFT_DETECTOR, RH_LIGHT, FAN_1, FAN_2, BLADE_1)


# --------------------------------------------------------------------------
# normalize_position / position alias map
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("AFTCARGO", "AFT"),
        ("aft cargo", "AFT"),
        ("  AFT CARGO  ", "AFT"),
        ("ENG1", "#1"),
        ("eng 1", "#1"),
        ("1", "#1"),
        ("ENG2", "#2"),
        ("eng 2", "#2"),
        ("2", "#2"),
        ("RHD", "RHD"),
        ("LWR RH", "LWR RH"),
        ("FWD", "FWD"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_normalize_position(raw: str | None, expected: str | None) -> None:
    assert normalize_position(raw) == expected


# --------------------------------------------------------------------------
# resolve_scope: rule 1a (exact part + position)
# --------------------------------------------------------------------------


def test_scope_rule_1a_exact_pn_position_passes() -> None:
    gate = resolve_scope(
        part_numbers=["4735975"],
        position="AFT",
        header_part_number="4735975",
        focus_components=FOCUS_SET,
    )
    assert gate.final and gate.passed
    assert gate.decision == PredictionDecision.HISTORICAL_INTERVAL
    assert gate.component_key == "473597-5|AFT"
    assert gate.gate_basis == "exact_pn_position"
    assert gate.match is not None and gate.match.gate_basis == "exact_pn_position"


def test_scope_rule_1a_superseded_alias_resolves_to_canonical_key() -> None:
    """`3400010360` is a superseded PN that must still resolve to the
    canonical `340-001-038-0|#1` component (§5.2, §8.1)."""
    gate = resolve_scope(
        part_numbers=["3400010360"],
        position="#1",
        header_part_number="3400010360",
        focus_components=FOCUS_SET,
    )
    assert gate.final and gate.passed
    assert gate.component_key == "340-001-038-0|#1"
    assert gate.gate_basis == "exact_pn_position"


# --------------------------------------------------------------------------
# resolve_scope: rule 1b (position present, no matching focus position)
# --------------------------------------------------------------------------


def test_scope_rule_1b_fwd_detector_out_of_scope() -> None:
    """A FWD `473597-5` detector: exact part match, but no focus key has an
    FWD position (only AFT does)."""
    gate = resolve_scope(
        part_numbers=["4735975"],
        position="FWD",
        header_part_number="4735975",
        focus_components=FOCUS_SET,
    )
    assert gate.final and not gate.passed
    assert gate.decision == PredictionDecision.OUT_OF_SCOPE
    assert gate.reason == PredictionReason.POSITION_NOT_IN_FOCUS_SET


# --------------------------------------------------------------------------
# resolve_scope: rule 1c (position missing -> lead-time stats needed)
# --------------------------------------------------------------------------


def test_scope_rule_1c_first_pass_requests_stats() -> None:
    gate = resolve_scope(
        part_numbers=["2085M31G03"],
        position=None,
        header_part_number="2085M31G03",
        focus_components=FOCUS_SET,
    )
    assert not gate.final
    assert set(gate.needs_stats_for) == {"2085M31G03|#1", "2085M31G03|#2"}
    assert not gate.needs_vote


def test_scope_rule_1c_both_zero_samples_no_lead_time_samples() -> None:
    gate = resolve_scope(
        part_numbers=["2085M31G03"],
        position=None,
        header_part_number="2085M31G03",
        focus_components=FOCUS_SET,
        lead_sample_counts={"2085M31G03|#1": 0, "2085M31G03|#2": 0},
    )
    assert gate.final and not gate.passed
    assert gate.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert gate.reason == PredictionReason.NO_LEAD_TIME_SAMPLES
    assert {c.component_key for c in gate.candidates} == {
        "2085M31G03|#1",
        "2085M31G03|#2",
    }
    assert all((c.n or 0) == 0 for c in gate.candidates)


def test_scope_rule_1c_one_candidate_with_samples_is_still_ambiguous() -> None:
    """The vote never picks between positions: even one non-zero candidate
    yields `ambiguous_position`, never a pass."""
    gate = resolve_scope(
        part_numbers=["2085M31G03"],
        position=None,
        header_part_number="2085M31G03",
        focus_components=FOCUS_SET,
        lead_sample_counts={"2085M31G03|#1": 0, "2085M31G03|#2": 7},
    )
    assert gate.final and not gate.passed
    assert gate.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert gate.reason == PredictionReason.AMBIGUOUS_POSITION
    ns = {c.component_key: c.n for c in gate.candidates}
    assert ns == {"2085M31G03|#1": 0, "2085M31G03|#2": 7}


# --------------------------------------------------------------------------
# resolve_scope: rule 2 (header part number not in focus)
# --------------------------------------------------------------------------


def test_scope_rule_2_header_part_not_in_focus_out_of_scope() -> None:
    gate = resolve_scope(
        part_numbers=["62197301001"],
        position=None,
        header_part_number="62197301001",
        focus_components=FOCUS_SET,
    )
    assert gate.final and not gate.passed
    assert gate.decision == PredictionDecision.OUT_OF_SCOPE
    assert gate.reason == PredictionReason.COMPONENT_NOT_IN_FOCUS_SET
    assert gate.reason_detail is not None
    assert gate.reason_detail["part_numbers"] == ["62197301001"]
    assert "473597-5|AFT" in gate.reason_detail["focus_components"]


# --------------------------------------------------------------------------
# resolve_scope: rule 3 (symptom-only WO -> vote)
# --------------------------------------------------------------------------


def test_scope_rule_3_first_pass_requests_vote() -> None:
    gate = resolve_scope(
        part_numbers=[],
        position=None,
        header_part_number=None,
        focus_components=FOCUS_SET,
    )
    assert not gate.final
    assert gate.needs_vote
    assert not gate.needs_stats_for


def test_scope_rule_3_vote_winner_single_position_passes() -> None:
    vote = VoteResult(
        passed=True,
        winner_component_key="473597-5|AFT",
        support=18,
        share=1.0,
        top_sim=0.91,
    )
    gate = resolve_scope(
        part_numbers=[],
        position=None,
        header_part_number=None,
        focus_components=FOCUS_SET,
        vote=vote,
    )
    assert gate.final and gate.passed
    assert gate.decision == PredictionDecision.HISTORICAL_INTERVAL
    assert gate.component_key == "473597-5|AFT"
    assert gate.gate_basis == "anchor_vote"
    assert gate.match is not None
    assert gate.match.vote_support == 18
    assert gate.match.vote_share == 1.0
    assert gate.match.vote_top_sim == 0.91


def test_scope_rule_3_vote_winner_twin_positions_ambiguous() -> None:
    """A vote winner whose part has several focus positions (engine keys)
    must never be accepted (§5.2 rule 3)."""
    vote = VoteResult(
        passed=True,
        winner_component_key="2085M31G03|#1",
        support=10,
        share=0.8,
        top_sim=0.9,
    )
    gate = resolve_scope(
        part_numbers=[],
        position=None,
        header_part_number=None,
        focus_components=FOCUS_SET,
        vote=vote,
    )
    assert gate.final and not gate.passed
    assert gate.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert gate.reason == PredictionReason.AMBIGUOUS_POSITION


def test_scope_rule_3_vote_fails_with_non_focus_parts_named_out_of_scope() -> None:
    vote = VoteResult(passed=False)
    gate = resolve_scope(
        part_numbers=["62197301001"],
        position=None,
        header_part_number=None,
        focus_components=FOCUS_SET,
        vote=vote,
    )
    assert gate.final and not gate.passed
    assert gate.decision == PredictionDecision.OUT_OF_SCOPE
    assert gate.reason == PredictionReason.COMPONENT_NOT_IN_FOCUS_SET


def test_scope_rule_3_vote_fails_no_parts_named_no_confident_match() -> None:
    vote = VoteResult(passed=False)
    gate = resolve_scope(
        part_numbers=[],
        position=None,
        header_part_number=None,
        focus_components=FOCUS_SET,
        vote=vote,
    )
    assert gate.final and not gate.passed
    assert gate.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert gate.reason == PredictionReason.NO_CONFIDENT_COMPONENT_MATCH


# --------------------------------------------------------------------------
# vote_component
# --------------------------------------------------------------------------


def _neighbour(component_key: str, sim: float) -> Neighbour:
    return Neighbour(
        wo_uuid=f"uuid-{component_key}-{sim}",
        wo_id="1",
        aircraft_reg="9H-REG00163",
        sim=sim,
        snippet="",
        component_key=component_key,
    )


def test_vote_component_passes_above_thresholds() -> None:
    neighbours = [
        _neighbour("473597-5|AFT", 0.95),
        _neighbour("473597-5|AFT", 0.9),
        _neighbour("473597-5|AFT", 0.88),
    ]
    result = vote_component(neighbours, min_support=3, min_share=0.5)
    assert result.passed
    assert result.winner_component_key == "473597-5|AFT"
    assert result.support == 3
    assert result.share == pytest.approx(1.0)
    assert result.top_sim == pytest.approx(0.95)


def test_vote_component_fails_below_min_support() -> None:
    neighbours = [_neighbour("473597-5|AFT", 0.95), _neighbour("473597-5|AFT", 0.9)]
    result = vote_component(neighbours, min_support=3, min_share=0.5)
    assert not result.passed


def test_vote_component_fails_below_min_share() -> None:
    neighbours = [
        *[_neighbour("473597-5|AFT", 0.9) for _ in range(3)],
        *[_neighbour("45-0351-4|RH", 0.89) for _ in range(3)],
    ]
    result = vote_component(neighbours, min_support=3, min_share=0.6)
    assert not result.passed


def test_vote_component_no_neighbours_fails() -> None:
    result = vote_component([], min_support=3, min_share=0.5)
    assert not result.passed
    assert result.winner_component_key is None


def test_vote_component_sqrt_normalisation_changes_winner() -> None:
    """A key with many mediocre-similarity neighbours can out-score a key
    with fewer, higher-similarity neighbours under raw-sum scoring; `sqrt`
    normalisation divides by `sqrt(support)` to dampen that."""
    many_mediocre = [_neighbour("473597-5|AFT", 0.81) for _ in range(4)]
    few_strong = [_neighbour("45-0351-4|RH", 0.95) for _ in range(3)]
    neighbours = many_mediocre + few_strong

    raw = vote_component(neighbours, min_support=1, min_share=0.0, normalisation="none")
    sqrt = vote_component(
        neighbours, min_support=1, min_share=0.0, normalisation="sqrt"
    )

    assert raw.winner_component_key == "473597-5|AFT"  # raw sum 3.24 beats 2.85
    assert (
        sqrt.winner_component_key == "45-0351-4|RH"
    )  # 0.95*sqrt(3)=1.645 beats 0.81*sqrt(4)=1.62


# --------------------------------------------------------------------------
# decide (§5.5 rows 1-5)
# --------------------------------------------------------------------------


def _passed_gate(component_key: str = "473597-5|AFT") -> GateResult:
    return GateResult(
        final=True,
        passed=True,
        decision=PredictionDecision.HISTORICAL_INTERVAL,
        component_key=component_key,
        gate_basis="exact_pn_position",
    )


def _failed_gate(reason: PredictionReason, decision: PredictionDecision) -> GateResult:
    return GateResult(final=True, passed=False, decision=decision, reason=reason)


def test_decide_row1_gate_not_passed_echoes_reason() -> None:
    gate = _failed_gate(
        PredictionReason.POSITION_NOT_IN_FOCUS_SET, PredictionDecision.OUT_OF_SCOPE
    )
    decision = decide(
        gate,
        None,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=False,
    )
    assert decision.decision == PredictionDecision.OUT_OF_SCOPE
    assert decision.reason == PredictionReason.POSITION_NOT_IN_FOCUS_SET
    assert decision.missing == ()


def test_decide_row1_missing_matches_reason_table() -> None:
    gate = _failed_gate(
        PredictionReason.AMBIGUOUS_POSITION, PredictionDecision.NO_RELIABLE_PREDICTION
    )
    decision = decide(
        gate,
        None,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=False,
    )
    assert decision.missing == ("component_position",)


def test_decide_requires_final_gate() -> None:
    with pytest.raises(ValueError):
        decide(
            GateResult(final=False, needs_vote=True),
            None,
            min_sample=5,
            max_replacement_interval_share=0.5,
            show_supporting_interval=False,
        )


def test_decide_row2_zero_samples() -> None:
    stats = LeadStats(component_key="2085M31G03|#1", n=0)
    decision = decide(
        _passed_gate(),
        stats,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=False,
    )
    assert decision.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert decision.reason == PredictionReason.NO_LEAD_TIME_SAMPLES


def test_decide_row2_none_stats_treated_as_zero() -> None:
    decision = decide(
        _passed_gate(),
        None,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=False,
    )
    assert decision.reason == PredictionReason.NO_LEAD_TIME_SAMPLES


def test_decide_row3_insufficient_samples() -> None:
    """RH: n=1, below `PMA_MIN_SAMPLE=5`."""
    stats = LeadStats(component_key="45-0351-4|RH", n=1, aircraft_n=1, replacement_n=1)
    decision = decide(
        _passed_gate("45-0351-4|RH"),
        stats,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=False,
    )
    assert decision.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert decision.reason == PredictionReason.INSUFFICIENT_SAMPLES
    assert (
        decision.evidence_support is not None
        and decision.evidence_support.sample_size == 1
    )


def test_decide_row4_samples_not_symptom_to_replacement() -> None:
    """AFT: n=27, replacement_interval_share=0.9259 > 0.5."""
    stats = LeadStats(
        component_key="473597-5|AFT",
        n=27,
        aircraft_n=24,
        replacement_n=27,
        lead_min=4,
        lead_max=3158,
        lead_p50=356.0,
        lead_p90=1968.2,
        replacement_interval_share=0.9259,
        chained_sample_n=2,
    )
    decision = decide(
        _passed_gate(),
        stats,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=False,
    )
    assert decision.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert decision.reason == PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT
    assert decision.interval is None
    assert decision.supporting_interval is None


def test_decide_row4_supporting_interval_shown_when_enabled() -> None:
    stats = LeadStats(
        component_key="473597-5|AFT",
        n=27,
        aircraft_n=24,
        replacement_n=27,
        lead_min=4,
        lead_max=3158,
        lead_p50=356.0,
        lead_p90=1968.2,
        replacement_interval_share=0.9259,
        chained_sample_n=2,
    )
    decision = decide(
        _passed_gate(),
        stats,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=True,
    )
    assert decision.supporting_interval is not None
    si = decision.supporting_interval
    assert si.basis == "consecutive_replacement_closing_tac_interval"
    assert si.calibrated is False
    assert (
        si.p50 == 356.0
        and si.p90 == 1968.2
        and si.min == 4
        and si.max == 3158
        and si.n == 27
    )
    assert si.label is not None and "Not a forecast" in si.label
    # never mapped into `timing`/`interval`.
    assert decision.interval is None


def test_decide_row5_historical_interval() -> None:
    stats = LeadStats(
        component_key="X|Y",
        n=10,
        aircraft_n=8,
        replacement_n=10,
        lead_min=1,
        lead_max=100,
        lead_p50=20.0,
        lead_p90=80.0,
        replacement_interval_share=0.2,
    )
    decision = decide(
        _passed_gate("X|Y"),
        stats,
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_supporting_interval=False,
    )
    assert decision.decision == PredictionDecision.HISTORICAL_INTERVAL
    assert decision.reason is None
    assert decision.interval is not None
    assert decision.interval.basis == "closing_tac_to_closing_tac_interval"
    assert decision.interval.calibrated is False
    assert decision.interval.p50 == 20.0 and decision.interval.p90 == 80.0


# --------------------------------------------------------------------------
# project_window (Option B projected window, approved 2026-09-24)
# --------------------------------------------------------------------------

_AFT_SUPPORTING = IntervalStats(
    basis="consecutive_replacement_closing_tac_interval",
    unit="aircraft_flight_cycles",
    p50=356.0,
    p90=1968.2,
    min=4,
    max=3158,
    n=27,
    calibrated=False,
    label="Observed interval between consecutive replacements. Not a forecast.",
    aircraft=24,
)

_AFT_LAST = LastReplacement(
    tac=17938,
    replacement_date=date(2026, 4, 25),
    wo_id="WO-OLD-1",
    wo_uuid="wo-old-uuid-1",
)


def test_project_window_none_when_no_last_replacement() -> None:
    assert project_window(_AFT_SUPPORTING, None, None) is None


def test_project_window_worked_example_between_p50_and_p90() -> None:
    """SP-REG00374 / 473597-5|AFT worked example from the approved spec:
    last.tac=17938, p50=356, p90=1968.2, current tac=18660 (closing 2026-08-20)
    -> tac_p50=18294, tac_p90=19906, cycles_since=722, between_p50_p90."""
    current_tac = CurrentTac(
        value=18660,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, current_tac)
    assert window is not None
    assert window.last_replacement_tac == 17938
    assert window.last_replacement_date == date(2026, 4, 25)
    assert window.last_replacement_wo_id == "WO-OLD-1"
    assert window.tac_p50 == 18294
    assert window.tac_p90 == 19906
    assert window.cycles_since_last_replacement == 722
    assert window.position == "between_p50_p90"
    assert window.calibrated is False
    assert window.basis == "last_replacement_plus_fleet_replacement_interval"
    assert window.unit == "aircraft_flight_cycles"
    assert window.label == "Fleet pattern, not a forecast"


def test_project_window_no_current_tac_leaves_cycles_and_position_none() -> None:
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, None)
    assert window is not None
    assert window.cycles_since_last_replacement is None
    assert window.position is None


def test_project_window_counter_inconsistent_leaves_cycles_and_position_none() -> None:
    current_tac = CurrentTac(
        value=18660,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
        counter_inconsistent=True,
    )
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, current_tac)
    assert window is not None
    assert window.cycles_since_last_replacement is None
    assert window.position is None


def test_project_window_current_tac_before_last_replacement_leaves_cycles_none() -> (
    None
):
    current_tac = CurrentTac(
        value=17000,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, current_tac)
    assert window is not None
    assert window.cycles_since_last_replacement is None
    assert window.position is None


def test_project_window_boundary_before_p50() -> None:
    # cycles_since = 355 < round(p50)=356 -> before_p50.
    current_tac = CurrentTac(
        value=17938 + 355,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, current_tac)
    assert window is not None
    assert window.cycles_since_last_replacement == 355
    assert window.position == "before_p50"


def test_project_window_boundary_exactly_p50_is_between() -> None:
    # cycles_since == round(p50)=356 -> not `< p50`, so between_p50_p90.
    current_tac = CurrentTac(
        value=17938 + 356,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, current_tac)
    assert window is not None
    assert window.cycles_since_last_replacement == 356
    assert window.position == "between_p50_p90"


def test_project_window_boundary_exactly_p90_is_between() -> None:
    # cycles_since == round(p90)=1968 -> `<= p90`, so still between_p50_p90.
    current_tac = CurrentTac(
        value=17938 + 1968,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, current_tac)
    assert window is not None
    assert window.cycles_since_last_replacement == 1968
    assert window.position == "between_p50_p90"


def test_project_window_boundary_just_past_p90() -> None:
    # cycles_since == round(p90)+1=1969 -> past_p90.
    current_tac = CurrentTac(
        value=17938 + 1969,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    window = project_window(_AFT_SUPPORTING, _AFT_LAST, current_tac)
    assert window is not None
    assert window.cycles_since_last_replacement == 1969
    assert window.position == "past_p90"


def test_round_cycles_rounds_half_up_not_to_even() -> None:
    # Python's round(380.5) == 380 (banker's); BigQuery ROUND gives 381.
    assert round_cycles(380.5) == 381
    assert round_cycles(381.5) == 382
    assert round_cycles(1890.8) == 1891
    assert round_cycles(1968.2000000000003) == 1968
    assert round_cycles(356) == 356


def test_project_window_half_cycle_percentile_rounds_half_up() -> None:
    # PERCENTILE_CONT yields .5 for an even n; the window must use 381 here.
    supporting = replace(_AFT_SUPPORTING, p50=380.5, p90=1890.8)
    current_tac = CurrentTac(
        value=17938 + 381,
        source="latest_closing_tac",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    window = project_window(supporting, _AFT_LAST, current_tac)
    assert window is not None
    assert window.tac_p50 == 17938 + 381
    assert window.tac_p90 == 17938 + 1891
    assert window.position == "between_p50_p90"


# --------------------------------------------------------------------------
# resolve_current_tac (§5.1)
# --------------------------------------------------------------------------


def test_current_tac_user_supplied_within_24h_wins() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    tac = resolve_current_tac(
        user_supplied_tac=19500,
        user_supplied_observed_at=now - timedelta(hours=1),
        now=now,
        issue_tac=19000,
        issue_date=now - timedelta(days=1),
        analysis_as_of=now,
        latest_closing=None,
        tac_stale_days=7,
    )
    assert tac is not None
    assert tac.value == 19500
    assert tac.source == "user_supplied"
    assert tac.stale is False and tac.counter_inconsistent is False


def test_current_tac_user_supplied_stale_falls_through_to_issue_tac() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    tac = resolve_current_tac(
        user_supplied_tac=19500,
        user_supplied_observed_at=now - timedelta(hours=48),
        now=now,
        issue_tac=19000,
        issue_date=now - timedelta(days=1),
        analysis_as_of=now,
        latest_closing=None,
        tac_stale_days=7,
    )
    assert tac is not None
    assert tac.value == 19000
    assert tac.source == "workorder_issue_tac"
    assert tac.stale is False


def test_current_tac_issue_tac_stale_after_window() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    tac = resolve_current_tac(
        user_supplied_tac=None,
        user_supplied_observed_at=None,
        now=now,
        issue_tac=19000,
        issue_date=now - timedelta(days=10),
        analysis_as_of=now,
        latest_closing=None,
        tac_stale_days=7,
    )
    assert tac is not None
    assert tac.source == "workorder_issue_tac"
    assert tac.stale is True


def test_current_tac_latest_closing_stale_and_counter_inconsistent() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    candidate = CurrentTac(
        value=19107,
        source="latest_closing_tac",
        observed_at=now - timedelta(days=17),
        stale=False,
        counter_inconsistent=False,
    )
    tac = resolve_current_tac(
        user_supplied_tac=None,
        user_supplied_observed_at=None,
        now=now,
        issue_tac=None,
        issue_date=None,
        analysis_as_of=now,
        latest_closing=candidate,
        latest_closing_max_tac_before_cutoff=19999,
        tac_stale_days=7,
    )
    assert tac is not None
    assert tac.value == 19107
    assert tac.source == "latest_closing_tac"
    assert tac.stale is True
    assert tac.counter_inconsistent is True


def test_current_tac_latest_closing_consistent_when_max_equals_value() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    candidate = CurrentTac(
        value=19999, source="latest_closing_tac", observed_at=now - timedelta(days=1)
    )
    tac = resolve_current_tac(
        user_supplied_tac=None,
        user_supplied_observed_at=None,
        now=now,
        issue_tac=None,
        issue_date=None,
        analysis_as_of=now,
        latest_closing=candidate,
        latest_closing_max_tac_before_cutoff=19999,
        tac_stale_days=7,
    )
    assert tac is not None
    assert tac.counter_inconsistent is False
    assert tac.stale is False


def test_current_tac_nothing_available_returns_none() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    tac = resolve_current_tac(
        user_supplied_tac=None,
        user_supplied_observed_at=None,
        now=now,
        issue_tac=None,
        issue_date=None,
        analysis_as_of=now,
        latest_closing=None,
        tac_stale_days=7,
    )
    assert tac is None


# --------------------------------------------------------------------------
# check_build (§5.6 build consistency)
# --------------------------------------------------------------------------

_REQUIRED_TABLES = (
    "dim_focus_components",
    "fct_replacement_events",
    "dim_reference_set",
    "replacement_anchor_embeddings",
    "wo_embeddings",
    "adjudicated_precursors",
    "fct_lead_time_samples",
)


def _build_info(overrides: dict[str, datetime] | None = None) -> BuildInfo:
    base = datetime(2026, 9, 23, 15, 26, 30, tzinfo=UTC)
    times = dict.fromkeys(_REQUIRED_TABLES, base)
    if overrides:
        times.update(overrides)
    return BuildInfo(table_creation_times=times)


def test_check_build_consistent_returns_none() -> None:
    assert check_build(_build_info(), max_spread_s=3600) is None


def test_check_build_missing_table_detected() -> None:
    times = {name: datetime(2026, 9, 23, tzinfo=UTC) for name in _REQUIRED_TABLES[:-1]}
    detail = check_build(BuildInfo(table_creation_times=times), max_spread_s=3600)
    assert detail is not None and "missing_tables" in detail
    assert "fct_lead_time_samples" in detail


def test_check_build_spread_too_wide() -> None:
    base = datetime(2026, 9, 23, 15, 0, 0, tzinfo=UTC)
    info = _build_info({"wo_embeddings": base + timedelta(seconds=7200)})
    detail = check_build(info, max_spread_s=3600)
    assert detail is not None and "spread_s" in detail


def test_check_build_downstream_older_than_anchor() -> None:
    base = datetime(2026, 9, 23, 15, 26, 30, tzinfo=UTC)
    info = _build_info({"fct_lead_time_samples": base - timedelta(seconds=10)})
    detail = check_build(info, max_spread_s=3600)
    assert detail is not None and "stale_downstream" in detail
    assert "fct_lead_time_samples" in detail


def test_check_build_downstream_only_rebuild_passes() -> None:
    """2026-09-24 data update: `fct_replacement_events` created a day before
    the 6 downstream tables (a legitimate downstream-only rebuild) must not
    trip the spread check - only the downstream tables' own tight spread
    matters, and the anchor is older than all of them, not younger."""
    anchor_time = datetime(2026, 9, 23, 15, 20, 37, tzinfo=UTC)
    downstream_time = datetime(2026, 9, 24, 10, 40, 0, tzinfo=UTC)
    info = _build_info(
        {
            name: downstream_time
            for name in _REQUIRED_TABLES
            if name != "fct_replacement_events"
        }
        | {"fct_replacement_events": anchor_time}
    )
    assert check_build(info, max_spread_s=3600) is None


def test_check_build_half_finished_downstream_spread_fails() -> None:
    """A downstream-only rebuild that dies partway through still leaves the
    downstream tables spread further apart than `max_spread_s`, even though
    the (old, untouched) anchor is excluded from that spread - this is the
    half-finished-CTAS case the check exists to catch."""
    anchor_time = datetime(2026, 9, 23, 15, 20, 37, tzinfo=UTC)
    base = datetime(2026, 9, 24, 10, 36, 37, tzinfo=UTC)
    info = _build_info(
        {name: base for name in _REQUIRED_TABLES if name != "fct_replacement_events"}
        | {
            "fct_replacement_events": anchor_time,
            "fct_lead_time_samples": base + timedelta(seconds=7200),
        }
    )
    detail = check_build(info, max_spread_s=3600)
    assert detail is not None and "spread_s" in detail


def test_check_build_downstream_older_than_anchor_after_downstream_rebuild() -> None:
    """Even with the anchor excluded from the spread check, a downstream
    table older than `fct_replacement_events` itself is still inconsistent
    (for example a table the rebuild skipped)."""
    anchor_time = datetime(2026, 9, 24, 10, 36, 37, tzinfo=UTC)
    downstream_time = datetime(2026, 9, 24, 10, 40, 0, tzinfo=UTC)
    info = _build_info(
        {
            name: downstream_time
            for name in _REQUIRED_TABLES
            if name != "fct_replacement_events"
        }
        | {
            "fct_replacement_events": anchor_time,
            "fct_lead_time_samples": anchor_time - timedelta(seconds=10),
        }
    )
    detail = check_build(info, max_spread_s=3600)
    assert detail is not None and "stale_downstream" in detail
    assert "fct_lead_time_samples" in detail


# --------------------------------------------------------------------------
# build_evidence: unique titles
# --------------------------------------------------------------------------


def test_build_evidence_titles_are_unique() -> None:
    same_date = datetime(2026, 5, 2, tzinfo=UTC).date()
    anchors = [
        Neighbour(
            wo_uuid="u1",
            wo_id="190529876",
            aircraft_reg="9H-REG00163",
            sim=0.91,
            snippet="AFT CARGO SMOKE DET LOOP B INOP",
            component_key="473597-5|AFT",
            replacement_date=same_date,
        ),
        Neighbour(
            wo_uuid="u2",
            wo_id="190529876",
            aircraft_reg="9H-REG00163",
            sim=0.87,
            snippet="duplicate wo id + date",
            component_key="473597-5|AFT",
            replacement_date=same_date,
        ),
    ]
    evidence = build_evidence(anchor_neighbours=anchors)
    titles = [e["title"] for e in evidence]
    assert len(titles) == len(set(titles))
    assert titles[0] == "Replacement WO 190529876 (2026-05-02)"
    assert titles[1] == "Replacement WO 190529876 (2026-05-02) (2)"


def test_build_evidence_respects_limits_and_kinds() -> None:
    anchors = [
        Neighbour(
            wo_uuid=f"u{i}",
            wo_id=str(i),
            aircraft_reg="9H-REG00163",
            sim=0.9,
            snippet="s",
            component_key="473597-5|AFT",
        )
        for i in range(10)
    ]
    precursors = [
        PrecursorEvidence(
            component_key="473597-5|AFT",
            aircraft_reg="9H-REG00163",
            precursor_wo_id=str(100 + i),
            precursor_wo_uuid=f"p{i}",
            precursor_tac=100,
            replacement_wo_uuid="r1",
            replacement_tac=200,
            replacement_date=None,
            lead_cycles=6,
            sim=0.92,
            reason="symptom",
            precursor_snippet="snippet",
        )
        for i in range(10)
    ]
    wo_neighbours = [
        Neighbour(
            wo_uuid=f"w{i}",
            wo_id=str(200 + i),
            aircraft_reg="9H-REG00163",
            sim=0.8,
            snippet="s",
        )
        for i in range(10)
    ]
    evidence = build_evidence(
        anchor_neighbours=anchors,
        precursor_evidence=precursors,
        wo_neighbours=wo_neighbours,
        anchor_limit=5,
        precursor_limit=5,
        wo_limit=3,
    )
    kinds = [e["kind"] for e in evidence]
    assert kinds.count("similar_past_replacement") == 5
    assert kinds.count("adjudicated_precursor") == 5
    assert kinds.count("similar_past_workorder") == 3


# --------------------------------------------------------------------------
# limitations (§5.5)
# --------------------------------------------------------------------------


def test_limitations_always_on_only() -> None:
    result = limitations(
        mode="new_work_order", embedding_endpoint_label_present=True, current_tac=None
    )
    assert result == [
        "not_calibrated",
        "outcome_selected_sample",
        "no_installation_eligibility_check",
        "closing_tac_basis",
    ]


def test_limitations_replay_adds_in_sample_flag() -> None:
    result = limitations(
        mode="historical_replay",
        embedding_endpoint_label_present=True,
        current_tac=None,
    )
    assert "in_sample_curated_artifacts" in result


def test_limitations_missing_endpoint_label_adds_flag() -> None:
    result = limitations(
        mode="new_work_order", embedding_endpoint_label_present=False, current_tac=None
    )
    assert "embedding_provenance_unverified" in result


def test_limitations_stale_and_counter_inconsistent_tac() -> None:
    tac = CurrentTac(
        value=1,
        source="latest_closing_tac",
        observed_at=None,
        stale=True,
        counter_inconsistent=True,
    )
    result = limitations(
        mode="new_work_order", embedding_endpoint_label_present=True, current_tac=tac
    )
    assert "current_tac_stale" in result
    assert "counter_inconsistent" in result


# --------------------------------------------------------------------------
# Condensed recommendation (approved 2026-09-24, USER REQUEST: base it on
# `wo_embeddings` similar-work-order matching plus `fct_lead_time_samples`)
# --------------------------------------------------------------------------

_REC_SETTINGS = PredictionSettings()  # min_sample=5, rec_min_sample_high=8,
# rec_max_cv=0.5, rec_strong_sim=0.80, min_vote_support=3, min_vote_share=0.5


def _lead_sample(
    component_key: str,
    uuid: str,
    lead_cycles: int | None,
    aircraft_reg: str = "9H-REG1",
) -> NeighbourLeadSample:
    return NeighbourLeadSample(
        component_key=component_key,
        precursor_wo_uuid=uuid,
        aircraft_reg=aircraft_reg,
        lead_cycles=lead_cycles,
    )


def _wo_neighbour(uuid: str, sim: float, wo_id: str | None = None) -> Neighbour:
    return Neighbour(
        wo_uuid=uuid, wo_id=wo_id or uuid, aircraft_reg="9H-REG1", sim=sim, snippet=""
    )


def test_aggregate_neighbour_lead_samples_groups_and_computes_percentiles() -> None:
    samples = [
        _lead_sample("PN1|AFT", "u1", 80),
        _lead_sample("PN1|AFT", "u2", 100),
        _lead_sample("PN1|AFT", "u3", 120),
        _lead_sample("PN2|FWD", "u4", 50),
        _lead_sample("PN1|AFT", "u5", None),  # dropped: no lead_cycles
    ]
    sims = {"u1": 0.9, "u2": 0.8, "u3": 0.7, "u4": 0.6}
    aggs = aggregate_neighbour_lead_samples(samples, sims)
    assert set(aggs) == {"PN1|AFT", "PN2|FWD"}
    aft = aggs["PN1|AFT"]
    assert aft.n == 3
    assert aft.lead_p50 == pytest.approx(100.0)
    assert aft.mean_sim == pytest.approx((0.9 + 0.8 + 0.7) / 3)
    assert aft.top_sim == pytest.approx(0.9)
    assert aggs["PN2|FWD"].n == 1


def test_aggregate_neighbour_lead_samples_missing_sim_defaults_to_zero() -> None:
    samples = [_lead_sample("PN1|AFT", "unknown-uuid", 100)]
    aggs = aggregate_neighbour_lead_samples(samples, {})
    assert aggs["PN1|AFT"].mean_sim == 0.0
    assert aggs["PN1|AFT"].top_sim == 0.0


def _current_tac(value: int, *, counter_inconsistent: bool = False) -> CurrentTac:
    return CurrentTac(
        value=value,
        source="latest_closing_tac",
        observed_at=datetime(2026, 9, 24, tzinfo=UTC),
        counter_inconsistent=counter_inconsistent,
    )


_EXACT_MATCH_GATE = GateResult(
    final=True,
    passed=True,
    decision=PredictionDecision.HISTORICAL_INTERVAL,
    component_key="PN1|AFT",
    part_number="PN1",
    position="AFT",
    gate_basis="exact_pn_position",
    match=ComponentMatch(gate_basis="exact_pn_position"),
)
_NO_DECISION = Decision(decision=PredictionDecision.OUT_OF_SCOPE)


def test_build_recommendation_exact_match_similar_workorders_high_confidence() -> None:
    """Basis (a): the gate's own exact-match component_key has a
    `neighbour_lead_samples` aggregate of its own - wins outright, no vote
    needed. n=8, cv=0 (identical leads), mean_sim=0.85 -> `high`."""
    samples = [_lead_sample("PN1|AFT", f"u{i}", 100) for i in range(8)]
    neighbours = [_wo_neighbour(f"u{i}", 0.85) for i in range(8)]
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=_NO_DECISION,
        stats=None,
        last_replacement=None,
        current_tac=_current_tac(17000),
        neighbour_lead_samples=samples,
        wo_neighbours=neighbours,
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "similar_workorders"
    assert rec.component_key == "PN1|AFT"
    assert rec.predicted_replacement.lead_tac_p50 == 100
    assert rec.predicted_replacement.tac_p50 == 17100
    assert rec.confidence.level == "high"
    assert rec.confidence.similarity == pytest.approx(0.85)
    assert rec.confidence.sample_size == 8
    assert rec.confidence.cv == pytest.approx(0.0)
    assert rec.action == "recommend_inspection_or_part_planning"
    assert rec.reference_tac == 17000
    assert rec.reference_tac_source == "latest_closing_tac"
    assert len(rec.evidence) == 3


def test_build_recommendation_weighted_vote_picks_dominant_component() -> None:
    """Basis (b): gate did not exact-match (symptom-only WO), two component
    keys aggregate from the neighbour set - the weighted vote (mean_sim * n)
    picks PN2|AFT over PN3|FWD, gated by min_vote_support/min_vote_share."""
    samples = [
        *[_lead_sample("PN2|AFT", f"a{i}", 100) for i in range(5)],
        *[_lead_sample("PN3|FWD", f"b{i}", 200) for i in range(3)],
    ]
    neighbours = [
        *[_wo_neighbour(f"a{i}", 0.9) for i in range(5)],
        *[_wo_neighbour(f"b{i}", 0.5) for i in range(3)],
    ]
    focus = [_focus("PN2", "AFT")]
    gate = GateResult(
        final=True,
        passed=False,
        decision=PredictionDecision.OUT_OF_SCOPE,
        reason=PredictionReason.COMPONENT_NOT_IN_FOCUS_SET,
    )
    rec = build_recommendation(
        gate=gate,
        decision=Decision(decision=PredictionDecision.OUT_OF_SCOPE),
        stats=None,
        last_replacement=None,
        current_tac=_current_tac(5000),
        neighbour_lead_samples=samples,
        wo_neighbours=neighbours,
        settings=_REC_SETTINGS,
        focus_components=focus,
    )
    assert rec is not None
    assert rec.basis == "similar_workorders"
    assert rec.component_key == "PN2|AFT"
    assert rec.confidence.sample_size == 5
    assert rec.confidence.level == "medium"  # n=5 >= min_vote_support(3), < 8


def test_build_recommendation_vote_rejects_sibling_position_conflict() -> None:
    """The weighted vote never picks between sibling engine positions
    (#1/#2) - same safety rule as the anchor-neighbour vote - so a winner
    whose part number has two focus positions is rejected outright, and (no
    other basis applying) the recommendation falls through to `None`."""
    samples = [_lead_sample("PN9|#1", f"c{i}", 100) for i in range(5)]
    neighbours = [_wo_neighbour(f"c{i}", 0.9) for i in range(5)]
    focus = [_focus("PN9", "#1"), _focus("PN9", "#2")]
    gate = GateResult(
        final=True,
        passed=False,
        decision=PredictionDecision.OUT_OF_SCOPE,
        reason=PredictionReason.COMPONENT_NOT_IN_FOCUS_SET,
    )
    rec = build_recommendation(
        gate=gate,
        decision=Decision(decision=PredictionDecision.OUT_OF_SCOPE),
        stats=None,
        last_replacement=None,
        current_tac=_current_tac(5000),
        neighbour_lead_samples=samples,
        wo_neighbours=neighbours,
        settings=_REC_SETTINGS,
        focus_components=focus,
    )
    assert rec is None


def test_build_recommendation_similar_workorders_falls_through_without_current_tac() -> (
    None
):
    """(a)/(b) and (c) all project from the reference TAC (`current_tac`);
    without one they fall through to (d) `fleet_replacement_interval`, which
    anchors on the aircraft's own last replacement instead (TACs set, leads
    `None` because there is no reference TAC to subtract)."""
    samples = [_lead_sample("PN1|AFT", f"u{i}", 100) for i in range(8)]
    neighbours = [_wo_neighbour(f"u{i}", 0.85) for i in range(8)]
    stats = LeadStats(
        component_key="PN1|AFT", n=10, lead_p50=200.0, lead_p90=400.0, lead_p95=450.0
    )
    decision = Decision(
        decision=PredictionDecision.NO_RELIABLE_PREDICTION,
        supporting_interval=IntervalStats(
            basis="closing_tac_to_closing_tac_interval",
            unit="aircraft_flight_cycles",
            p50=200.0,
            p90=400.0,
            min=50,
            max=500,
            n=10,
            calibrated=False,
        ),
    )
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=decision,
        stats=stats,
        last_replacement=LastReplacement(
            tac=10000, replacement_date=date(2026, 1, 1), wo_id="WO-1", wo_uuid="wo-1"
        ),
        current_tac=None,
        neighbour_lead_samples=samples,
        wo_neighbours=neighbours,
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "fleet_replacement_interval"
    assert rec.predicted_replacement.tac_p50 == 10200
    assert rec.predicted_replacement.lead_tac_p50 is None
    assert rec.reference_tac is None


def test_build_recommendation_similar_workorders_falls_through_when_counter_inconsistent() -> (
    None
):
    samples = [_lead_sample("PN1|AFT", f"u{i}", 100) for i in range(8)]
    neighbours = [_wo_neighbour(f"u{i}", 0.85) for i in range(8)]
    stats = LeadStats(component_key="PN1|AFT", n=10, lead_p50=200.0, lead_p90=400.0)
    decision = Decision(
        decision=PredictionDecision.HISTORICAL_INTERVAL,
        interval=IntervalStats(
            basis="closing_tac_to_closing_tac_interval",
            unit="aircraft_flight_cycles",
            p50=200.0,
            p90=400.0,
            min=50,
            max=500,
            n=10,
            calibrated=False,
        ),
    )
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=decision,
        stats=stats,
        last_replacement=None,
        current_tac=_current_tac(5000, counter_inconsistent=True),
        neighbour_lead_samples=samples,
        wo_neighbours=neighbours,
        settings=_REC_SETTINGS,
    )
    # A counter-inconsistent TAC is not a usable reference, and there is no
    # last replacement / supporting interval for (d): no recommendation.
    assert rec is None


def test_build_recommendation_component_history_basis() -> None:
    """Basis (c): no neighbour_lead_samples at all, gate matched and
    `decide()` already produced a calibrated HISTORICAL_INTERVAL. Evidence
    falls back to the top `wo_neighbours`."""
    stats = LeadStats(
        component_key="PN1|AFT",
        n=10,
        lead_p50=200.0,
        lead_p90=400.0,
        lead_p95=450.0,
        cv=0.2,
    )
    decision = Decision(
        decision=PredictionDecision.HISTORICAL_INTERVAL,
        interval=IntervalStats(
            basis="closing_tac_to_closing_tac_interval",
            unit="aircraft_flight_cycles",
            p50=200.0,
            p90=400.0,
            min=50,
            max=500,
            n=10,
            calibrated=False,
        ),
    )
    last = LastReplacement(
        tac=10000, replacement_date=date(2026, 1, 1), wo_id="WO-1", wo_uuid="wo-1"
    )
    neighbours = [
        _wo_neighbour("n1", 0.6),
        _wo_neighbour("n2", 0.75),
        _wo_neighbour("n3", 0.4),
    ]
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=decision,
        stats=stats,
        last_replacement=last,
        current_tac=_current_tac(10100),
        neighbour_lead_samples=[],
        wo_neighbours=neighbours,
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "component_history"
    assert rec.predicted_replacement.tac_p50 == 10300  # current 10100 + p50 200
    assert rec.predicted_replacement.lead_tac_p50 == 200
    assert rec.reference_tac == 10100
    assert rec.predicted_replacement.tac_p90 == 10500
    assert rec.predicted_replacement.tac_p95 == 10550
    assert rec.confidence.level == "medium"  # n=10>=8, cv=0.2<=0.5
    assert rec.confidence.similarity == pytest.approx(0.75)  # top wo_neighbour sim
    assert rec.evidence[0]["sim"] == pytest.approx(0.75)  # sorted desc


def test_build_recommendation_exact_match_below_min_vote_support_uses_component_history() -> (
    None
):
    """Two matched neighbour samples would collapse p50/p90/p95 into one
    value; with fewer than `min_vote_support` (3) the exact-match component
    falls back to its own lead-time history (c)."""
    stats = LeadStats(
        component_key="PN1|AFT",
        n=13,
        lead_p50=1659.0,
        lead_p90=2301.0,
        lead_p95=2624.0,
        cv=0.8,
    )
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=_NO_DECISION,
        stats=stats,
        last_replacement=None,
        current_tac=_current_tac(6584),
        neighbour_lead_samples=[
            _lead_sample("PN1|AFT", "u1", 1659),
            _lead_sample("PN1|AFT", "u2", 1659),
        ],
        wo_neighbours=[_wo_neighbour("u1", 0.8), _wo_neighbour("u2", 0.79)],
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "component_history"
    assert rec.confidence.sample_size == 13
    assert rec.predicted_replacement.tac_p50 == 6584 + 1659
    assert rec.predicted_replacement.tac_p95 == 6584 + 2624


def test_build_recommendation_component_history_low_confidence_below_sample_floor() -> (
    None
):
    stats = LeadStats(component_key="PN1|AFT", n=6, lead_p50=200.0, lead_p90=400.0)
    decision = Decision(
        decision=PredictionDecision.HISTORICAL_INTERVAL,
        interval=IntervalStats(
            basis="closing_tac_to_closing_tac_interval",
            unit="aircraft_flight_cycles",
            p50=200.0,
            p90=400.0,
            min=50,
            max=500,
            n=6,
            calibrated=False,
        ),
    )
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=decision,
        stats=stats,
        last_replacement=None,
        current_tac=_current_tac(10100),
        neighbour_lead_samples=[],
        wo_neighbours=[],
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "component_history"
    assert rec.confidence.level == "low"  # min_sample 5 <= n=6 < rec_min_sample_high 8
    assert rec.predicted_replacement.tac_p50 == 10300
    assert rec.action == "monitor"


def test_build_recommendation_fleet_replacement_interval_basis() -> None:
    """Basis (d): the SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT row's
    supporting_interval, anchored on last_replacement. Always `low`
    confidence regardless of sample size (spec item 8)."""
    supporting = IntervalStats(
        basis="consecutive_replacement_closing_tac_interval",
        unit="aircraft_flight_cycles",
        p50=356.0,
        p90=1968.2,
        min=4,
        max=3158,
        n=27,
        calibrated=False,
        label="Observed interval between consecutive replacements. Not a forecast.",
        aircraft=24,
    )
    decision = Decision(
        decision=PredictionDecision.NO_RELIABLE_PREDICTION,
        reason=PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT,
        supporting_interval=supporting,
    )
    last = LastReplacement(
        tac=17938,
        replacement_date=date(2026, 4, 25),
        wo_id="WO-OLD-1",
        wo_uuid="wo-old-uuid-1",
    )
    gate = GateResult(
        final=True,
        passed=True,
        decision=PredictionDecision.NO_RELIABLE_PREDICTION,
        component_key="PN1|AFT",
        gate_basis="exact_pn_position",
        match=ComponentMatch(gate_basis="exact_pn_position"),
    )
    rec = build_recommendation(
        gate=gate,
        decision=decision,
        stats=None,
        last_replacement=last,
        current_tac=_current_tac(18660),
        neighbour_lead_samples=[],
        wo_neighbours=[],
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "fleet_replacement_interval"
    assert rec.component_key == "PN1|AFT"
    assert rec.predicted_replacement.tac_p50 == 18294
    assert rec.predicted_replacement.tac_p90 == 19906
    assert rec.confidence.level == "low"


def test_build_recommendation_none_when_nothing_supports_it() -> None:
    gate = GateResult(
        final=True,
        passed=False,
        decision=PredictionDecision.OUT_OF_SCOPE,
        reason=PredictionReason.COMPONENT_NOT_IN_FOCUS_SET,
    )
    rec = build_recommendation(
        gate=gate,
        decision=Decision(decision=PredictionDecision.OUT_OF_SCOPE),
        stats=None,
        last_replacement=None,
        current_tac=None,
        neighbour_lead_samples=[],
        wo_neighbours=[],
        settings=_REC_SETTINGS,
    )
    assert rec is None


def test_build_recommendation_action_override_when_reference_tac_reached_p50() -> None:
    """`reference_tac >= tac_p50` forces `recommend_inspection_or_part_
    planning` even at `low` confidence (spec item 10's "already due"
    override). Uses `fleet_replacement_interval` basis: `tac_p50` anchors off
    `last_replacement.tac`, independent of `reference_tac` (current_tac.value),
    so the aircraft can genuinely have already flown past the projected
    p50 replacement point - unlike `similar_workorders`/`component_history`,
    where tac_p50 is current_tac + a positive lead and can never be reached."""
    stats = LeadStats(component_key="PN1|AFT", n=3, lead_p50=200.0, lead_p90=400.0)
    decision = Decision(
        decision=PredictionDecision.NO_RELIABLE_PREDICTION,
        supporting_interval=IntervalStats(
            basis="closing_tac_to_closing_tac_interval",
            unit="aircraft_flight_cycles",
            p50=200.0,
            p90=400.0,
            min=50,
            max=500,
            n=3,
            calibrated=False,
        ),
    )
    last = LastReplacement(
        tac=10000, replacement_date=date(2026, 1, 1), wo_id="WO-1", wo_uuid="wo-1"
    )
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=decision,
        stats=stats,
        last_replacement=last,
        current_tac=_current_tac(10300),  # already past tac_p50 = 10000+200 = 10200
        neighbour_lead_samples=[],
        wo_neighbours=[],
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "fleet_replacement_interval"
    assert rec.confidence.level == "low"  # fleet basis is always low
    assert rec.predicted_replacement.lead_tac_p50 == -100
    assert rec.predicted_replacement.tac_p50 == 10200
    assert rec.reference_tac == 10300
    assert rec.action == "recommend_inspection_or_part_planning"


def test_build_recommendation_low_confidence_stays_monitor_when_not_yet_due() -> None:
    # n=5 (< rec_min_sample_high) with a wide spread: low confidence.
    stats = LeadStats(
        component_key="PN1|AFT", n=5, lead_p50=100.0, lead_p90=300.0, lead_p95=400.0, cv=0.9
    )
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=_NO_DECISION,
        stats=stats,
        last_replacement=None,
        current_tac=_current_tac(16000),  # well before tac_p50=16100
        neighbour_lead_samples=[],
        wo_neighbours=[_wo_neighbour("u1", 0.9)],
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.basis == "component_history"
    assert rec.confidence.level == "low"
    assert rec.action == "monitor"


def test_build_recommendation_exact_gate_without_samples_never_votes_other_part() -> (
    None
):
    """An exact PN+position gate match whose component has no matched samples
    must not be overridden by a strong vote for a *different* component
    (e.g. a CABIN PN on a WO whose text reads like the AFT fire loop): it
    goes to (c) for the gate's own component instead."""
    samples = [_lead_sample("OTHER|AFT", f"u{i}", 100) for i in range(8)]
    neighbours = [_wo_neighbour(f"u{i}", 0.9) for i in range(8)]
    stats = LeadStats(
        component_key="PN1|AFT", n=6, lead_p50=200.0, lead_p90=400.0, lead_p95=450.0
    )
    rec = build_recommendation(
        gate=_EXACT_MATCH_GATE,
        decision=Decision(decision=PredictionDecision.HISTORICAL_INTERVAL),
        stats=stats,
        last_replacement=None,
        current_tac=_current_tac(10000),
        neighbour_lead_samples=samples,
        wo_neighbours=neighbours,
        settings=_REC_SETTINGS,
    )
    assert rec is not None
    assert rec.component_key == "PN1|AFT"
    assert rec.basis == "component_history"
    assert rec.predicted_replacement.tac_p95 == 10450
