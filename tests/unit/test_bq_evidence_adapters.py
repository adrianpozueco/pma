"""Unit tests for the N3 BigQuery evidence adapters.

Every test here uses a fake runner and/or a fake/local history provider -
no real BigQuery client, no network, no credentials. ``LocalHistoryProvider``
(``amos_data.retrieval``) is the same deterministic reference implementation
of the ``HistoryProvider`` protocol that ``BQHistoryProvider`` implements
against real BigQuery, so tests that exercise eligibility/exclusion/dedup
behavior through it are exercising the provider's real logic, not a mock of
it - only the corpus-unavailable/timeout/permission-error paths (which
``LocalHistoryProvider`` has no reason to ever hit) use a small hand-built
fake provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from amos_data.retrieval import LocalHistoryProvider
from pm_agent.sub_agents.bq_analytics import adapters
from pm_agent.workorders.evidence import (
    AircraftContext,
    EvidenceRequest,
    ExclusionSet,
    PartCandidate,
    SourceStatus,
)

AS_OF = "2026-06-01T00:00:00+00:00"


def _request(
    *,
    available_symptoms: tuple[str, ...] = (),
    part_candidates: tuple[PartCandidate, ...] = (),
    aircraft: AircraftContext | None = None,
    exclusions: ExclusionSet | None = None,
    analysis_as_of: str = AS_OF,
    question: str = "why did the pump fail",
) -> EvidenceRequest:
    return EvidenceRequest(
        question=question,
        input_mode="new_work_order",
        analysis_as_of=analysis_as_of,
        invocation_scope_id="scope-1",
        part_candidates=part_candidates,
        aircraft=aircraft or AircraftContext(),
        available_symptoms=available_symptoms,
        exclusions=exclusions or ExclusionSet(),
    )


# --------------------------------------------------------------------------
# Fake QueryRunner for get_part_history / get_part_coverage
# --------------------------------------------------------------------------


@dataclass
class _FakeOutcome:
    status: SourceStatus
    rows: tuple[dict[str, Any], ...] = ()
    truncated: bool = False
    error_detail: str | None = None


@dataclass
class FakeRunner:
    """Duck-types QueryRunner.run/table; never touches SQL text or a real
    client. ``responses`` maps sql_name -> canned _FakeOutcome; a sql_name
    the adapter calls without a registered response raises KeyError, which
    is used deliberately to prove short-circuiting (e.g. get_part_coverage
    must never query FAA rows after its AMOS query already failed)."""

    responses: dict[str, _FakeOutcome]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def table(self, name: str) -> str:
        return f"`fake.{name}`"

    def run(
        self,
        sql_name: str,
        parameters: list[tuple[str, str, Any]],
        *,
        limit: int,
        table_placeholders: dict[str, str] | None = None,
    ) -> _FakeOutcome:
        self.calls.append(
            {
                "sql_name": sql_name,
                "parameters": {name: value for name, _, value in parameters},
                "limit": limit,
                "table_placeholders": table_placeholders,
            }
        )
        return self.responses[sql_name]


# --------------------------------------------------------------------------
# get_part_history
# --------------------------------------------------------------------------


def test_get_part_history_blank_part_number_is_error_not_no_match() -> None:
    runner = FakeRunner(responses={})
    result = adapters.get_part_history(runner, _request())
    assert result.status == SourceStatus.ERROR
    assert result.error_detail == "error:blank_part_number"
    assert result.executed_parameters["part_number"] is None
    assert runner.calls == []  # never queries with no resolvable part number


def test_get_part_history_tags_own_aircraft_vs_comparable() -> None:
    rows = (
        {
            "workorder_id": "wo-1",
            "workorder_number": "WO-0001",
            "aircraft_full_registration": "D-OWNED",
            "part_on_number": "ABC-123-04",
        },
        {
            "workorder_id": "wo-2",
            "workorder_number": "WO-0002",
            "aircraft_registration": "D-OTHER",
            "part_on_number": "ABC-123-04",
        },
    )
    runner = FakeRunner(
        responses={"get_part_history": _FakeOutcome(SourceStatus.SUCCESS, rows)}
    )
    request = _request(aircraft=AircraftContext(aircraft_id="D-OWNED"))
    result = adapters.get_part_history(runner, request, part_number="ABC-123-04")
    assert result.status == SourceStatus.SUCCESS
    scopes = {r["workorder_id"]: r["history_scope"] for r in result.records}
    assert scopes == {"wo-1": "own_aircraft", "wo-2": "comparable_amos"}
    assert result.counts == {"history_row_count": 2, "distinct_workorder_count": 2}


def test_get_part_history_applies_own_workorder_exclusion_client_side() -> None:
    rows = (
        {"workorder_id": "wo-keep", "workorder_number": "WO-KEEP", "part_on_number": "P1"},
        {"workorder_id": "wo-drop", "workorder_number": "WO-DROP", "part_on_number": "P1"},
    )
    runner = FakeRunner(
        responses={"get_part_history": _FakeOutcome(SourceStatus.SUCCESS, rows)}
    )
    request = _request(exclusions=ExclusionSet(workorder_ids=frozenset({"wo-drop"})))
    result = adapters.get_part_history(runner, request, part_number="P1")
    assert [r["workorder_id"] for r in result.records] == ["wo-keep"]
    assert result.counts["history_row_count"] == 1


def test_get_part_history_zero_rows_is_no_match_not_error() -> None:
    runner = FakeRunner(
        responses={"get_part_history": _FakeOutcome(SourceStatus.NO_MATCH, ())}
    )
    result = adapters.get_part_history(runner, _request(), part_number="P1")
    assert result.status == SourceStatus.NO_MATCH


def test_get_part_history_failure_never_surfaces_as_no_match() -> None:
    runner = FakeRunner(
        responses={
            "get_part_history": _FakeOutcome(
                SourceStatus.PERMISSION_DENIED, (), error_detail="permission_denied:Forbidden"
            )
        }
    )
    result = adapters.get_part_history(runner, _request(), part_number="P1")
    assert result.status == SourceStatus.PERMISSION_DENIED
    assert result.error_detail == "permission_denied:Forbidden"
    assert result.records == ()


def test_get_part_history_normalizes_match_param_but_keeps_display_formatting() -> None:
    rows = ({"workorder_id": "wo-1", "part_on_number": "abc-123-04"},)
    runner = FakeRunner(
        responses={"get_part_history": _FakeOutcome(SourceStatus.SUCCESS, rows)}
    )
    result = adapters.get_part_history(runner, _request(), part_number="ABC-123-04")
    assert result.executed_parameters["part_number"] == "ABC12304"
    assert runner.calls[0]["parameters"]["part_number"] == "ABC12304"
    # Row's own field is passed through untouched - display formatting retained.
    assert result.records[0]["part_on_number"] == "abc-123-04"


def test_get_part_history_uses_resolved_candidate_when_no_override() -> None:
    runner = FakeRunner(
        responses={"get_part_history": _FakeOutcome(SourceStatus.NO_MATCH, ())}
    )
    candidates = (
        PartCandidate(part_key="XYZ1", part_number="XYZ-1", resolution_status="ambiguous"),
        PartCandidate(part_key="ABC2", part_number="ABC-2", resolution_status="resolved"),
    )
    request = _request(part_candidates=candidates)
    result = adapters.get_part_history(runner, request)
    assert result.executed_parameters["part_number"] == "ABC2"


def test_get_part_history_binds_before_ts_and_family_position() -> None:
    runner = FakeRunner(
        responses={"get_part_history": _FakeOutcome(SourceStatus.NO_MATCH, ())}
    )
    request = _request(aircraft=AircraftContext(family="A320", position="#2"))
    adapters.get_part_history(runner, request, part_number="P1")
    bound = runner.calls[0]["parameters"]
    assert bound["before_ts"] == AS_OF
    assert bound["family"] == "A320"
    assert bound["position"] == "#2"
    assert runner.calls[0]["table_placeholders"] == {"workorders_table": "`fake.wo_workorders`"}


# --------------------------------------------------------------------------
# get_part_coverage
# --------------------------------------------------------------------------


def test_get_part_coverage_blank_part_number_is_error() -> None:
    runner = FakeRunner(responses={})
    result = adapters.get_part_coverage(runner, _request(), range_start="2026-01-01T00:00:00+00:00")
    assert result.status == SourceStatus.ERROR
    assert result.error_detail == "error:blank_part_number"
    assert runner.calls == []


def test_get_part_coverage_reports_three_independent_counts() -> None:
    changes = (
        {"workorder_id": "wo-1", "component_change_uuid": "c1"},
        {"workorder_id": "wo-1", "component_change_uuid": "c2"},
        {"workorder_id": "wo-2", "component_change_uuid": "c3"},
    )
    faa_rows = (
        {"report_id": "r1"},
        {"report_id": "r2"},
    )
    runner = FakeRunner(
        responses={
            "get_part_coverage_changes": _FakeOutcome(SourceStatus.SUCCESS, changes),
            "get_part_coverage_faa": _FakeOutcome(SourceStatus.SUCCESS, faa_rows),
        }
    )
    result = adapters.get_part_coverage(
        runner, _request(), range_start="2026-01-01T00:00:00+00:00", part_number="P1"
    )
    assert result.status == SourceStatus.SUCCESS
    assert result.counts == {
        "workorder_count": 2,
        "component_change_count": 3,
        "faa_report_count": 2,
    }
    # Never merged into one number.
    assert "total" not in result.counts
    assert len({result.counts["workorder_count"], result.counts["component_change_count"]}) == 2


def test_get_part_coverage_excludes_own_workorder_from_amos_side_only() -> None:
    changes = (
        {"workorder_id": "wo-own", "component_change_uuid": "c1"},
        {"workorder_id": "wo-other", "component_change_uuid": "c2"},
    )
    faa_rows = ({"report_id": "r1"},)
    runner = FakeRunner(
        responses={
            "get_part_coverage_changes": _FakeOutcome(SourceStatus.SUCCESS, changes),
            "get_part_coverage_faa": _FakeOutcome(SourceStatus.SUCCESS, faa_rows),
        }
    )
    request = _request(exclusions=ExclusionSet(workorder_ids=frozenset({"wo-own"})))
    result = adapters.get_part_coverage(
        runner, request, range_start="2026-01-01T00:00:00+00:00", part_number="P1"
    )
    assert result.counts["workorder_count"] == 1
    assert result.counts["component_change_count"] == 1
    # FAA rows carry no work-order linkage - exclusion never touches them.
    assert result.counts["faa_report_count"] == 1


def test_get_part_coverage_short_circuits_before_faa_query_on_amos_failure() -> None:
    runner = FakeRunner(
        responses={
            "get_part_coverage_changes": _FakeOutcome(
                SourceStatus.TIMEOUT, (), error_detail="timeout:TimeoutError"
            )
            # Deliberately no "get_part_coverage_faa" entry: a KeyError here
            # would mean the adapter queried FAA anyway after the AMOS side
            # already failed.
        }
    )
    result = adapters.get_part_coverage(
        runner, _request(), range_start="2026-01-01T00:00:00+00:00", part_number="P1"
    )
    assert result.status == SourceStatus.TIMEOUT
    assert result.error_detail == "timeout:TimeoutError"
    assert [c["sql_name"] for c in runner.calls] == ["get_part_coverage_changes"]


def test_get_part_coverage_propagates_faa_failure_after_amos_success() -> None:
    changes = ({"workorder_id": "wo-1", "component_change_uuid": "c1"},)
    runner = FakeRunner(
        responses={
            "get_part_coverage_changes": _FakeOutcome(SourceStatus.SUCCESS, changes),
            "get_part_coverage_faa": _FakeOutcome(
                SourceStatus.UNAVAILABLE, (), error_detail="unavailable:ServiceUnavailable"
            ),
        }
    )
    result = adapters.get_part_coverage(
        runner, _request(), range_start="2026-01-01T00:00:00+00:00", part_number="P1"
    )
    assert result.status == SourceStatus.UNAVAILABLE
    assert result.error_detail == "unavailable:ServiceUnavailable"
    assert result.counts == {}


def test_get_part_coverage_zero_rows_both_sides_is_no_match() -> None:
    runner = FakeRunner(
        responses={
            "get_part_coverage_changes": _FakeOutcome(SourceStatus.NO_MATCH, ()),
            "get_part_coverage_faa": _FakeOutcome(SourceStatus.NO_MATCH, ()),
        }
    )
    result = adapters.get_part_coverage(
        runner, _request(), range_start="2026-01-01T00:00:00+00:00", part_number="P1"
    )
    assert result.status == SourceStatus.NO_MATCH
    assert result.counts == {"workorder_count": 0, "component_change_count": 0, "faa_report_count": 0}


def test_get_part_coverage_range_end_defaults_to_analysis_as_of() -> None:
    runner = FakeRunner(
        responses={
            "get_part_coverage_changes": _FakeOutcome(SourceStatus.NO_MATCH, ()),
            "get_part_coverage_faa": _FakeOutcome(SourceStatus.NO_MATCH, ()),
        }
    )
    result = adapters.get_part_coverage(
        runner, _request(), range_start="2026-01-01T00:00:00+00:00", part_number="P1"
    )
    assert result.executed_parameters["range_end"] == AS_OF
    assert runner.calls[0]["parameters"]["range_end"] == AS_OF


# --------------------------------------------------------------------------
# find_historical_symptoms / find_faa_reports - shared BQHistoryProvider reuse
# --------------------------------------------------------------------------


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


def test_find_historical_symptoms_keeps_only_amos_namespace() -> None:
    docs = [
        _doc(),
        _doc(
            document_id="doc-2",
            record_id="rec-2",
            source_namespace="faa_sdr",
            workorder_id=None,
            workorder_number=None,
            source_span="faa:rec-2",
        ),
    ]
    provider = LocalHistoryProvider(docs)
    request = _request(available_symptoms=("pump seal leak",))
    result = adapters.find_historical_symptoms(
        provider, request, part_number="ABC-123-04", method="keyword"
    )
    assert result.status == SourceStatus.SUCCESS
    assert [r["source_namespace"] for r in result.records] == ["amos"]
    assert result.counts == {"symptom_case_rows": 1}


def test_find_faa_reports_keeps_only_faa_namespace_and_tags_public_report() -> None:
    docs = [
        _doc(),
        _doc(
            document_id="doc-2",
            record_id="rec-2",
            source_namespace="faa_sdr",
            workorder_id=None,
            workorder_number=None,
            source_span="faa:rec-2",
        ),
    ]
    provider = LocalHistoryProvider(docs)
    request = _request(available_symptoms=("pump seal leak",))
    result = adapters.find_faa_reports(
        provider, request, part_number="ABC-123-04", method="keyword"
    )
    assert result.status == SourceStatus.SUCCESS
    assert len(result.records) == 1
    assert result.records[0]["source_namespace"] == "faa_sdr"
    assert result.records[0]["history_scope"] == "public_report"
    assert result.counts == {"faa_report_rows": 1}


def test_find_historical_symptoms_applies_own_workorder_exclusion() -> None:
    docs = [
        _doc(workorder_id="wo-drop", workorder_number="WO-DROP"),
        _doc(document_id="doc-2", record_id="rec-2", workorder_id="wo-keep", workorder_number="WO-KEEP"),
    ]
    provider = LocalHistoryProvider(docs)
    request = _request(
        available_symptoms=("pump seal leak",),
        exclusions=ExclusionSet(workorder_ids=frozenset({"wo-drop"})),
    )
    result = adapters.find_historical_symptoms(
        provider, request, part_number="ABC-123-04", method="keyword"
    )
    assert [r["workorder_id"] for r in result.records] == ["wo-keep"]


def test_find_historical_symptoms_applies_text_hash_exclusion() -> None:
    docs = [
        _doc(text_hash="drop-me"),
        _doc(document_id="doc-2", record_id="rec-2", text_hash="keep-me"),
    ]
    provider = LocalHistoryProvider(docs)
    request = _request(
        available_symptoms=("pump seal leak",),
        exclusions=ExclusionSet(text_hashes=frozenset({"drop-me"})),
    )
    result = adapters.find_historical_symptoms(
        provider, request, part_number="ABC-123-04", method="keyword"
    )
    assert len(result.records) == 1
    assert result.records[0]["record_id"] != "rec-1" or result.records[0]["document_id"] != "doc-1"


def test_find_historical_symptoms_excludes_future_records() -> None:
    docs = [
        _doc(available_at="2027-01-01T00:00:00+00:00"),  # after AS_OF -> excluded
        _doc(document_id="doc-2", record_id="rec-2", available_at="2026-01-01T00:00:00+00:00"),
    ]
    provider = LocalHistoryProvider(docs)
    request = _request(available_symptoms=("pump seal leak",))
    result = adapters.find_historical_symptoms(
        provider, request, part_number="ABC-123-04", method="keyword"
    )
    assert len(result.records) == 1
    assert result.records[0]["document_id"] == "doc-2"


def test_find_faa_reports_excludes_future_records_same_as_amos() -> None:
    docs = [
        _doc(
            document_id="doc-2",
            record_id="rec-2",
            source_namespace="faa_sdr",
            workorder_id=None,
            workorder_number=None,
            available_at="2027-01-01T00:00:00+00:00",
        ),
    ]
    provider = LocalHistoryProvider(docs)
    request = _request(available_symptoms=("pump seal leak",))
    result = adapters.find_faa_reports(
        provider, request, part_number="ABC-123-04", method="keyword"
    )
    assert result.status == SourceStatus.NO_MATCH
    assert result.records == ()


def test_find_historical_symptoms_case_level_dedup_via_provider() -> None:
    # Two raw documents for the same (source_namespace, record_id) - the
    # provider's own case_id keying collapses them to one case, the same
    # dedup BQHistoryProvider's SQL performs with QUALIFY ROW_NUMBER().
    docs = [
        _doc(document_id="doc-1a", raw_text="pump seal leak reported"),
        _doc(document_id="doc-1b", raw_text="pump seal leak reported again"),
    ]
    provider = LocalHistoryProvider(docs)
    request = _request(available_symptoms=("pump seal leak",))
    result = adapters.find_historical_symptoms(
        provider, request, part_number="ABC-123-04", method="keyword"
    )
    assert len(result.records) == 1


class FakeErrorProvider:
    """Models BQHistoryProvider's "error" status path (unconfigured corpus,
    timeout, permission failure) without a real BigQuery client."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        self.calls = 0

    def search(self, query: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "cases": [],
            "status": "error",
            "errors": self.errors,
            "truncation": {"truncated": False},
            "applied_filters": {},
            "versions": {},
        }


