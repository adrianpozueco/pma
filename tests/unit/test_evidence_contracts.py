import json
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from pm_agent.workorders.evidence import (
    AircraftContext,
    ArtifactProvenance,
    CounterValue,
    EvidenceCounters,
    EvidenceRequest,
    ExclusionSet,
    PartCandidate,
    SourceResult,
    SourceStatus,
    build_join_payload,
    merge_source_results,
)
from pm_agent.workorders.service import WorkOrderAnalysisError


def _request(**overrides) -> EvidenceRequest:
    values = {
        "question": "Check the fuel nozzle for repeat failures.",
        "input_mode": "new_work_order",
        "analysis_as_of": "2026-09-01T10:00:00+00:00",
        "invocation_scope_id": "turn-1",
        "artifact": ArtifactProvenance(filename="wo.xml", version=0, sha256="a" * 64),
        "selected_wo_id": "105177647",
        "part_candidates": (
            PartCandidate(part_key="2085M31G03", part_number="2085M31G03"),
        ),
        "aircraft": AircraftContext(aircraft_id="N123AB", family="B737-8", position="2"),
        "available_symptoms": ("nozzle leak reported",),
        "counters": EvidenceCounters(
            issue_tac=CounterValue(value=100, source="xml_issue", observed_at="2026-08-01T00:00:00Z", status="valid"),
            closing_tac=CounterValue(value=None, source="xml_closing", observed_at=None, status="missing_or_invalid"),
            supplied_current_tac=CounterValue(value=None, source=None, observed_at=None, status="not_supplied"),
        ),
        "exclusions": ExclusionSet(workorder_ids=frozenset({"105177647"}), text_hashes=frozenset({"deadbeef"})),
    }
    values.update(overrides)
    return EvidenceRequest(**values)


def test_evidence_request_round_trips_through_plain_dict_and_json():
    request = _request()
    restored = EvidenceRequest.from_dict(json.loads(json.dumps(request.to_dict())))
    assert restored == request


def test_artifact_version_zero_is_preserved_not_treated_as_missing():
    request = _request(artifact=ArtifactProvenance(filename="wo.xml", version=0, sha256="a" * 64))
    assert request.artifact.version == 0
    round_tripped = EvidenceRequest.from_dict(request.to_dict())
    assert round_tripped.artifact.version == 0
    assert round_tripped.artifact is not None


def test_evidence_request_without_artifact_is_valid():
    # A combined maintenance question with no XML attachment is a real case.
    request = _request(artifact=None)
    assert request.artifact is None
    assert EvidenceRequest.from_dict(request.to_dict()).artifact is None


def test_missing_current_tac_is_an_allowed_state_not_an_error():
    request = _request()  # supplied_current_tac left at the default.
    assert request.counters.supplied_current_tac.status == "not_supplied"
    assert request.counters.supplied_current_tac.value is None
    # Building the request at all must not raise for a missing current TAC.
    EvidenceRequest(
        question="Any repeat nozzle issues?",
        input_mode="new_work_order",
        analysis_as_of="2026-09-01T10:00:00+00:00",
        invocation_scope_id="turn-2",
    )


def test_counter_provenance_stays_distinct():
    request = _request()
    counters = request.counters
    assert counters.issue_tac.value == 100
    assert counters.closing_tac.value is None
    assert counters.supplied_current_tac.value is None
    # Closing TAC must never be read as the current counter.
    assert counters.closing_tac.status != counters.issue_tac.status
    assert counters.closing_tac is not counters.supplied_current_tac


def test_exclusions_are_preserved_through_round_trip():
    request = _request()
    restored = EvidenceRequest.from_dict(request.to_dict())
    assert restored.exclusions.workorder_ids == frozenset({"105177647"})
    assert restored.exclusions.text_hashes == frozenset({"deadbeef"})
    assert restored.exclusions.record_ids == frozenset()


@pytest.mark.parametrize(
    "status",
    [
        SourceStatus.SUCCESS,
        SourceStatus.NO_MATCH,
        SourceStatus.UNAVAILABLE,
        SourceStatus.PERMISSION_DENIED,
        SourceStatus.TIMEOUT,
        SourceStatus.ERROR,
    ],
)
def test_every_source_status_round_trips_distinctly(status):
    result = SourceResult(source="bigquery.get_workorder", status=status)
    restored = SourceResult.from_dict(json.loads(json.dumps(result.to_dict())))
    assert restored.status is status


def test_no_match_is_not_unavailable_and_not_timeout():
    no_match = SourceResult(source="bigquery.get_part_history", status=SourceStatus.NO_MATCH, records=())
    unavailable = SourceResult(source="ipc_manual_retrieval", status=SourceStatus.UNAVAILABLE)
    timeout = SourceResult(source="bigquery.find_faa_reports", status=SourceStatus.TIMEOUT)
    assert len({no_match.status, unavailable.status, timeout.status}) == 3
    assert no_match.status != SourceStatus.UNAVAILABLE
    assert no_match.status != SourceStatus.TIMEOUT


