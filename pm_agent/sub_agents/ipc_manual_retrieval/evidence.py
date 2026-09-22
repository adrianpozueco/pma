# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IPC manual evidence adapter (N4).

Wraps the existing IPC Illustrated Parts Catalogue Vertex AI Search datastore
(``pm_agent/sub_agents/ipc_manual_retrieval/agent.py``) as one evidence branch
that takes the shared :class:`~pm_agent.workorders.evidence.EvidenceRequest`
and returns exactly one
:class:`~pm_agent.workorders.evidence.SourceResult` with
``source="ipc_manual_retrieval"``.

``VertexAiSearchTool`` is *built-in* Gemini grounding: the datastore is
searched by the model as part of one ``generate_content`` call, and the real,
structured evidence comes back as ``candidate.grounding_metadata`` (see
``google.genai.types.GroundingMetadata`` /
``GroundingChunkRetrievedContext``). This module reads only that structured
metadata. It deliberately does not reuse the specialist agent's
``INSTRUCTION`` "Citations" prompt: that heading is prose the *model*
reconstructs from a retrieved title, not a field the retrieval backend
returns, so it cannot stand in for an exact document/page/revision. Where the
installed SDK's ``GroundingChunkRetrievedContext`` does not carry a field
(there is no "revision" field at all, and ``page_number`` is documented as
"not supported in Vertex AI"), this adapter leaves the corresponding key out
of its records/citations rather than filling it from the model's prose or
guessing.

What this deliberately does not do, and why:

* It does not call the ``ipc_manual_retrieval_agent`` (chat) node or run it
  through an ADK ``Runner``/session. That agent is a multi-turn chat
  specialist whose final answer is model prose; this adapter needs one
  stateless, structured retrieval per resolved part, so it drives the same
  ``VertexAiSearchTool`` configuration directly through a single
  ``generate_content`` call instead.
* It does not reuse the specialist's persona/citation ``INSTRUCTION`` text.
  That prompt exists to make the chat agent format a human-readable answer;
  reusing it here would tempt this adapter into treating the model's
  generated "Citations:" section as evidence, which the plan explicitly
  forbids.
