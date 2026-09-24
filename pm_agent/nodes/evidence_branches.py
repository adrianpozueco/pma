"""Parallel BigQuery/IPC evidence branches, their join and final answer (N5).

Wires N2/N3 (``bq_analytics/queries.py``, ``bq_analytics/adapters.py``) and N4
(``ipc_manual_retrieval/evidence.py``) into the graph as two nodes that run
concurrently off one shared :class:`~pm_agent.workorders.evidence.EvidenceRequest`
(built by ``pm_agent/nodes/workorder_upload.py``), a :class:`JoinNode` that
collects both, and one final node that renders a single combined answer.

Neither ``bq_evidence`` nor ``ipc_evidence`` ever raises past its own
boundary: an escaping exception would abort the whole join (and lose the
other branch's already-computed evidence), so every failure - including one
this module did not anticipate - is caught here and converted into a
``SourceResult`` with an honest ``SourceStatus`` instead.

Design decision, not yet revisited: ``get_part_coverage`` (``adapters.py``) is
never called from ``bq_evidence``. It requires a mandatory ``range_start``
with no corresponding field on ``EvidenceRequest``; inventing an arbitrary
trailing window here would itself be fabricated scope, which the plan
forbids. Flagged for team-lead review rather than guessed at.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from google.adk.agents.context import Context
from google.adk.events import Event
from google.adk.workflow import JoinNode
from google.genai import types

from pm_agent.sub_agents.bq_analytics.adapters import (
    find_faa_reports,
    find_historical_symptoms,
    get_part_history,
)
from pm_agent.sub_agents.bq_analytics.queries import (
    QueryRunner,
    build_default_runner,
    get_component_changes,
    get_workorder,
    get_workorder_actions,
)
from pm_agent.sub_agents.ipc_manual_retrieval.evidence import run_ipc_evidence
from pm_agent.workorders.evidence import EvidenceRequest, SourceResult, SourceStatus
from pm_agent.workorders.service import HistoryProvider, default_history_provider

__all__ = [
    "bq_evidence",
    "compose_evidence_answer",
    "ipc_evidence",
    "join_evidence",
]

logger = logging.getLogger(__name__)

# Same state key ``workorder_upload.py`` writes: the join node's structural
# input is only {"bq_evidence": ..., "ipc_evidence": ...}, so the upload
# result needed for part (a) of the composed answer travels via ctx.state
# instead. Written fresh every turn that reaches the evidence fan-out, so a
# later read within the same invocation never sees a stale prior turn's value.
EVIDENCE_CONTEXT_STATE_KEY = "workorder_evidence_context"

_FAILURE_STATUSES = frozenset(
    {
        SourceStatus.UNAVAILABLE,
        SourceStatus.PERMISSION_DENIED,
        SourceStatus.TIMEOUT,
        SourceStatus.ERROR,
    }
)

# Deterministic precedence when several sub-tool calls inside one branch
# disagree on status: worst-first, matching what a reader would want named.
_STATUS_PRECEDENCE = (
    SourceStatus.PERMISSION_DENIED,
    SourceStatus.ERROR,
    SourceStatus.TIMEOUT,
    SourceStatus.UNAVAILABLE,
    SourceStatus.NO_MATCH,
    SourceStatus.SUCCESS,
)

# Production seams: tests monkeypatch these two names directly (never
# QueryRunner/HistoryProvider/run_ipc_evidence internals), so a fake never has
# to reach through a real BigQuery client or Vertex AI credentials.
_build_runner = build_default_runner
_build_history_provider = default_history_provider


def _merge_branch(source: str, results: Sequence[SourceResult]) -> SourceResult:
    """Fold one branch's several sub-tool ``SourceResult``s into exactly one.

    SUCCESS if any sub-result succeeded (partial evidence is still evidence);
    otherwise NO_MATCH if any sub-result no-matched; otherwise the worst
    failing status by ``_STATUS_PRECEDENCE``. Per-tool status/error detail is
    preserved under ``executed_parameters["tools"]`` (keyed by each sub-tool's
    own ``source`` string) so a caller can still see exactly which underlying
    query failed, even though only one status is reported at the branch level.
    """
    if not results:
        return SourceResult(
            source=source,
            status=SourceStatus.NO_MATCH,
            counts={"tools_executed": 0},
        )

    tools: dict[str, Any] = {}
    records: list[Any] = []
    source_ids: list[str] = []
    citations: list[Any] = []
    truncated = False
    succeeded = 0
    failed = 0
    for result in results:
        tools[result.source] = {
            "status": result.status.value,
            "error_detail": result.error_detail,
            "counts": dict(result.counts),
        }
        if result.status is SourceStatus.SUCCESS:
            succeeded += 1
            records.extend(result.records)
            source_ids.extend(result.source_ids)
            citations.extend(result.citations)
        elif result.status in _FAILURE_STATUSES:
            failed += 1
        truncated = truncated or result.truncated

    statuses = {result.status for result in results}
    if SourceStatus.SUCCESS in statuses:
        status = SourceStatus.SUCCESS
    elif SourceStatus.NO_MATCH in statuses:
        status = SourceStatus.NO_MATCH
    else:
        status = next(s for s in _STATUS_PRECEDENCE if s in statuses)

    error_detail = None
    if status in _FAILURE_STATUSES:
        failing = next(r for r in results if r.status is status)
        error_detail = failing.error_detail

    return SourceResult(
        source=source,
        status=status,
        records=tuple(records),
        source_ids=tuple(dict.fromkeys(source_ids)),
        citations=tuple(citations),
        executed_parameters={"tools": tools},
        counts={
            "tools_executed": len(results),
            "tools_succeeded": succeeded,
            "tools_failed": failed,
        },
        truncated=truncated,
        error_detail=error_detail,
    )


def _unavailable_result(source: str, *, part_number: str, reason: str) -> SourceResult:
    """Explicit UNAVAILABLE stand-in when no HistoryProvider is configured.

    Never silently skipped: the caller must be able to see that this specific
    sub-tool was not run and why, in the same ``executed_parameters["tools"]``
    breakdown a real call would have populated.
    """
    return SourceResult(
        source=source,
        status=SourceStatus.UNAVAILABLE,
        executed_parameters={"part_number": part_number},
        error_detail=f"unavailable:{reason}",
    )


def _run_bq_tools(
    request: EvidenceRequest,
    runner: QueryRunner,
    provider: HistoryProvider | None,
) -> SourceResult:
    """Pure, synchronous BQ branch body - directly unit-testable with a fake
    runner/provider, no asyncio or network involved.

    Calls the work-order-scoped tools when ``request.selected_wo_id`` is set,
    then one part-history/symptom/FAA lookup per resolved part candidate.
    ``get_part_coverage`` is deliberately not called (see module docstring).
    """
    results: list[SourceResult] = []

    if request.selected_wo_id:
        results.append(get_workorder(runner, request.selected_wo_id))
        results.append(get_workorder_actions(runner, request.selected_wo_id))
        results.append(get_component_changes(runner, request.selected_wo_id))

    for candidate in request.part_candidates:
        results.append(
            get_part_history(runner, request, part_number=candidate.part_number)
        )
        if provider is not None:
            results.append(
                find_historical_symptoms(
                    provider, request, part_number=candidate.part_number
                )
            )
            results.append(
                find_faa_reports(provider, request, part_number=candidate.part_number)
            )
        else:
            results.append(
                _unavailable_result(
                    "bigquery.find_historical_symptoms",
                    part_number=candidate.part_number,
                    reason="no_history_provider_configured",
                )
            )
            results.append(
                _unavailable_result(
                    "bigquery.find_faa_reports",
                    part_number=candidate.part_number,
                    reason="no_history_provider_configured",
                )
            )

    return _merge_branch("bq_evidence", results)


async def _run_bq_branch(request: EvidenceRequest) -> SourceResult:
    """Production async seam: build a real runner/provider, run the
    synchronous tool calls off the event loop."""
    runner = _build_runner()
    provider = _build_history_provider()
    return await asyncio.to_thread(_run_bq_tools, request, runner, provider)


async def _run_ipc_branch(request: EvidenceRequest) -> SourceResult:
    """Production async seam for the IPC branch; tests monkeypatch this name
    directly rather than reaching into ``run_ipc_evidence``'s own ``search``
    seam, since the graph node never sees that argument."""
    return await run_ipc_evidence(request)


async def bq_evidence(ctx: Context, node_input: dict[str, Any]) -> dict[str, Any]:
    """Evidence node: BigQuery branch. Never raises - see module docstring."""
    del ctx  # Unused; kept for symmetry/auto-detection with ipc_evidence.
    # Reading the request and serializing the result both belong inside the
    # boundary: a malformed node_input or a value the evidence contract cannot
    # coerce (a live BigQuery datetime did this) would otherwise escape and
    # abort the join, losing the other branch's work as well.
    try:
        request = EvidenceRequest.from_dict(node_input["evidence_request"])
        result = await _run_bq_branch(request)
        return result.to_dict()
    except Exception:  # boundary: must not abort the join.
        logger.exception("Unexpected error in bq_evidence branch")
        return SourceResult(
            source="bq_evidence",
            status=SourceStatus.ERROR,
            error_detail="error:unexpected_bq_evidence_failure",
        ).to_dict()


async def ipc_evidence(ctx: Context, node_input: dict[str, Any]) -> dict[str, Any]:
    """Evidence node: IPC manual branch. Never raises - see module docstring."""
    del ctx
    # Reading the request and serializing the result both belong inside the
    # boundary: a malformed node_input or a value the evidence contract cannot
    # coerce (a live BigQuery datetime did this) would otherwise escape and
    # abort the join, losing the other branch's work as well.
    try:
        request = EvidenceRequest.from_dict(node_input["evidence_request"])
        result = await _run_ipc_branch(request)
        return result.to_dict()
    except Exception:  # boundary: must not abort the join.
        logger.exception("Unexpected error in ipc_evidence branch")
        return SourceResult(
            source="ipc_manual_retrieval",
            status=SourceStatus.ERROR,
            error_detail="error:unexpected_ipc_evidence_failure",
        ).to_dict()


join_evidence = JoinNode(name="join_evidence")


def _status_label(status: SourceStatus) -> str:
    return {
        SourceStatus.SUCCESS: "succeeded",
        SourceStatus.NO_MATCH: "found no matching records",
        SourceStatus.UNAVAILABLE: "is UNAVAILABLE (not deployed/loaded)",
        SourceStatus.PERMISSION_DENIED: "was PERMISSION_DENIED",
        SourceStatus.TIMEOUT: "TIMED OUT",
        SourceStatus.ERROR: "FAILED with an ERROR",
    }[status]


def _render_bq_section(result: SourceResult) -> list[str]:
    lines = ["**(b) Earlier BigQuery cases**"]
    tools = (result.executed_parameters or {}).get("tools") or {}
    if result.status is SourceStatus.SUCCESS:
        lines.append(
            f"BigQuery evidence {_status_label(result.status)}: "
            f"{len(result.records)} record(s) across "
            f"{result.counts.get('tools_succeeded', 0)} of "
            f"{result.counts.get('tools_executed', 0)} queries."
        )
        for tool_source, detail in tools.items():
            if detail.get("status") == "success":
                lines.append(f"- `{tool_source}`: {detail.get('counts')}")
    elif result.status is SourceStatus.NO_MATCH:
        lines.append("BigQuery was queried and found no matching records.")
    else:
        lines.append(
            f"BigQuery evidence {_status_label(result.status)}"
            + (f" ({result.error_detail})" if result.error_detail else "")
            + ". This is not the same as 'no matches found' - the query did not "
            "complete, so absence of results here is not evidence of absence."
        )
    failing_tools = [
        (name, detail)
        for name, detail in tools.items()
        if detail.get("status") not in ("success", "no_match")
    ]
    if failing_tools and result.status is SourceStatus.SUCCESS:
        lines.append("Some BigQuery sub-queries did not complete:")
        for name, detail in failing_tools:
            lines.append(f"- `{name}`: {detail.get('status')} ({detail.get('error_detail')})")
    return lines


def _render_ipc_section(result: SourceResult) -> list[str]:
    lines = ["**(c) Retrieved manual pages (IPC)**"]
    if result.status is SourceStatus.SUCCESS:
        lines.append(
            f"Knowledge-base evidence {_status_label(result.status)}: "
            f"{len(result.citations)} document(s) matched, "
            f"{result.counts.get('chunks_returned', 0)} excerpt(s) returned."
        )
        for citation in result.citations:
            title = citation.get("title") or citation.get("document_name") or citation.get("uri")
            scope = citation.get("catalog_scope", "family_catalogue")
            applicability = citation.get("aircraft_applicability")
            note = f" ({applicability})" if applicability else ""
            lines.append(f"- {title} - catalogue scope: {scope}{note}")
    elif result.status is SourceStatus.NO_MATCH:
        lines.append("The IPC knowledge base was queried and found no matching pages.")
    else:
        lines.append(
            f"IPC knowledge-base evidence {_status_label(result.status)}"
            + (f" ({result.error_detail})" if result.error_detail else "")
            + ". This is not the same as 'no matches found' - the search did not "
            "complete, so absence of results here is not evidence of absence."
        )
    return lines


def _pma_lines(pma: dict[str, Any]) -> list[str]:
    """Section (d) lines for the PMA prediction block (§6.5 fixed strings,
    plus the Option B projected-window strings, requirement 7, approved
    2026-09-24).

    Pure/sync and independent of ``ctx``/the graph so it is directly
    unit-testable; ``compose_evidence_answer`` only calls it. Reads the flat
    ``pma`` dict shape :meth:`PredictionResult.to_dict` actually produces
    (``component_key``/``part_number``/``position``/``match``/``interval``/...
    at the top level - the plan's §6.2 JSON is illustrative/nested, this is
    not), never a nested ``"component"`` sub-object. Every line comes from
    ``chat.py`` (chat's ``_pma_lines`` for the component, decision/interval
    and Option B (a)-(d) lines, plus the stale-TAC line (e)) so both
    renderers show identical wording from one source instead of two
    hand-kept-in-sync copies.
    """
    # Lazy import: avoids a module-load cycle with `chat.py`.
    from pm_agent.workorders.chat import _pma_lines as _chat_pma_lines
    from pm_agent.workorders.chat import _pma_stale_tac_line

    lines = _chat_pma_lines(pma)

    stale_line = _pma_stale_tac_line(pma)
    if stale_line:
        lines.append(stale_line)

    return lines


async def compose_evidence_answer(ctx: Context, node_input: dict[str, Any]) -> Event:
    """Final node: one combined answer from both evidence branches.

    ``node_input`` is the join's structural payload, keyed by predecessor
    node name (``{"bq_evidence": ..., "ipc_evidence": ...}``). The uploaded
    work order's own facts (part (a)) are read back from ``ctx.state`` - see
    ``EVIDENCE_CONTEXT_STATE_KEY`` - since the join payload only carries the
    two branch results, not the original upload/analysis.
    """
    from pm_agent.workorders.chat import (
        _recommendation_block_lines,
        render_uploaded_workorder_summary,
    )

    stashed = ctx.state.get(EVIDENCE_CONTEXT_STATE_KEY) or {}
    upload_result = stashed.get("upload_result")

    bq_result = SourceResult.from_dict(node_input["bq_evidence"])
    ipc_result = SourceResult.from_dict(node_input["ipc_evidence"])

    lines: list[str] = []
    # The condensed recommendation block (approved 2026-09-24) renders once,
    # before every other section, not inside (a) or (d).
    rec_lines = _recommendation_block_lines(
        (upload_result or {}).get("analysis") or {}
    )
    if rec_lines:
        lines.extend(rec_lines)
        lines.append("")
    if upload_result is not None:
        lines.append("**(a) What the uploaded work order records**")
        # PMA lines render once, in section (d), not also in (a) or the
        # recommendation block above.
        lines.append(
            render_uploaded_workorder_summary(
                upload_result, include_pma=False, include_recommendation=False
            )
        )
    else:
        lines.append("**(a) What the uploaded work order records**")
        lines.append("No uploaded work order is associated with this evidence request.")

    lines.append("")
    lines.extend(_render_bq_section(bq_result))
    lines.append("")
    lines.extend(_render_ipc_section(ipc_result))

    lines.append("")
    lines.append("**(d) Missing/conflicting evidence and unavailable predictions**")
    gaps: list[str] = []
    if upload_result is not None:
        analysis = upload_result.get("analysis") or {}
        for limitation in analysis.get("limitations", []):
            gaps.append(limitation)
        gaps.extend(_pma_lines(analysis.get("pma") or {}))
    if bq_result.status in _FAILURE_STATUSES:
        gaps.append(
            f"BigQuery evidence is incomplete: branch {_status_label(bq_result.status)}."
        )
    if ipc_result.status in _FAILURE_STATUSES:
        gaps.append(
            f"IPC manual evidence is incomplete: branch {_status_label(ipc_result.status)}."
        )
    if bq_result.status is SourceStatus.SUCCESS and ipc_result.status is SourceStatus.SUCCESS:
        pass  # No structural gap beyond whatever `gaps` already collected above.
    pma_block = ((upload_result or {}).get("analysis") or {}).get("pma") or {}
    pma_decision = pma_block.get("decision")
    if pma_block.get("projected_window"):
        gaps.append(
            "Failure probability, remaining life and a replacement deadline are not given; "
            "the projected window above is a fleet pattern, not a forecast or a deadline, "
            "and closing TAC is not the current counter or component age."
        )
    elif pma_decision == "historical_interval":
        gaps.append(
            "Failure probability, remaining life and a replacement deadline are not given; "
            "the interval above is historical, not a forecast, and closing TAC is not the "
            "current counter or component age."
        )
    else:
        gaps.append(
            "Failure probability, remaining life and replacement deadline are unavailable. "
            "No validated model or replacement policy is configured; closing TAC is historical "
            "and is not the current counter or component age."
        )
    lines.extend(f"- {gap}" for gap in gaps)

    text = "\n".join(lines)
    return Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
    )
