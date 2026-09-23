"""Part-history/coverage and existing symptom/FAA evidence adapters (N3).

Each function here takes one shared :class:`~pm_agent.workorders.evidence.EvidenceRequest`
(plus an injectable runner/provider seam so tests never touch real BigQuery)
and returns exactly one
:class:`~pm_agent.workorders.evidence.SourceResult`, matching the N1 contract
every evidence branch in ``docs/adk-parallel-evidence-plan.md`` shares.

``get_part_history`` and ``get_part_coverage`` reuse :class:`QueryRunner`
(``bq_analytics/queries.py``) exactly as N2 built it: fixed SQL templates
under ``sql/``, real named BigQuery parameters, code-owned table allowlists.
Neither touches ``QueryRunner.run``/``_job_config``; own-work-order exclusion
is applied in Python after fetch (documented on each function) because that
runner binds only scalar parameters and an exclusion set can hold an
arbitrary number of ids.

``find_historical_symptoms`` and ``find_faa_reports`` both go through
:class:`~pm_agent.sub_agents.bq_analytics.history.BQHistoryProvider` (or an
equivalent injected provider, e.g. ``amos_data.retrieval.LocalHistoryProvider``
in tests) via the same ``HistoryQuery``/``.search()`` call: every eligibility,
exclusion, ranking and availability decision is the provider's, reused
unmodified, never reimplemented here. The two adapters differ only in which
``source_namespace`` they keep from the shared result - ``amos`` for
historical symptoms, ``faa_sdr`` (already tagged ``history_scope ==
"public_report"`` by the provider) for FAA reports - so FAA evidence and AMOS
installation history are always reported as separate counting units, never
merged.

Full semantic history stays unavailable until the reference corpus and
embeddings are loaded (see ``docs/next-agent-handover.md``): with no
``corpus_version`` configured, the provider itself reports an error naming
the missing piece, which both adapters surface as
``SourceStatus.UNAVAILABLE`` - never a silent fallback to a different
retrieval method.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from amos_data.parser import part_key as _normalize_part_key
from amos_data.retrieval import HistoryProvider, HistoryQuery, RetrievalMethod
from pm_agent.sub_agents.bq_analytics.queries import (
    DEFAULT_LIMIT,
    FAA_TABLE,
    MAX_LIMIT,
    WORKORDERS_TABLE,
    QueryRunner,
)
from pm_agent.workorders.evidence import EvidenceRequest, SourceResult, SourceStatus
from pm_agent.workorders.service import _aware_timestamp

__all__ = [
    "find_faa_reports",
    "find_historical_symptoms",
    "get_part_coverage",
    "get_part_history",
]

_AMOS_NAMESPACES = frozenset({"amos"})
_FAA_NAMESPACES = frozenset({"faa_sdr"})

# BQHistoryProvider/LocalHistoryProvider error vocabulary this module knows how
# to name; anything else still maps to a safe, credential-free ERROR rather
# than being swallowed.
_PROVIDER_UNAVAILABLE_ERRORS = frozenset(
    {"reference_corpus_unconfigured", "vector_embedding_provider_unconfigured"}
)
_PROVIDER_TIMEOUT_ERRORS = frozenset({"query_timeout"})


def _resolve_part(
    request: EvidenceRequest, part_number: str | None
) -> tuple[str, str] | None:
    """Return ``(normalized_part_key, display_part_number)`` or ``None``.

    ``part_number`` (raw, any formatting) overrides ``request.part_candidates``
    when given. Otherwise the first candidate with
    ``resolution_status == "resolved"`` is used, falling back to the first
    candidate; ``None`` means no part number is available from either source.
    """
    if part_number:
        normalized = _normalize_part_key(part_number)
        return (normalized, part_number) if normalized else None
    candidates = request.part_candidates
    if not candidates:
        return None
    chosen = next(
        (c for c in candidates if c.resolution_status == "resolved"), candidates[0]
    )
    return (chosen.part_key, chosen.part_number)


def _excluded(row: Mapping[str, Any], excluded_ids: frozenset[str]) -> bool:
    """Own-work-order exclusion, checked against both id forms a row may carry."""
    if not excluded_ids:
        return False
    return (
        str(row.get("workorder_id") or "") in excluded_ids
        or str(row.get("workorder_number") or "") in excluded_ids
    )


def _distinct(rows: Any, key: str) -> tuple[str, ...]:
    return tuple(sorted({str(row[key]) for row in rows if row.get(key)}))


def _history_scope(row: Mapping[str, Any], aircraft_id: str | None) -> str:
    """Tag one get_part_history row "own_aircraft" vs "comparable_amos".

    Mirrors BQHistoryProvider's vocabulary (see history.py/retrieval.py) but is
    derived independently here: this is a structured on/off-part row, not a
    text-retrieval case, so there is no shared eligibility/ranking logic to
    reuse for it.
    """
    if not aircraft_id:
        return "comparable_amos"
    needle = aircraft_id.strip().upper()
    haystacks = (
        row.get("aircraft_full_registration"),
        row.get("aircraft_registration"),
        row.get("aircraft_msn"),
    )
    if any(str(value or "").strip().upper() == needle for value in haystacks):
        return "own_aircraft"
    return "comparable_amos"


def get_part_history(
    runner: QueryRunner,
    request: EvidenceRequest,
    *,
    part_number: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> SourceResult:
    """Dated AMOS installation history for one part: comparable-fleet and
    own-aircraft rows, all strictly before ``request.analysis_as_of``.

    ``part_number`` overrides ``request.part_candidates`` when given (see
    :func:`_resolve_part`); a request with neither is a blank-parameter error,
    matching the existing ``bigquery.get_workorder`` etc. convention in
    ``queries.py``.

    Own-work-order exclusion (``request.exclusions.workorder_ids``) is
    enforced in Python after fetch, not in SQL: ``QueryRunner.run`` binds only
    scalar parameters (see ``queries.py``), and this exclusion set can hold an
    arbitrary number of ids. The future-record exclusion (nothing at or after
    ``request.analysis_as_of``) *is* enforced in SQL (``@before_ts`` in
    ``sql/get_part_history.sql``), since it is always exactly one scalar
    value. ``limit`` truncation accounts for rows the exclusion filter may
    remove by over-fetching first.

    ``source`` is ``"bigquery.get_part_history"``. Each record's
    ``history_scope`` is ``"own_aircraft"`` when it matches
    ``request.aircraft.aircraft_id``, else ``"comparable_amos"`` -
    reproducing ``BQHistoryProvider``'s vocabulary for a structured (not
    retrieval) source. ``counts["history_row_count"]`` and
    ``counts["distinct_workorder_count"]`` are independent units.
    """
    resolved = _resolve_part(request, part_number)
    executed_parameters: dict[str, Any] = {
        "part_number": resolved[0] if resolved else None,
        "before_ts": request.analysis_as_of,
        "family": request.aircraft.family,
        "position": request.aircraft.position,
        "limit": limit,
    }
    if resolved is None:
        return SourceResult(
            source="bigquery.get_part_history",
            status=SourceStatus.ERROR,
            executed_parameters=executed_parameters,
            error_detail="error:blank_part_number",
        )
    normalized_part, _display_part = resolved
    excluded_ids = request.exclusions.workorder_ids
    fetch_limit = min(MAX_LIMIT, limit + len(excluded_ids)) if excluded_ids else limit
    outcome = runner.run(
        "get_part_history",
        [
            ("part_number", "STRING", normalized_part),
            ("before_ts", "TIMESTAMP", request.analysis_as_of),
            ("family", "STRING", request.aircraft.family),
            ("position", "STRING", request.aircraft.position),
        ],
        limit=fetch_limit,
        table_placeholders={"workorders_table": runner.table(WORKORDERS_TABLE)},
    )
    if outcome.status not in (SourceStatus.SUCCESS, SourceStatus.NO_MATCH):
        return SourceResult(
            source="bigquery.get_part_history",
            status=outcome.status,
            executed_parameters=executed_parameters,
            error_detail=outcome.error_detail,
        )
    filtered = tuple(row for row in outcome.rows if not _excluded(row, excluded_ids))
    truncated = outcome.truncated or len(filtered) > limit
    rows = filtered[:limit]
    tagged = tuple(
        {**row, "history_scope": _history_scope(row, request.aircraft.aircraft_id)}
        for row in rows
    )
    status = SourceStatus.SUCCESS if tagged else SourceStatus.NO_MATCH
    return SourceResult(
        source="bigquery.get_part_history",
        status=status,
        records=tagged,
        source_ids=_distinct(rows, "workorder_id"),
        executed_parameters=executed_parameters,
        counts={
            "history_row_count": len(tagged),
            "distinct_workorder_count": len(_distinct(rows, "workorder_id")),
        },
        truncated=truncated,
        limit=limit,
    )


def get_part_coverage(
    runner: QueryRunner,
    request: EvidenceRequest,
    *,
    range_start: str,
    range_end: str | None = None,
    part_number: str | None = None,
    fetch_limit: int = MAX_LIMIT,
) -> SourceResult:
    """Three independent coverage counts for one part in
    ``[range_start, range_end]``: distinct AMOS work orders, AMOS component
    changes, and FAA reports. ``counts`` always carries all three as separate
    keys - they are never summed or merged into one number.

    ``range_start``/``range_end`` are ISO-8601 timestamps with a timezone
    (same format as ``request.analysis_as_of``); ``range_end`` defaults to
    ``request.analysis_as_of`` when omitted, the same future-record boundary
    used by ``get_part_history``. Unlike that boundary elsewhere in this
    module, this date-range filter is a plain calendar count window, not a
    claim about when a report was actually publicly available - that
    stronger check belongs to :func:`find_faa_reports`
    (via ``BQHistoryProvider``), which this raw ``faa_sdr_wo_parts`` table
    does not carry the ``availability_status``/``available_at`` columns to
    support directly (see ``sql/get_part_coverage_faa.sql``).

    Own-work-order exclusion (``request.exclusions.workorder_ids``) is
    applied to the AMOS rows in Python after fetch, for the same
    scalar-parameter-only reason as :func:`get_part_history`; it does not
    apply to the FAA rows, which carry no work-order linkage at all - FAA
    reports stay a separate counting unit from AMOS work orders/changes.

    Counts are exact within the fetched window (bounded by ``fetch_limit``,
    at most ``MAX_LIMIT`` rows per side, per ``QueryRunner.run``); if either
    side's fetch was truncated, ``truncated=True`` flags that the
    corresponding count(s) are a lower bound rather than reporting a
    false-precise number. This is verifiable in tests only up to the fake
    runner's fixture size; whether real coverage in this dataset ever exceeds
    ``MAX_LIMIT`` (200) rows for one part/date-range is only checkable
    against live BigQuery.

    ``source`` is ``"bigquery.get_part_coverage"``.
    """
    resolved = _resolve_part(request, part_number)
    range_end = range_end or request.analysis_as_of
    executed_parameters: dict[str, Any] = {
        "part_number": resolved[0] if resolved else None,
        "range_start": range_start,
        "range_end": range_end,
        "family": request.aircraft.family,
        "limit": fetch_limit,
    }
    if resolved is None:
        return SourceResult(
            source="bigquery.get_part_coverage",
            status=SourceStatus.ERROR,
            executed_parameters=executed_parameters,
            error_detail="error:blank_part_number",
        )
    normalized_part, _display_part = resolved
    changes_outcome = runner.run(
        "get_part_coverage_changes",
        [
            ("part_number", "STRING", normalized_part),
            ("range_start", "TIMESTAMP", range_start),
            ("range_end", "TIMESTAMP", range_end),
            ("family", "STRING", request.aircraft.family),
        ],
        limit=fetch_limit,
        table_placeholders={"workorders_table": runner.table(WORKORDERS_TABLE)},
    )
    if changes_outcome.status not in (SourceStatus.SUCCESS, SourceStatus.NO_MATCH):
        return SourceResult(
            source="bigquery.get_part_coverage",
            status=changes_outcome.status,
            executed_parameters=executed_parameters,
            error_detail=changes_outcome.error_detail,
        )
    excluded_ids = request.exclusions.workorder_ids
    filtered_changes = tuple(
        row for row in changes_outcome.rows if not _excluded(row, excluded_ids)
    )
    faa_outcome = runner.run(
        "get_part_coverage_faa",
        [
            ("part_number", "STRING", normalized_part),
            ("range_start", "TIMESTAMP", range_start),
            ("range_end", "TIMESTAMP", range_end),
            ("family", "STRING", request.aircraft.family),
        ],
        limit=fetch_limit,
        table_placeholders={"faa_table": runner.table(FAA_TABLE)},
    )
    if faa_outcome.status not in (SourceStatus.SUCCESS, SourceStatus.NO_MATCH):
        return SourceResult(
            source="bigquery.get_part_coverage",
            status=faa_outcome.status,
            executed_parameters=executed_parameters,
            error_detail=faa_outcome.error_detail,
        )
    workorder_count = len(_distinct(filtered_changes, "workorder_id"))
    component_change_count = len(filtered_changes)
    faa_report_count = len(_distinct(faa_outcome.rows, "report_id"))
    truncated = changes_outcome.truncated or faa_outcome.truncated
    status = (
        SourceStatus.SUCCESS
        if (workorder_count or component_change_count or faa_report_count)
        else SourceStatus.NO_MATCH
    )
    return SourceResult(
        source="bigquery.get_part_coverage",
        status=status,
        source_ids=_distinct(filtered_changes, "workorder_id"),
        executed_parameters=executed_parameters,
        counts={
            "workorder_count": workorder_count,
            "component_change_count": component_change_count,
            "faa_report_count": faa_report_count,
        },
        truncated=truncated,
        limit=fetch_limit,
    )


def _symptoms_text(request: EvidenceRequest, part_number: str | None) -> str:
    """Free-text query for HistoryQuery.symptoms (must not be blank).

    Prefers ``request.available_symptoms``. ``find_faa_reports``'s contract
    input is "PN/context" rather than symptom text, so when no symptoms were
    supplied this falls back to a PN/aircraft-context string, and finally to
    ``request.question`` (always non-blank per EvidenceRequest) - never
    inventing free text the caller did not supply, only reusing what is
    already on the shared request.
    """
    joined = "\n".join(s.strip() for s in request.available_symptoms if s and s.strip())
    if joined:
        return joined
    fallback_bits = [part_number, request.aircraft.family, request.aircraft.position]
    fallback = " ".join(bit for bit in fallback_bits if bit)
    return fallback or request.question


def _build_history_query(
    request: EvidenceRequest,
    *,
    part_number: str | None,
    corpus_version: str | None,
    method: RetrievalMethod,
    limit: int,
) -> HistoryQuery:
    resolved = _resolve_part(request, part_number)
    target_parts = (
        (resolved[0],)
        if resolved
        else tuple(c.part_key for c in request.part_candidates)
    )
    symptoms = _symptoms_text(request, resolved[1] if resolved else None)
    as_of = _aware_timestamp(request.analysis_as_of, "analysis_as_of")
    return HistoryQuery(
        symptoms=symptoms,
        analysis_as_of=as_of,
        target_part_numbers=target_parts,
        aircraft_family=request.aircraft.family,
        aircraft_id=request.aircraft.aircraft_id,
        position=request.aircraft.position,
        exclude_workorder_ids=request.exclusions.workorder_ids,
        exclude_text_hashes=request.exclusions.text_hashes,
        corpus_version=corpus_version,
        limit=max(1, min(limit, 50)),
        method=method,
    )


def _classify_provider_error(error: str) -> SourceStatus:
    """Map one BQHistoryProvider/LocalHistoryProvider error string to a
    non-NO_MATCH SourceStatus. Mirrors queries._classify_exception's
    vocabulary, since the provider only hands back a fixed error string (never
    the exception instance queries.py classifies from)."""
    if error in _PROVIDER_UNAVAILABLE_ERRORS:
        return SourceStatus.UNAVAILABLE
    if error in _PROVIDER_TIMEOUT_ERRORS:
        return SourceStatus.TIMEOUT
    if error.startswith("query_error:"):
        exc_name = error.split(":", 1)[1]
        if exc_name == "Forbidden":
            return SourceStatus.PERMISSION_DENIED
        if exc_name in ("DeadlineExceeded", "TimeoutError"):
            return SourceStatus.TIMEOUT
        if exc_name in ("ServiceUnavailable", "TooManyRequests"):
            return SourceStatus.UNAVAILABLE
        return SourceStatus.ERROR
    return SourceStatus.ERROR


def _provider_source_result(
    source: str,
    provider: HistoryProvider,
    request: EvidenceRequest,
    *,
    part_number: str | None,
    corpus_version: str | None,
    method: RetrievalMethod,
    limit: int,
    namespaces: frozenset[str],
    count_key: str,
) -> SourceResult:
    """Shared plumbing for find_historical_symptoms/find_faa_reports: build
    the HistoryQuery, call provider.search() exactly once, and keep only the
    requested source_namespace's cases. Never retries with a different
    method/corpus - whatever the provider reports is final."""
    query = _build_history_query(
        request,
        part_number=part_number,
        corpus_version=corpus_version,
        method=method,
        limit=limit,
    )
    executed_parameters: dict[str, Any] = {
        "part_number": next(iter(query.target_part_numbers), None),
        "corpus_version": corpus_version,
        "method": method,
        "as_of": request.analysis_as_of,
        "limit": query.limit,
        "aircraft_family": query.aircraft_family,
        "position": query.position,
    }
    result = provider.search(query)
    if result.get("status") == "error":
        errors = result.get("errors") or ["unknown_provider_error"]
        error = errors[0]
        status = _classify_provider_error(error)
        return SourceResult(
            source=source,
            status=status,
            executed_parameters=executed_parameters,
            error_detail=f"{status.value}:{error}",
        )
    cases = tuple(
        case
        for case in result.get("cases", ())
        if case.get("source_namespace") in namespaces
    )
    truncation = result.get("truncation") or {}
    status = SourceStatus.SUCCESS if cases else SourceStatus.NO_MATCH
    return SourceResult(
        source=source,
        status=status,
        records=cases,
        source_ids=tuple(
            sorted({str(c["case_id"]) for c in cases if c.get("case_id")})
        ),
        citations=tuple(
            {"case_id": c.get("case_id"), "citation": c.get("citation")}
            for c in cases
            if c.get("citation")
        ),
        executed_parameters=executed_parameters,
        counts={count_key: len(cases)},
        truncated=bool(truncation.get("truncated", False)),
        limit=query.limit,
    )


def find_historical_symptoms(
    provider: HistoryProvider,
    request: EvidenceRequest,
    *,
    part_number: str | None = None,
    corpus_version: str | None = None,
    method: RetrievalMethod = "hybrid",
    limit: int = 10,
) -> SourceResult:
    """Existing keyword/vector/hybrid AMOS case retrieval, via
    ``BQHistoryProvider.search()`` (or an equivalent injected provider, e.g.
    ``amos_data.retrieval.LocalHistoryProvider`` in tests). Every eligibility,
    exclusion, ranking and availability decision (own-WO, text-hash,
    future-record, split, corpus version, availability status) is the
    provider's, reused unmodified - this adapter only builds the
    ``HistoryQuery`` from ``request`` and keeps the ``amos``-namespace cases
    from the shared search result. :func:`find_faa_reports` keeps the
    ``faa_sdr``-namespace cases from the same kind of search separately, so
    the two are never merged into one count.

    If ``corpus_version`` is not configured, or the requested ``method``
    needs semantic search the provider cannot serve, the provider reports an
    "error" status naming the missing piece (e.g.
    ``"reference_corpus_unconfigured"``, ``"vector_embedding_provider_unconfigured"``);
    this is surfaced as ``SourceStatus.UNAVAILABLE`` with that name folded
    into ``error_detail`` - this adapter never retries with a different
    method as a silent fallback.

    ``source`` is ``"bigquery.find_historical_symptoms"``.
    ``counts["symptom_case_rows"]`` counts only the AMOS-namespace cases.
    """
    return _provider_source_result(
        "bigquery.find_historical_symptoms",
        provider,
        request,
        part_number=part_number,
        corpus_version=corpus_version,
        method=method,
        limit=limit,
        namespaces=_AMOS_NAMESPACES,
        count_key="symptom_case_rows",
    )


def find_faa_reports(
    provider: HistoryProvider,
    request: EvidenceRequest,
    *,
    part_number: str | None = None,
    corpus_version: str | None = None,
    method: RetrievalMethod = "hybrid",
    limit: int = 10,
) -> SourceResult:
    """Public FAA-report retrieval evidence, kept separate from AMOS
    aircraft/serial installation history (``find_historical_symptoms``,
    ``get_part_history``). Goes through the same ``BQHistoryProvider.search()``
    call as :func:`find_historical_symptoms` (same ``HistoryQuery``, same
    eligibility/exclusion/availability checks, not reimplemented here),
    filtered to ``source_namespace == "faa_sdr"`` cases only; each such case
    already carries ``history_scope == "public_report"`` from the provider.

    Submission/difficulty dates alone are not treated as sufficient proof
    that a report was historically available - this adapter relies entirely
    on the provider's own ``available_at``/``availability_status`` eligibility
    gate, so an unconfigured corpus reports ``SourceStatus.UNAVAILABLE``
    rather than silently answering from raw dates. (``get_part_coverage``'s
    FAA count, by contrast, is a plain calendar-window count over the raw
    ``faa_sdr_wo_parts`` table with no availability claim at all - see its
    docstring and ``sql/get_part_coverage_faa.sql``.)

    ``source`` is ``"bigquery.find_faa_reports"``.
    ``counts["faa_report_rows"]`` counts only the faa_sdr-namespace cases.
    """
    return _provider_source_result(
        "bigquery.find_faa_reports",
        provider,
        request,
        part_number=part_number,
        corpus_version=corpus_version,
        method=method,
        limit=limit,
        namespaces=_FAA_NAMESPACES,
        count_key="faa_report_rows",
    )
