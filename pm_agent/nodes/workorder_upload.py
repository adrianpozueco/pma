"""Route XML attachments through the shared parser before ordinary chat.

    START -> prepare_workorder_upload
               -"workorder_prompt"-> display_workorder_upload   (selection/replay/error prompts, unchanged)
               -"workorder_evidence"-> bq_evidence + ipc_evidence -> join_evidence -> compose_evidence_answer
               -"chat"-> router -> ipc / bq

An "analyzed" result builds one shared
:class:`~pm_agent.workorders.evidence.EvidenceRequest` and fans it out to both
evidence branches (``pm_agent/nodes/evidence_branches.py``); every other
status (selection required, replay-cutoff prompt, invalid follow-up, parse
error, ...) still short-circuits straight to the unchanged
``display_workorder_upload`` without ever touching the evidence branches.
"""

import uuid
from dataclasses import replace
from typing import Any

from google.adk.agents.context import Context
from google.adk.events import Event
from google.genai import types

from amos_data.documents import normalize_text, text_hash
from amos_data.parser import part_key
from pm_agent.nodes.evidence_branches import EVIDENCE_CONTEXT_STATE_KEY
from pm_agent.workorders.chat import analyze_chat_upload, render_chat_upload
from pm_agent.workorders.evidence import (
    AircraftContext,
    ArtifactProvenance,
    CounterValue,
    EvidenceCounters,
    EvidenceRequest,
    ExclusionSet,
    PartCandidate,
)


def _user_question_text(ctx: Context) -> str:
    """Raw user message text for this turn, or "" if none/non-text."""
    parts = (ctx.user_content.parts if ctx.user_content else None) or []
    texts = [part.text for part in parts if getattr(part, "text", None)]
    return "\n".join(texts).strip()


def _build_evidence_request(ctx: Context, result: dict[str, Any]) -> EvidenceRequest:
    """One shared, per-invocation :class:`EvidenceRequest` from an "analyzed"
    ``analyze_chat_upload`` result.

    Every field is mapped from data ``analyze_chat_upload`` already computed;
    nothing here re-derives or re-guesses a fact the parser/service already
    established. ``invocation_scope_id`` is freshly generated on every call so
    a branch can never be handed - or reuse - a previous turn's scope.
    """
    analysis = result["analysis"]
    source = result["source"]
    row = result["uploaded_workorder"]
    context = analysis["parsed_context"]
    aircraft = context.get("aircraft") or {}
    pma = analysis.get("pma") or {}

    part_candidates = [
        PartCandidate(
            part_key=target["part_key"],
            part_number=target["part_number"],
            resolution_status=target["resolution_status"],
            roles=tuple(target.get("roles", ())),
        )
        for target in analysis["target_parts"]
    ]
    pma_part_number = pma.get("part_number")
    if pma.get("component_key") and pma_part_number:
        pma_key = part_key(pma_part_number)
        # A PMA part that is also a configured target gets the role added to
        # that candidate; a second candidate would run every per-part query
        # (part history, IPC) twice and duplicate its records.
        for index, candidate in enumerate(part_candidates):
            if candidate.part_key == pma_key:
                part_candidates[index] = replace(
                    candidate, roles=(*candidate.roles, "pma_component")
                )
                break
        else:
            part_candidates.append(
                PartCandidate(
                    part_key=pma_key,
                    part_number=pma_part_number,
                    roles=("pma_component",),
                )
            )
    part_candidates = tuple(part_candidates)

    available_symptoms = tuple(
        text
        for symptom in context["symptoms"]
        if (text := (symptom.get("description") or symptom.get("headline")))
    )

    question = _user_question_text(ctx) or (
        f"Evidence for uploaded work order {row['workorder_number']}."
    )

    return EvidenceRequest(
        question=question,
        input_mode=analysis["input_mode"],
        analysis_as_of=analysis["analysis_as_of"],
        invocation_scope_id=uuid.uuid4().hex,
        artifact=ArtifactProvenance(
            filename=source["filename"],
            version=source["version"],
            sha256=source["upload_hash"],
        ),
        selected_wo_id=analysis.get("wo_id"),
        part_candidates=part_candidates,
        # `position`: prefer the PMA gate's resolved position (already
        # normalised/validated against the focus set by prediction/policy.py)
        # and fall back to parsed_context's own `position` (T10) when the PMA
        # core did not resolve one (disabled, out of scope, no match, ...).
        aircraft=AircraftContext(
            aircraft_id=aircraft.get("full_registration"),
            family=aircraft.get("variant"),
            position=pma.get("position") or context.get("position"),
        ),
        available_symptoms=available_symptoms,
        counters=EvidenceCounters(
            issue_tac=CounterValue.from_dict(context["issue"]["tac"]),
            closing_tac=CounterValue.from_dict(context["closing"]["tac"]),
            supplied_current_tac=CounterValue.from_dict(context["current_aircraft_tac"]),
        ),
        exclusions=ExclusionSet(
            workorder_ids=frozenset(
                filter(None, [analysis.get("wo_id"), row.get("workorder_number")])
            ),
            # Approximates, but is not byte-identical to, service._history()'s
            # own exclude_text_hashes: that also hashes the combined joined
            # query_text, which analyze_chat_upload does not expose to
            # callers. Hashing each available symptom individually is the
            # closest equivalent obtainable from this result.
            text_hashes=frozenset(
                text_hash(normalize_text(symptom)) for symptom in available_symptoms
            ),
        ),
    )


async def prepare_workorder_upload(ctx: Context) -> Event:
    result = await analyze_chat_upload(ctx)
    if result is None:
        return Event(output={}, route="chat")
    if result["status"] != "analyzed":
        return Event(output=result, route="workorder_prompt")
    request = _build_evidence_request(ctx, result)
    # Stashed under a state key fresh every turn that reaches this branch, so
    # compose_evidence_answer (which only receives the join's
    # {"bq_evidence": ..., "ipc_evidence": ...} payload) can still read back
    # the original upload result; a state write within this invocation is
    # immediately visible to every later node in the same turn, and this key
    # is unconditionally overwritten on every "analyzed" turn, so a later
    # turn can never see a stale earlier one's upload result.
    ctx.state[EVIDENCE_CONTEXT_STATE_KEY] = {
        "upload_result": result,
        "evidence_request": request.to_dict(),
    }
    # Keep emitting the full analyze_chat_upload result as this node's output
    # (unchanged contract: callers that only look at prepare_workorder_upload's
    # own output, e.g. existing tests reading status/source/analysis, must
    # keep seeing exactly what they always did) and additionally carry
    # "evidence_request" alongside it, since that merged dict is what the two
    # evidence branches receive as their own node_input.
    return Event(
        output={**result, "evidence_request": request.to_dict()},
        route="workorder_evidence",
    )


def display_workorder_upload(node_input: dict[str, Any]) -> Event:
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=render_chat_upload(node_input))],
        ),
    )
