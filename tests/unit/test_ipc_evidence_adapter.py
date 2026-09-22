"""Unit tests for the IPC evidence adapter (N4).

Every test injects a fake ``search`` callable - never network access or real
credentials - built from real ``google.genai.types`` grounding objects so the
shapes under test match what the installed SDK actually returns.
"""

from __future__ import annotations

import pytest
from google.genai import types

from pm_agent.sub_agents.ipc_manual_retrieval.agent import ask_vertex_retrieval
from pm_agent.sub_agents.ipc_manual_retrieval.evidence import (
    SOURCE_NAME,
    IpcPermissionDenied,
    IpcTimeout,
    IpcUnavailable,
    run_ipc_evidence,
)
from pm_agent.workorders.evidence import (
    AircraftContext,
    EvidenceRequest,
    PartCandidate,
    SourceStatus,
)

_CREDENTIAL_LIKE_SUBSTRINGS = (
    "bearer",
    "token",
    "api key",
    "apikey",
    "secret",
    "password",
    "credential",
    "authorization",
)


def _request(**overrides) -> EvidenceRequest:
    values = {
        "question": "Is the fuel nozzle in the IPC catalogue?",
        "input_mode": "new_work_order",
        "analysis_as_of": "2026-09-01T10:00:00+00:00",
        "invocation_scope_id": "turn-1",
        "part_candidates": (
            PartCandidate(part_key="2085M31G03", part_number="2085M31G03"),
        ),
    }
    values.update(overrides)
    return EvidenceRequest(**values)


def _retrieved_context(**overrides) -> types.GroundingChunkRetrievedContext:
    values = {
        "document_name": (
            "projects/p/locations/global/collections/default_collection/"
            "dataStores/ipc-part-numbers_1789998929768/branches/0/documents/doc-1"
        ),
        "title": "Chapter 73-11, 737-800, AIPC, D638A001-RYR-0137",
        "text": "2085M31G03 ... FUEL NOZZLE ASSEMBLY",
        "uri": "https://example.invalid/ipc/doc-1",
    }
    values.update(overrides)
    return types.GroundingChunkRetrievedContext(**values)


def _metadata(*chunks: types.GroundingChunkRetrievedContext) -> types.GroundingMetadata:
    return types.GroundingMetadata(
        grounding_chunks=[
            types.GroundingChunk(retrieved_context=chunk) for chunk in chunks
        ]
    )


def _fake_search(outcome, calls: list[str] | None = None):
    async def _search(query: str):
        if calls is not None:
            calls.append(query)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _search


@pytest.mark.asyncio
async def test_full_grounding_metadata_is_preserved_verbatim():
    chunk = _retrieved_context(page_number=42)
    result = await run_ipc_evidence(
        _request(), search=_fake_search(_metadata(chunk))
    )

    assert result.source == SOURCE_NAME
    assert result.status == SourceStatus.SUCCESS
    assert len(result.records) == 1
    record = result.records[0]
    assert record["document_name"] == chunk.document_name
    assert record["title"] == chunk.title
    assert record["uri"] == chunk.uri
    assert record["text"] == chunk.text
    assert record["page_number"] == 42
    assert result.citations[0]["document_name"] == chunk.document_name
    assert result.source_ids == (chunk.document_name,)


@pytest.mark.asyncio
async def test_missing_page_yields_absent_key_not_a_fabricated_value():
    chunk = _retrieved_context(page_number=None)
    result = await run_ipc_evidence(
        _request(), search=_fake_search(_metadata(chunk))
    )

    record = result.records[0]
    citation = result.citations[0]
    assert "page_number" not in record
    assert "page_number" not in citation
    # No field named "revision" exists on the installed SDK's retrieved
    # context at all, so it must never appear anywhere in the output.
    assert "revision" not in record
    assert "revision" not in citation


@pytest.mark.asyncio
async def test_zero_hits_is_no_match_not_unavailable_or_error():
    result = await run_ipc_evidence(_request(), search=_fake_search(_metadata()))

    assert result.status == SourceStatus.NO_MATCH
    assert result.records == ()
    assert result.citations == ()
    assert result.counts["queries_executed"] == 1
    assert result.counts["chunks_returned"] == 0