* It never claims a hit confirms the part is installed on a specific
  aircraft. The datastore only covers a small set of 737-800/737-8200 IPC
  chapters at the family level (see ``agent.py``'s ``INSTRUCTION``), so every
  record/citation is tagged ``catalog_scope="family_catalogue"`` and, only
  when the request names a specific aircraft, an explicit
  ``aircraft_applicability="unconfirmed_by_catalog"`` marker is added rather
  than flattening the two notions together.

Only contracts already frozen in ``pm_agent/workorders/evidence.py`` are
imported here; nothing in that module, in ``agent.py`` or in the graph is
edited by this file.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from google.genai import errors as genai_errors
from google.genai import types

from pm_agent.config import MODEL
from pm_agent.sub_agents.ipc_manual_retrieval.agent import ask_vertex_retrieval
from pm_agent.workorders.evidence import EvidenceRequest, SourceResult, SourceStatus

__all__ = [
    "SOURCE_NAME",
    "IpcPermissionDenied",
    "IpcRetrievalError",
    "IpcTimeout",
    "IpcUnavailable",
    "run_ipc_evidence",
]

logger = logging.getLogger(__name__)

SOURCE_NAME = "ipc_manual_retrieval"

# This datastore only ever establishes family-level catalogue presence (see
# the module docstring); it can never confirm applicability to one specific
# aircraft, so every record/citation carries this constant.
_CATALOG_SCOPE = "family_catalogue"
_AIRCRAFT_APPLICABILITY_UNCONFIRMED = "unconfirmed_by_catalog"

# A neutral nudge to actually invoke the retrieval tool (agent.py's own
# comment notes gemini-3.8-flash otherwise tends to ignore it); this is not
# the specialist's citation-formatting prompt and none of its text is reused.
_SEARCH_INSTRUCTION = (
    "Search the connected data source for the exact query text you are given "
    "as input, then briefly summarize what was retrieved."
)

# Bounds one query's round trip so a hung backend surfaces as SourceStatus
# .TIMEOUT instead of blocking the whole evidence branch indefinitely.
_SEARCH_TIMEOUT_SECONDS = 20.0

SearchFn = Callable[[str], Awaitable["types.GroundingMetadata | None"]]


class IpcRetrievalError(RuntimeError):
    """Base class for a failed (not merely empty) IPC datastore lookup."""


class IpcPermissionDenied(IpcRetrievalError):
    """The runtime principal was denied access to the datastore/model call."""


class IpcTimeout(IpcRetrievalError):
    """The retrieval call did not complete within the search timeout."""


class IpcUnavailable(IpcRetrievalError):
    """The datastore/backend is unreachable or not provisioned."""


def _default_client() -> Any:
    """Build a Vertex AI ``genai.Client`` lazily, on first real use only.

    Imported/constructed inside the function (not at module import time) so
    importing this module - and running its unit tests, which always inject a
    fake ``search`` - never requires credentials or network access.
    """
    from google import genai

    # No project/location passed explicitly: like the rest of the app (see
    # pm_agent/config.py and CLAUDE.md's "fix GOOGLE_CLOUD_LOCATION, not the
    # model name" guidance), this defers to GOOGLE_CLOUD_PROJECT /
    # GOOGLE_CLOUD_LOCATION so the adapter follows the same deployed
    # configuration as every other Gemini call in this app.
    return genai.Client(vertexai=True)


async def _default_search(query: str) -> types.GroundingMetadata | None:
    """Production ``SearchFn``: one grounded ``generate_content`` call.

    Reuses ``ask_vertex_retrieval.data_store_id`` and ``.max_results`` - the
    already-configured ``VertexAiSearchTool`` from ``agent.py`` - as the sole
    source of truth for which datastore and result cap to use, so this
    adapter cannot drift from the specialist's datastore configuration.
    """
    client = _default_client()
    config = types.GenerateContentConfig(
        system_instruction=_SEARCH_INSTRUCTION,
        tools=[
            types.Tool(
                retrieval=types.Retrieval(
                    vertex_ai_search=types.VertexAISearch(
                        datastore=ask_vertex_retrieval.data_store_id,
                        max_results=ask_vertex_retrieval.max_results,
                    )
                )
            )
        ],
    )
    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=MODEL, contents=query, config=config
            ),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise IpcTimeout(f"IPC datastore search timed out for {query!r}") from exc
    except genai_errors.ClientError as exc:
        if exc.code == 403 or exc.status == "PERMISSION_DENIED":
            raise IpcPermissionDenied(
                f"IPC datastore search was denied for {query!r}"
            ) from exc
        if exc.code in (404, 400) or exc.status in (
            "NOT_FOUND",
            "FAILED_PRECONDITION",
        ):
            raise IpcUnavailable(
                f"IPC datastore is unreachable or not provisioned for {query!r}"
            ) from exc
        raise
    except genai_errors.ServerError as exc:
        raise IpcUnavailable(
            f"IPC datastore backend is unavailable for {query!r}"
        ) from exc

    candidates = response.candidates or []
    if not candidates:
        return None
    return candidates[0].grounding_metadata


def _distinct_queries(request: EvidenceRequest) -> tuple[tuple[str, str, str], ...]:
    """Resolved PN/component context to search, per the shared request.

    Returns ``(part_key, part_number, resolution_status)`` triples, deduped
    by ``part_key`` and ordered by first appearance in
    ``request.part_candidates``. The query text is the display part number
    itself (short - one to a few tokens - matching the datastore's own
    short-query guidance), not a combined/boolean query across parts.
    """
    seen: set[str] = set()
    queries: list[tuple[str, str, str]] = []
    for candidate in request.part_candidates:
        part_number = candidate.part_number.strip()
        if not part_number or candidate.part_key in seen:
            continue
        seen.add(candidate.part_key)
        queries.append((candidate.part_key, part_number, candidate.resolution_status))
    return tuple(queries)


