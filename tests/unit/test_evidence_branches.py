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