@pytest.mark.asyncio
async def test_no_resolved_parts_is_no_match_without_calling_search():
    calls: list[str] = []
    result = await run_ipc_evidence(
        _request(part_candidates=()), search=_fake_search(_metadata(), calls)
    )

    assert result.status == SourceStatus.NO_MATCH
    assert calls == []
    assert result.counts["queries_executed"] == 0


@pytest.mark.asyncio
async def test_auth_failure_is_permission_denied_not_no_match():
    result = await run_ipc_evidence(
        _request(),
        search=_fake_search(IpcPermissionDenied("denied")),
    )

    assert result.status == SourceStatus.PERMISSION_DENIED
    assert result.records == ()


@pytest.mark.asyncio
async def test_timeout_is_timeout_not_no_match():
    result = await run_ipc_evidence(
        _request(), search=_fake_search(IpcTimeout("slow"))
    )

    assert result.status == SourceStatus.TIMEOUT
    assert result.records == ()


@pytest.mark.asyncio
async def test_unreachable_datastore_is_unavailable_not_no_match():
    result = await run_ipc_evidence(
        _request(), search=_fake_search(IpcUnavailable("down"))
    )

    assert result.status == SourceStatus.UNAVAILABLE
    assert result.records == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [IpcPermissionDenied("denied"), IpcTimeout("slow"), IpcUnavailable("down")],
)
async def test_error_detail_carries_no_credential_like_content(outcome):
    result = await run_ipc_evidence(_request(), search=_fake_search(outcome))

    assert result.error_detail is not None
    lowered = result.error_detail.lower()
    for needle in _CREDENTIAL_LIKE_SUBSTRINGS:
        assert needle not in lowered


@pytest.mark.asyncio
async def test_family_level_hit_is_distinguishable_from_aircraft_context_hit():
    chunk = _retrieved_context()

    without_aircraft = await run_ipc_evidence(
        _request(), search=_fake_search(_metadata(chunk))
    )
    with_aircraft = await run_ipc_evidence(
        _request(aircraft=AircraftContext(aircraft_id="N123AB", family="B737-8")),
        search=_fake_search(_metadata(chunk)),
    )

    assert without_aircraft.records[0]["catalog_scope"] == "family_catalogue"
    assert "aircraft_applicability" not in without_aircraft.records[0]

    assert with_aircraft.records[0]["catalog_scope"] == "family_catalogue"
    assert (
        with_aircraft.records[0]["aircraft_applicability"] == "unconfirmed_by_catalog"
    )


@pytest.mark.asyncio
async def test_multiple_resolved_parts_are_deduped_and_each_queried_once():
    calls: list[str] = []
    request = _request(
        part_candidates=(
            PartCandidate(part_key="2085M31G03", part_number="2085M31G03"),
            PartCandidate(part_key="2085M31G03", part_number="2085M31G03"),
            PartCandidate(part_key="62197301001", part_number="62197-301-001"),
        )
    )
    result = await run_ipc_evidence(
        request, search=_fake_search(_metadata(_retrieved_context()), calls)
    )

    assert calls == ["2085M31G03", "62197-301-001"]
    assert result.status == SourceStatus.SUCCESS
    assert result.counts["queries_executed"] == 2


@pytest.mark.asyncio
async def test_truncated_when_chunk_count_reaches_the_configured_limit():
    chunks = [
        _retrieved_context(document_name=f"doc-{i}", title=f"t{i}", uri=None)
        for i in range(ask_vertex_retrieval.max_results)
    ]
    result = await run_ipc_evidence(
        _request(), search=_fake_search(_metadata(*chunks))
    )

    assert result.truncated is True
    assert result.limit == ask_vertex_retrieval.max_results


@pytest.mark.asyncio
async def test_a_failed_query_stops_before_querying_later_parts():
    calls: list[str] = []

    async def _search(query: str):
        calls.append(query)
        raise IpcTimeout("slow")

    request = _request(
        part_candidates=(
            PartCandidate(part_key="2085M31G03", part_number="2085M31G03"),
            PartCandidate(part_key="62197301001", part_number="62197-301-001"),
        )
    )
    result = await run_ipc_evidence(request, search=_search)

    assert result.status == SourceStatus.TIMEOUT
    assert calls == ["2085M31G03"]
    assert result.executed_parameters["failed_query"] == "2085M31G03"