def _applicability_fields(request: EvidenceRequest) -> dict[str, Any]:
    """Family-vs-aircraft distinction shared by every record/citation.

    ``catalog_scope`` is always the family-level constant: this datastore
    never establishes anything more specific. ``aircraft_applicability`` is
    only added when the request actually names an aircraft to distinguish
    from - otherwise there is nothing to contrast catalogue presence
    against, so the key is left out entirely rather than set to some default.
    """
    fields: dict[str, Any] = {"catalog_scope": _CATALOG_SCOPE}
    if request.aircraft.aircraft_id:
        fields["aircraft_applicability"] = _AIRCRAFT_APPLICABILITY_UNCONFIRMED
    return fields


def _document_key(retrieved: types.GroundingChunkRetrievedContext, index: int) -> str:
    """A best-effort dedup key for citations across chunks/queries.

    Falls back to a per-chunk unique key when neither ``document_name`` nor
    ``title``/``uri`` is present, so unidentifiable chunks are kept as
    separate citations rather than silently merged.
    """
    if retrieved.document_name:
        return f"document_name::{retrieved.document_name}"
    if retrieved.title or retrieved.uri:
        return f"title_uri::{retrieved.title}::{retrieved.uri}"
    return f"unidentified::{index}"


def _retrieved_context_fields(
    retrieved: types.GroundingChunkRetrievedContext,
) -> dict[str, Any]:
    """Only the real fields the SDK actually populated, nothing invented.

    There is no "revision" field on ``GroundingChunkRetrievedContext`` in the
    installed ``google-genai`` SDK at all, so one is never fabricated here.
    ``page_number`` is included when present, but the SDK documents it as
    "not supported in Vertex AI", so it is expected to usually be absent.
    """
    fields: dict[str, Any] = {}
    if retrieved.document_name:
        fields["document_name"] = retrieved.document_name
    if retrieved.title:
        fields["title"] = retrieved.title
    if retrieved.uri:
        fields["uri"] = retrieved.uri
    if retrieved.page_number is not None:
        fields["page_number"] = retrieved.page_number
    return fields