def test_source_result_round_trip_preserves_records_counts_and_error_detail():
    result = SourceResult(
        source="bigquery.get_component_changes",
        status=SourceStatus.SUCCESS,
        records=({"part_off_number": "2085M31G03", "serial_off": "S1"},),
        source_ids=("job-123",),
        citations=({"table": "wo_workorders"},),
        executed_parameters={"part_number": "2085M31G03", "as_of": "2026-09-01T10:00:00+00:00"},
        counts={"workorders": 1, "component_changes": 4},
        truncated=False,
        limit=50,
        error_detail=None,
    )
    restored = SourceResult.from_dict(json.loads(json.dumps(result.to_dict())))
    assert restored == result
    # Work orders and component changes must stay separately countable units.
    assert restored.counts["workorders"] != restored.counts["component_changes"]


def test_error_detail_never_needs_credentials_and_stays_a_plain_string():
    result = SourceResult(
        source="bigquery.get_workorder",
        status=SourceStatus.ERROR,
        error_detail="BigQuery query failed: deadline exceeded",
    )
    assert "token" not in result.error_detail.lower()
    assert isinstance(SourceResult.from_dict(result.to_dict()).error_detail, str)


def test_merge_source_results_keeps_branches_disjoint():
    bq_result = SourceResult(source="bigquery.get_workorder", status=SourceStatus.SUCCESS, records=({"wo": 1},))
    ipc_result = SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)
    merged = merge_source_results([bq_result, ipc_result])
    assert set(merged) == {"bigquery.get_workorder", "ipc_manual_retrieval"}
    assert merged["bigquery.get_workorder"]["status"] == "success"
    assert merged["ipc_manual_retrieval"]["status"] == "no_match"
    json.dumps(merged)  # Must be JSON-serializable as-is.


def test_merge_source_results_rejects_duplicate_source_key():
    first = SourceResult(source="bigquery.get_workorder", status=SourceStatus.SUCCESS)
    second = SourceResult(source="bigquery.get_workorder", status=SourceStatus.ERROR)
    with pytest.raises(ValueError):
        merge_source_results([first, second])


def test_build_join_payload_combines_request_and_sources_as_json():
    request = _request()
    results = [
        SourceResult(source="bigquery.get_workorder", status=SourceStatus.SUCCESS, records=({"wo": 1},)),
        SourceResult(source="ipc_manual_retrieval", status=SourceStatus.UNAVAILABLE),
    ]
    payload = build_join_payload(request, results)
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["request"]["invocation_scope_id"] == "turn-1"
    assert decoded["sources"]["bigquery.get_workorder"]["status"] == "success"
    assert decoded["sources"]["ipc_manual_retrieval"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "field_name,value,code",
    [
        ("question", "   ", "invalid_evidence_request"),
        ("input_mode", "not_a_mode", "invalid_mode"),
        ("invocation_scope_id", "", "invalid_evidence_request"),
    ],
)
def test_evidence_request_rejects_invalid_fields(field_name, value, code):
    with pytest.raises(WorkOrderAnalysisError) as error:
        _request(**{field_name: value})
    assert error.value.code == code


def test_analysis_as_of_requires_timezone_like_analysis_input():
    with pytest.raises(WorkOrderAnalysisError) as error:
        _request(analysis_as_of="2026-09-01T10:00:00")
    assert error.value.code == "timezone_required"


def test_artifact_provenance_rejects_negative_version():
    with pytest.raises(WorkOrderAnalysisError) as error:
        ArtifactProvenance(filename="wo.xml", version=-1, sha256="a" * 64)
    assert error.value.code == "invalid_artifact_reference"


def test_source_status_accepts_plain_string_value():
    result = SourceResult(source="bigquery.get_workorder", status="no_match")
    assert result.status is SourceStatus.NO_MATCH


def test_source_result_coerces_bigquery_scalar_types() -> None:
    """Live BigQuery rows carry datetime/date/time/Decimal/bytes values.

    The mocked-client tests only ever produced strings, so this covers the
    real column types the predefined SQL selects (TIMESTAMP, DATE, TIME,
    NUMERIC, BYTES) reaching ``to_dict`` unconverted.
    """
    result = SourceResult(
        source="bigquery.get_workorder_actions",
        status=SourceStatus.SUCCESS,
        records=(
            {
                "performed_at": datetime(2026, 9, 1, 9, 15, tzinfo=UTC),
                "reported_on": date(2026, 9, 1),
                "time_of_day": time(9, 15),
                "quantity": Decimal("2.5"),
                "source_row_hash": b"\x00\xff",
            },
        ),
    )

    payload = result.to_dict()
    json.dumps(payload)  # must not raise
    record = payload["records"][0]

    assert record["performed_at"] == "2026-09-01T09:15:00+00:00"
    assert record["reported_on"] == "2026-09-01"
    assert record["time_of_day"] == "09:15:00"
    assert record["quantity"] == "2.5"
    assert record["source_row_hash"] == "00ff"


def test_source_result_still_rejects_genuinely_unknown_types() -> None:
    """Unknown objects must not be silently stringified into evidence."""

    class Opaque:
        pass

    with pytest.raises(TypeError):
        SourceResult(
            source="bigquery.get_workorder",
            status=SourceStatus.SUCCESS,
            records=({"thing": Opaque()},),
        ).to_dict()
