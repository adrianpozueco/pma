"""Unit tests for the pure/sync parts of ``pm_agent/nodes/evidence_branches.py``
(N5): ``_merge_branch``, ``_unavailable_result`` and ``_run_bq_tools``.

No asyncio, no ADK graph, no real BigQuery/Vertex AI - a fake ``Client`` wraps
a real :class:`QueryRunner` (same pattern as ``test_bq_predefined_queries.py``)
and a real ``LocalHistoryProvider`` (same pattern as
``test_bq_evidence_adapters.py``) stands in for the semantic-search branch.
The graph wiring/concurrency/join/render behavior is covered separately in
``tests/integration/test_adk_evidence_branches.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from amos_data.retrieval import LocalHistoryProvider
from pm_agent.nodes import evidence_branches
from pm_agent.nodes.evidence_branches import (
    _merge_branch,
    _pma_lines,
    _run_bq_tools,
    _unavailable_result,
    bq_evidence,
    ipc_evidence,
)
from pm_agent.sub_agents.bq_analytics.queries import QueryRunner
from pm_agent.workorders.evidence import (
    AircraftContext,
    EvidenceRequest,
    ExclusionSet,
    PartCandidate,
    SourceResult,
    SourceStatus,
)

AS_OF = "2026-06-01T00:00:00+00:00"


def _request(
    *,
    selected_wo_id=None,
    part_candidates=(),
    aircraft=None,
    available_symptoms=(),
    exclusions=None,
    analysis_as_of=AS_OF,
) -> EvidenceRequest:
    return EvidenceRequest(
        question="why did the pump fail",
        input_mode="new_work_order",
        analysis_as_of=analysis_as_of,
        invocation_scope_id="scope-1",
        selected_wo_id=selected_wo_id,
        part_candidates=part_candidates,
        aircraft=aircraft or AircraftContext(),
        available_symptoms=available_symptoms,
        exclusions=exclusions or ExclusionSet(),
    )


# --- fake BigQuery client/job, same pattern as test_bq_predefined_queries.py ---


class Job:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def result(self, timeout, **_):
        return list(self.rows)

    def cancel(self, **_):
        pass


class Client:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.sql_calls: list[str] = []

    def query(self, sql, job_config, **kwargs):
        self.sql_calls.append(sql)
        return self.jobs.pop(0) if self.jobs else Job()


def _runner(jobs) -> QueryRunner:
    return QueryRunner(Client(jobs=jobs), project="valid-project")


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "document_id": "doc-1",
        "source_namespace": "amos",
        "record_id": "rec-1",
        "workorder_id": "wo-1",
        "workorder_number": "WO-0001",
        "aircraft_id": "AC-1",
        "aircraft_family": "A320",
        "split": "train",
        "reference_corpus_version": "v1",
        "available_at": "2026-01-01T00:00:00+00:00",
        "availability_status": "available",
        "text_hash": "hash-1",
        "position": None,
        "ata_code": None,
        "component_class": None,
        "raw_text": "pump seal leak reported on inspection",
        "normalized_text": "PUMP SEAL LEAK REPORTED ON INSPECTION",
        "part_keys": ["ABC12304"],
        "source_span": "wo:WO-0001#1",
    }
    base.update(overrides)
    return base


# ------------------------------- _merge_branch ------------------------------


def test_merge_branch_empty_results_is_no_match():
    result = _merge_branch("bq_evidence", [])
    assert result.status == SourceStatus.NO_MATCH
    assert result.counts == {"tools_executed": 0}
    assert result.records == ()


def test_merge_branch_all_success_aggregates_records_and_counts():
    results = [
        SourceResult(
            source="bigquery.get_workorder",
            status=SourceStatus.SUCCESS,
            records=({"a": 1},),
            source_ids=("wo-1",),
            counts={"workorder_header_rows": 1},
        ),
        SourceResult(
            source="bigquery.get_workorder_actions",
            status=SourceStatus.SUCCESS,
            records=({"b": 2},),
            source_ids=("act-1",),
            counts={"action_rows": 1},
        ),
    ]
    merged = _merge_branch("bq_evidence", results)
    assert merged.status == SourceStatus.SUCCESS
    assert merged.records == ({"a": 1}, {"b": 2})
    assert merged.source_ids == ("wo-1", "act-1")
    assert merged.counts == {
        "tools_executed": 2,
        "tools_succeeded": 2,
        "tools_failed": 0,
    }
    tools = merged.executed_parameters["tools"]
    assert tools["bigquery.get_workorder"]["status"] == "success"
    assert tools["bigquery.get_workorder_actions"]["status"] == "success"
    assert merged.error_detail is None


def test_merge_branch_success_and_failure_mix_still_reports_success():
    results = [
        SourceResult(
            source="bigquery.get_workorder",
            status=SourceStatus.SUCCESS,
            records=({"a": 1},),
        ),
        SourceResult(
            source="bigquery.get_workorder_actions",
            status=SourceStatus.TIMEOUT,
            error_detail="timeout:TimeoutError",
        ),
    ]
    merged = _merge_branch("bq_evidence", results)
    # Partial evidence is still evidence: one success anywhere -> SUCCESS overall.
    assert merged.status == SourceStatus.SUCCESS
    assert merged.records == ({"a": 1},)
    assert merged.counts == {
        "tools_executed": 2,
        "tools_succeeded": 1,
        "tools_failed": 1,
    }
    # The per-tool breakdown still names the failing sub-tool and its detail.
    tools = merged.executed_parameters["tools"]
    assert tools["bigquery.get_workorder_actions"]["status"] == "timeout"
    assert tools["bigquery.get_workorder_actions"]["error_detail"] == "timeout:TimeoutError"
    # Overall error_detail is only set when the *overall* status is a failure.
    assert merged.error_detail is None


def test_merge_branch_no_match_only_is_no_match():
    results = [
        SourceResult(source="bigquery.get_workorder", status=SourceStatus.NO_MATCH),
        SourceResult(
            source="bigquery.get_workorder_actions", status=SourceStatus.NO_MATCH
        ),
    ]
    merged = _merge_branch("bq_evidence", results)
    assert merged.status == SourceStatus.NO_MATCH
    assert merged.records == ()
    assert merged.counts == {
        "tools_executed": 2,
        "tools_succeeded": 0,
        "tools_failed": 0,
    }


def test_merge_branch_worst_failure_precedence_permission_denied_over_others():
    results = [
        SourceResult(source="a", status=SourceStatus.UNAVAILABLE, error_detail="u"),
        SourceResult(source="b", status=SourceStatus.TIMEOUT, error_detail="t"),
        SourceResult(source="c", status=SourceStatus.ERROR, error_detail="e"),
        SourceResult(
            source="d", status=SourceStatus.PERMISSION_DENIED, error_detail="p"
        ),
    ]
    merged = _merge_branch("bq_evidence", results)
    assert merged.status == SourceStatus.PERMISSION_DENIED
    assert merged.error_detail == "p"


def test_merge_branch_worst_failure_precedence_error_over_timeout_and_unavailable():
    results = [
        SourceResult(source="a", status=SourceStatus.UNAVAILABLE, error_detail="u"),
        SourceResult(source="b", status=SourceStatus.TIMEOUT, error_detail="t"),
        SourceResult(source="c", status=SourceStatus.ERROR, error_detail="e"),
    ]
    merged = _merge_branch("bq_evidence", results)
    assert merged.status == SourceStatus.ERROR
    assert merged.error_detail == "e"


def test_merge_branch_worst_failure_precedence_timeout_over_unavailable():
    results = [
        SourceResult(source="a", status=SourceStatus.UNAVAILABLE, error_detail="u"),
        SourceResult(source="b", status=SourceStatus.TIMEOUT, error_detail="t"),
    ]
    merged = _merge_branch("bq_evidence", results)
    assert merged.status == SourceStatus.TIMEOUT
    assert merged.error_detail == "t"


# ----------------------------- _unavailable_result ---------------------------


def test_unavailable_result_shape():
    result = _unavailable_result(
        "bigquery.find_historical_symptoms",
        part_number="ABC-123-04",
        reason="no_history_provider_configured",
    )
    assert result.source == "bigquery.find_historical_symptoms"
    assert result.status == SourceStatus.UNAVAILABLE
    assert result.executed_parameters == {"part_number": "ABC-123-04"}
    assert result.error_detail == "unavailable:no_history_provider_configured"


# ------------------------------- _run_bq_tools -------------------------------


def test_run_bq_tools_with_selected_wo_id_calls_the_three_workorder_tools():
    jobs = [
        Job(rows=[{"workorder_id": "wo-1", "workorder_number": "WO-1"}]),
        Job(rows=[{"workorder_id": "wo-1", "action_uuid": "act-1"}]),
        Job(rows=[]),
    ]
    runner = _runner(jobs)
    request = _request(selected_wo_id="WO-1")
    result = _run_bq_tools(request, runner, provider=None)
    tools = result.executed_parameters["tools"]
    assert set(tools) == {
        "bigquery.get_workorder",
        "bigquery.get_workorder_actions",
        "bigquery.get_component_changes",
    }
    assert tools["bigquery.get_workorder"]["status"] == "success"
    assert tools["bigquery.get_workorder_actions"]["status"] == "success"
    assert tools["bigquery.get_component_changes"]["status"] == "no_match"
    assert result.status == SourceStatus.SUCCESS
    assert result.counts["tools_executed"] == 3


def test_run_bq_tools_with_part_candidate_and_provider_calls_history_and_symptom_tools():
    docs = [_doc(raw_text="pump seal leak reported", normalized_text="PUMP SEAL LEAK REPORTED")]
    provider = LocalHistoryProvider(docs)
    jobs = [Job(rows=[{"workorder_id": "wo-2", "part_off_number": "ABC-123-04"}])]
    runner = _runner(jobs)
    request = _request(
        part_candidates=(PartCandidate(part_key="ABC12304", part_number="ABC-123-04"),),
        available_symptoms=("pump seal leak",),
    )
    result = _run_bq_tools(request, runner, provider)
    tools = result.executed_parameters["tools"]
    assert set(tools) == {
        "bigquery.get_part_history",
        "bigquery.find_historical_symptoms",
        "bigquery.find_faa_reports",
    }
    assert tools["bigquery.get_part_history"]["status"] == "success"
    assert result.status == SourceStatus.SUCCESS


def test_run_bq_tools_without_provider_marks_history_symptom_tools_unavailable():
    jobs = [Job(rows=[{"workorder_id": "wo-2", "part_off_number": "ABC-123-04"}])]
    runner = _runner(jobs)
    request = _request(
        part_candidates=(PartCandidate(part_key="ABC12304", part_number="ABC-123-04"),),
    )
    result = _run_bq_tools(request, runner, provider=None)
    tools = result.executed_parameters["tools"]
    assert tools["bigquery.find_historical_symptoms"]["status"] == "unavailable"
    assert tools["bigquery.find_historical_symptoms"]["error_detail"] == (
        "unavailable:no_history_provider_configured"
    )
    assert tools["bigquery.find_faa_reports"]["status"] == "unavailable"
    # The part-history tool itself still runs (it does not need the provider).
    assert tools["bigquery.get_part_history"]["status"] == "success"
    # Partial evidence (get_part_history succeeded) is still SUCCESS overall.
    assert result.status == SourceStatus.SUCCESS


def test_run_bq_tools_with_nothing_selected_returns_empty_no_match():
    runner = _runner([])
    request = _request()
    result = _run_bq_tools(request, runner, provider=None)
    assert result.status == SourceStatus.NO_MATCH
    assert result.counts == {"tools_executed": 0}


def test_run_bq_tools_runs_a_pma_role_candidate_like_any_other():
    """`workorder_upload.py` appends a `roles=("pma_component",)` candidate
    (T13b); `_run_bq_tools` iterates every candidate regardless of role, so
    it must be picked up with no code change here."""
    jobs = [Job(rows=[{"workorder_id": "wo-3", "part_off_number": "PN1"}])]
    runner = _runner(jobs)
    request = _request(
        part_candidates=(
            PartCandidate(part_key="PN1", part_number="PN1", roles=("pma_component",)),
        ),
    )
    result = _run_bq_tools(request, runner, provider=None)
    tools = result.executed_parameters["tools"]
    assert tools["bigquery.get_part_history"]["status"] == "success"
    assert result.status == SourceStatus.SUCCESS


# ------------------------------- _pma_lines ----------------------------------
# Section (d)'s PMA rendering, per §6.5's fixed user-facing strings table.


def test_pma_lines_empty_pma_renders_nothing():
    assert _pma_lines({}) == []


def test_pma_lines_historical_interval_renders_header_interval_and_match():
    pma = {
        "decision": "historical_interval",
        "reason": None,
        "component_key": "PN1|POS-A",
        "match": {"gate_basis": "exact_pn_position"},
        "interval": {"p50": 1200, "p90": 2400, "n": 8, "aircraft": 5},
    }
    lines = _pma_lines(pma)
    assert "Matched component: PN1\\|POS-A (exact\\_pn\\_position)." in lines
    assert "**Observed historical interval (not a forecast)**" in lines
    assert (
        "Between closing TACs: p50 1200 cycles, p90 2400 cycles (n=8, 5 aircraft)."
        in lines
    )
    # No "no reliable prediction"/"out of scope" wording leaks in.
    assert not any("No reliable prediction" in line for line in lines)
    assert not any("Out of scope" in line for line in lines)


def test_pma_lines_no_reliable_prediction_has_no_interval_wording():
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "insufficient_samples",
        "component_key": "PN1|POS-A",
        "match": {"gate_basis": "anchor_vote", "vote_support": 4},
    }
    lines = _pma_lines(pma)
    assert "Matched component: PN1\\|POS-A (anchor\\_vote)." in lines
    assert (
        "No reliable prediction: too few historical samples exist for this component."
        in lines
    )
    # Interval wording only appears for a historical_interval decision.
    assert not any("not a forecast" in line for line in lines)


def test_pma_lines_out_of_scope_no_component_key():
    pma = {"decision": "out_of_scope", "reason": "aircraft_type_not_in_scope"}
    lines = _pma_lines(pma)
    assert lines == [
        "Out of scope for PMA prediction: this aircraft type is not in scope."
    ]


def test_pma_lines_supporting_interval_and_stale_tac():
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "no_lead_time_samples",
        "supporting_interval": {"p50": 300, "p90": 900, "n": 4, "aircraft": 3},
        "current_tac": {
            "value": 100,
            "source": "issue",
            "observed_at": "2025-01-01",
            "stale": True,
        },
    }
    lines = _pma_lines(pma)
    assert (
        "Fleet pattern (not a forecast): this component was replaced again after "
        "p50 300 / p90 900 cycles (n=4, 3 aircraft)." in lines
    )
    assert (
        "Current aircraft TAC is stale (last seen 2025-01-01); no due TAC is given."
        in lines
    )
    # No window/position wording without a `projected_window` (Option B).
    assert not any("Projected window" in line for line in lines)
    assert not any("fleet median" in line or "fleet p90" in line for line in lines)


def test_pma_lines_disabled_reason_renders_no_reliable_prediction():
    pma = {"decision": "no_reliable_prediction", "reason": "prediction_disabled"}
    assert _pma_lines(pma) == ["No reliable prediction: PMA prediction is disabled."]


# --- Option B: projected replacement window (approved 2026-09-24, requirement 7) ---
# Live-data numbers from 473597-5|AFT / SP-REG00374 (PMA-ONLINE-AGENT-plan.md
# background): n=27, p50 356, p90 ~1968.2 -> tac_p50 18294, tac_p90 19906 off
# last_replacement_tac 17938; latest known TAC 18660 is 722 cycles later.


def _aft_supporting_interval() -> dict[str, Any]:
    # p90 deliberately carries BigQuery PERCENTILE_CONT float noise so the
    # rounding fix (§7 requirement 7) is exercised, not just a round number.
    return {"p50": 356, "p90": 1968.2000000000003, "n": 27, "aircraft": 24}


def test_pma_lines_projected_window_renders_window_and_position_rounded():
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "component_key": "473597-5|AFT",
        "match": {"gate_basis": "anchor_vote"},
        "supporting_interval": _aft_supporting_interval(),
        "projected_window": {
            "last_replacement_tac": 17938,
            "last_replacement_date": "2026-04-25",
            "tac_p50": 18294,
            "tac_p90": 19906,
            "cycles_since_last_replacement": 722,
            "position": "between_p50_p90",
        },
    }
    lines = _pma_lines(pma)
    assert (
        "Fleet pattern (not a forecast): this component was replaced again after "
        "p50 356 / p90 1968 cycles (n=27, 24 aircraft)."
    ) in lines
    assert (
        "Projected window for this aircraft (fleet pattern, not a forecast): last "
        "replaced at TAC 17938 (2026-04-25); if the pattern repeats, next "
        "replacement around TAC 18294\u201319906."
    ) in lines
    assert (
        "Latest known TAC is 722 cycles after that replacement, past the fleet "
        "median but inside p90."
    ) in lines
    # No "1968.2000000000003"-style float noise anywhere in the rendered lines.
    assert not any("." in line and "2000000000003" in line for line in lines)


@pytest.mark.parametrize(
    ("position", "expected_suffix"),
    [
        ("before_p50", "before the fleet median."),
        ("between_p50_p90", "past the fleet median but inside p90."),
        (
            "past_p90",
            "beyond the fleet p90; replacement is overdue against the fleet pattern.",
        ),
    ],
)
def test_pma_lines_position_wording_by_boundary(position, expected_suffix):
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "supporting_interval": _aft_supporting_interval(),
        "projected_window": {
            "last_replacement_tac": 17938,
            "last_replacement_date": "2026-04-25",
            "tac_p50": 18294,
            "tac_p90": 19906,
            "cycles_since_last_replacement": 500,
            "position": position,
        },
    }
    lines = _pma_lines(pma)
    assert any(line.endswith(expected_suffix) for line in lines)


def test_pma_lines_projected_window_without_cycles_since_omits_position_line():
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "supporting_interval": _aft_supporting_interval(),
        "projected_window": {
            "last_replacement_tac": 17938,
            "last_replacement_date": "2026-04-25",
            "tac_p50": 18294,
            "tac_p90": 19906,
            "cycles_since_last_replacement": None,
            "position": None,
        },
    }
    lines = _pma_lines(pma)
    assert any(line.startswith("Projected window for this aircraft") for line in lines)
    assert not any(line.startswith("Latest known TAC is") for line in lines)


def test_pma_lines_no_prior_replacement_on_aircraft():
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "supporting_interval": _aft_supporting_interval(),
        "projected_window": None,
        "limitations": ["not_calibrated", "no_prior_replacement_on_aircraft"],
    }
    lines = _pma_lines(pma)
    assert (
        "No earlier replacement of this component on this aircraft is recorded, "
        "so no aircraft-specific window is given."
    ) in lines
    assert not any("Projected window" in line for line in lines)


def test_pma_lines_no_window_without_the_limitation_flag_stays_silent():
    """`projected_window_unavailable` (query failed) is a different signal
    from `no_prior_replacement_on_aircraft`; neither the window/position nor
    the "no earlier replacement" line is fabricated for it here (it still
    surfaces as a plain limitation string elsewhere)."""
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "supporting_interval": _aft_supporting_interval(),
        "projected_window": None,
        "limitations": ["projected_window_unavailable"],
    }
    lines = _pma_lines(pma)
    assert not any("Projected window" in line for line in lines)
    assert not any("No earlier replacement" in line for line in lines)


def test_pma_lines_stale_tac_with_projected_window_uses_window_wording():
    pma = {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "supporting_interval": _aft_supporting_interval(),
        "projected_window": {
            "last_replacement_tac": 17938,
            "last_replacement_date": "2026-04-25",
            "tac_p50": 18294,
            "tac_p90": 19906,
            "cycles_since_last_replacement": 722,
            "position": "between_p50_p90",
        },
        "current_tac": {
            "value": 18660,
            "source": "latest_closing_tac",
            "observed_at": "2026-08-20T22:30:00+00:00",
            "stale": True,
        },
    }
    lines = _pma_lines(pma)
    assert (
        "Current aircraft TAC is stale (last seen 2026-08-20T22:30:00+00:00); "
        "the window is not adjusted for cycles flown since."
    ) in lines
    assert not any(line.endswith("no due TAC is given.") for line in lines)


# --- error boundary: serialization must not escape the node (N5 regression) ---


class _Opaque:
    """Stands in for any branch value the evidence contract cannot serialize."""


async def _run_node(node, node_input):
    return await node(None, node_input)


def _node_input(request: EvidenceRequest) -> dict[str, Any]:
    return {"evidence_request": request.to_dict()}


@pytest.mark.parametrize(
    ("node", "patched", "expected_source"),
    [
        (bq_evidence, "_run_bq_branch", "bq_evidence"),
        (ipc_evidence, "_run_ipc_branch", "ipc_manual_retrieval"),
    ],
)
def test_branch_node_reports_error_when_its_result_cannot_be_serialized(
    monkeypatch, node, patched, expected_source
):
    """``to_dict`` used to run outside the try, so a value the contract could
    not coerce escaped the boundary and aborted the join instead of degrading
    that one branch. A live BigQuery ``datetime`` did exactly this.
    """

    async def _unserializable(request):
        return SourceResult(
            source=expected_source,
            status=SourceStatus.SUCCESS,
            records=({"thing": _Opaque()},),
        )

    monkeypatch.setattr(evidence_branches, patched, _unserializable)

    payload = asyncio.run(_run_node(node, _node_input(_request())))

    assert payload["status"] == SourceStatus.ERROR.value
    assert payload["source"] == expected_source
    json.dumps(payload)


def test_branch_node_reports_error_on_malformed_node_input():
    """Building the request also used to sit outside the boundary."""
    payload = asyncio.run(_run_node(bq_evidence, {"evidence_request": {}}))

    assert payload["status"] == SourceStatus.ERROR.value
    json.dumps(payload)