def _records_and_citations(
    request: EvidenceRequest,
    query_results: Sequence[tuple[tuple[str, str, str], types.GroundingMetadata | None]],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Flatten every query's grounding metadata into records/citations/ids.

    ``records`` is excerpt-level (one per retrieved chunk, tagged with which
    query produced it); ``citations`` is deduped per referenced document for
    display. Chunks without a ``retrieved_context`` (e.g. a stray web/maps
    chunk) are skipped: this datastore only ever produces retrieved-context
    chunks, and nothing here should invent a citation for a different kind.
    """
    applicability = _applicability_fields(request)
    records: list[dict[str, Any]] = []
    citations: dict[str, dict[str, Any]] = {}
    source_ids: list[str] = []

    for (part_key, part_number, resolution_status), metadata in query_results:
        if metadata is None or not metadata.grounding_chunks:
            continue
        for index, chunk in enumerate(metadata.grounding_chunks):
            retrieved = chunk.retrieved_context
            if retrieved is None:
                continue
            context_fields = _retrieved_context_fields(retrieved)

            record: dict[str, Any] = {
                "part_key": part_key,
                "part_number": part_number,
                "resolution_status": resolution_status,
                **applicability,
                **context_fields,
            }
            if retrieved.text:
                record["text"] = retrieved.text
            records.append(record)

            doc_key = _document_key(retrieved, index)
            if doc_key not in citations:
                citations[doc_key] = {**applicability, **context_fields}
                identifier = (
                    retrieved.document_name or retrieved.uri or retrieved.title
                )
                if identifier:
                    source_ids.append(identifier)

    return tuple(records), tuple(citations.values()), tuple(source_ids)


async def run_ipc_evidence(
    request: EvidenceRequest, *, search: SearchFn | None = None
) -> SourceResult:
    """Run the IPC evidence branch for one shared per-turn request.

    ``search`` defaults to a real Vertex AI Search grounded call
    (:func:`_default_search`); unit tests always inject a fake so no network
    access or credentials are required. A failure raised by ``search`` (one
    of the ``Ipc*`` exceptions below) is reported through ``status`` and a
    sanitized ``error_detail`` - it is never reported as
    ``SourceStatus.NO_MATCH``, and no query is attempted after the first
    failure.
    """
    search_fn = search or _default_search
    queries = _distinct_queries(request)

    if not queries:
        # Nothing to search on is a real, explicit empty result - not an
        # error - and must not be confused with a query that ran and found
        # nothing.
        return SourceResult(
            source=SOURCE_NAME,
            status=SourceStatus.NO_MATCH,
            executed_parameters={"queries": ()},
            counts={"queries_executed": 0, "chunks_returned": 0, "documents_matched": 0},
            limit=ask_vertex_retrieval.max_results,
        )

    executed_queries: list[str] = []
    query_results: list[tuple[tuple[str, str, str], types.GroundingMetadata | None]] = []
    for triple in queries:
        _, part_number, _ = triple
        try:
            metadata = await search_fn(part_number)
        except IpcRetrievalError as exc:
            status = {
                IpcPermissionDenied: SourceStatus.PERMISSION_DENIED,
                IpcTimeout: SourceStatus.TIMEOUT,
                IpcUnavailable: SourceStatus.UNAVAILABLE,
            }.get(type(exc), SourceStatus.ERROR)
            return SourceResult(
                source=SOURCE_NAME,
                status=status,
                executed_parameters={
                    "queries": tuple(executed_queries),
                    "failed_query": part_number,
                },
                counts={
                    "queries_executed": len(executed_queries),
                    "chunks_returned": 0,
                    "documents_matched": 0,
                },
                limit=ask_vertex_retrieval.max_results,
                error_detail=str(exc),
            )
        except Exception:  # Boundary: never leak internal exception text here.
            logger.exception(
                "Unexpected error querying the IPC datastore for %r", part_number
            )
            return SourceResult(
                source=SOURCE_NAME,
                status=SourceStatus.ERROR,
                executed_parameters={
                    "queries": tuple(executed_queries),
                    "failed_query": part_number,
                },
                counts={
                    "queries_executed": len(executed_queries),
                    "chunks_returned": 0,
                    "documents_matched": 0,
                },
                limit=ask_vertex_retrieval.max_results,
                error_detail="Unexpected error while querying the IPC datastore.",
            )
        executed_queries.append(part_number)
        query_results.append((triple, metadata))

    records, citations, source_ids = _records_and_citations(request, query_results)
    chunks_returned = sum(
        len(metadata.grounding_chunks) if metadata and metadata.grounding_chunks else 0
        for _, metadata in query_results
    )
    max_results = ask_vertex_retrieval.max_results
    truncated = bool(max_results) and any(
        metadata and metadata.grounding_chunks and len(metadata.grounding_chunks) >= max_results
        for _, metadata in query_results
    )
    counts = {
        "queries_executed": len(executed_queries),
        "chunks_returned": chunks_returned,
        "documents_matched": len(citations),
    }

    if not records:
        return SourceResult(
            source=SOURCE_NAME,
            status=SourceStatus.NO_MATCH,
            executed_parameters={"queries": tuple(executed_queries)},
            counts=counts,
            truncated=truncated,
            limit=max_results,
        )

    return SourceResult(
        source=SOURCE_NAME,
        status=SourceStatus.SUCCESS,
        records=records,
        source_ids=source_ids,
        citations=citations,
        executed_parameters={"queries": tuple(executed_queries)},
        counts=counts,
        truncated=truncated,
        limit=max_results,
    )