def test_find_historical_symptoms_unconfigured_corpus_is_unavailable_named() -> None:
    provider = FakeErrorProvider(["reference_corpus_unconfigured"])
    result = adapters.find_historical_symptoms(provider, _request())
    assert result.status == SourceStatus.UNAVAILABLE
    assert result.error_detail == "unavailable:reference_corpus_unconfigured"
    assert provider.calls == 1  # no silent retry/fallback


def test_find_historical_symptoms_missing_embedding_provider_is_unavailable() -> None:
    provider = FakeErrorProvider(["vector_embedding_provider_unconfigured"])
    result = adapters.find_historical_symptoms(provider, _request(), method="vector")
    assert result.status == SourceStatus.UNAVAILABLE
    assert result.error_detail == "unavailable:vector_embedding_provider_unconfigured"


def test_find_historical_symptoms_provider_timeout_maps_to_timeout() -> None:
    provider = FakeErrorProvider(["query_timeout"])
    result = adapters.find_historical_symptoms(provider, _request())
    assert result.status == SourceStatus.TIMEOUT


def test_find_historical_symptoms_provider_forbidden_maps_to_permission_denied() -> None:
    provider = FakeErrorProvider(["query_error:Forbidden"])
    result = adapters.find_historical_symptoms(provider, _request())
    assert result.status == SourceStatus.PERMISSION_DENIED


def test_find_faa_reports_unconfigured_corpus_is_unavailable() -> None:
    provider = FakeErrorProvider(["reference_corpus_unconfigured"])
    result = adapters.find_faa_reports(provider, _request())
    assert result.status == SourceStatus.UNAVAILABLE
    assert result.error_detail == "unavailable:reference_corpus_unconfigured"


def test_find_historical_symptoms_unknown_provider_error_is_error_not_no_match() -> None:
    provider = FakeErrorProvider(["something_unmodeled"])
    result = adapters.find_historical_symptoms(provider, _request())
    assert result.status == SourceStatus.ERROR
    assert result.error_detail == "error:something_unmodeled"


def test_find_historical_symptoms_falls_back_to_question_when_no_symptoms_or_part() -> None:
    docs = [_doc(raw_text="totally unrelated text with no overlap")]
    provider = LocalHistoryProvider(docs)
    request = _request(question="completely different wording than any document")
    result = adapters.find_historical_symptoms(provider, request, method="keyword")
    # No crash on blank-symptoms validation; a genuine no-overlap search is NO_MATCH.
    assert result.status == SourceStatus.NO_MATCH
