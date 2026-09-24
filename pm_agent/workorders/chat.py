"""Session-scoped XML attachment analysis for ADK chat.

This first upload slice is deterministic: uploaded work orders are never
replaced by a database lookup and their text is not sent to a chat model.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from google.adk.artifacts.artifact_util import parse_artifact_uri
from google.genai import types

from amos_data.parser import MAX_XML_BYTES, ParseError, parse_workorders
from pm_agent.prediction.policy import round_cycles
from pm_agent.prediction.service import default_predictor
from pm_agent.workorders.artifacts import (
    load_current_session_artifact,
    validate_artifact_filename,
)
from pm_agent.workorders.service import (
    AnalysisInput,
    WorkOrderAnalysisError,
    WorkOrderAnalysisService,
    _is_closed,
    _iso_at_or_before,
)

STATE_KEY = "workorder_upload"
XML_MIMES = {"application/xml", "text/xml"}
OPTION_NAMES = set(AnalysisInput.__dataclass_fields__) | {"filename"}
OPTION_PATTERN = re.compile(
    r"\b("
    + "|".join(sorted(OPTION_NAMES))
    + r")\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s,;]+))"
)


def is_xml_part(part: types.Part) -> bool:
    blob = part.inline_data or part.file_data
    return bool(
        blob
        and (
            (blob.display_name or "").lower().endswith(".xml")
            or (blob.mime_type or "").lower().split(";", 1)[0].strip() in XML_MIMES
        )
    )


def _options(text: str) -> dict[str, Any]:
    values = {}
    for match in OPTION_PATTERN.finditer(text):
        key = match[1]
        if key in values:
            raise WorkOrderAnalysisError(
                "Specify each analysis option once.", code="duplicate_option"
            )
        values[key] = next(value for value in match.groups()[1:] if value is not None)
    if "current_aircraft_tac" in values:
        try:
            values["current_aircraft_tac"] = int(values["current_aircraft_tac"])
        except ValueError as exc:
            raise WorkOrderAnalysisError(
                "current_aircraft_tac must be an integer.", code="invalid_tac"
            ) from exc
    return values


async def _store_attachments(ctx: Any, parts: list[types.Part]) -> list[dict[str, Any]]:
    if len(parts) > 10:
        raise WorkOrderAnalysisError(
            "Upload at most 10 XML files with a combined size of 25 MiB.",
            code="oversized_input",
        )
    references = []
    filenames = set()
    total_bytes = 0
    for index, part in enumerate(parts, 1):
        blob = part.inline_data or part.file_data
        mime = (blob.mime_type or "").lower().split(";", 1)[0].strip()
        if mime not in XML_MIMES | {"", "application/octet-stream", "text/plain"}:
            raise WorkOrderAnalysisError(
                "The XML attachment has an incompatible MIME type.",
                code="invalid_artifact_mime",
            )
        if part.file_data:
            reference = parse_artifact_uri(part.file_data.file_uri)
            session = ctx.session
            if reference is None or (
                reference.app_name,
                reference.user_id,
                reference.session_id,
            ) != (session.app_name, session.user_id, session.id):
                raise WorkOrderAnalysisError(
                    "Select an XML attachment from this session; external file URLs are not supported.",
                    code="invalid_artifact_reference",
                )
            filename, version = reference.filename, reference.version
            validate_artifact_filename(filename)
            payload = await load_current_session_artifact(ctx, filename, version)
        else:
            filename = validate_artifact_filename(
                blob.display_name or f"workorder-{index}.xml"
            )
            payload = bytes(blob.data or b"")
            version = None
        # File references are loaded lazily, so the aggregate limit must be
        # checked after loading each one as well as for inline uploads.
        total_bytes += len(payload)
        if total_bytes > MAX_XML_BYTES:
            raise WorkOrderAnalysisError(
                "Upload at most 10 XML files with a combined size of 25 MiB.",
                code="oversized_input",
            )
        if filename in filenames:
            raise WorkOrderAnalysisError(
                "Attach files with distinct filenames.", code="duplicate_filename"
            )
        filenames.add(filename)
        # Reject invalid XML before saving it as a reusable session artifact.
        parsed = await asyncio.to_thread(
            parse_workorders, payload, source_name=filename
        )
        if version is None:
            artifact = types.Part(
                inline_data=types.Blob(
                    data=payload, mime_type="application/xml", display_name=filename
                )
            )
            version = await ctx.save_artifact(filename=filename, artifact=artifact)
        references.append(
            {
                "filename": filename,
                "version": version,
                "upload_hash": parsed.upload_hash,
            }
        )
    return references


def _recorded_evidence(row: dict[str, Any], request: AnalysisInput) -> dict[str, Any]:
    """Expose completed actions only as dated historical evidence, never symptoms."""
    cutoff = datetime.fromisoformat(request.analysis_as_of.replace("Z", "+00:00"))
    snapshot_available = _iso_at_or_before(
        (row.get("envelope") or {}).get("envelope_ts"), cutoff
    )
    actions = []
    if request.mode == "historical_replay" and _is_closed(row) and snapshot_available:
        for step in row.get("work_steps", []):
            for action in step.get("actions", []):
                if _iso_at_or_before(action.get("performed_ts"), cutoff):
                    actions.append(
                        {
                            "step_id": step.get("uuid"),
                            "action_id": action.get("uuid"),
                            "performed_at": action.get("performed_ts"),
                            "text": action.get("action_text"),
                            "component_changes": action.get("component_changes", []),
                        }
                    )
    return {
        "workorder_number": row.get("workorder_number"),
        "recorded_actions": actions,
        "is_closed": _is_closed(row),
    }


async def analyze_chat_upload(ctx: Any) -> dict[str, Any] | None:
    """Return an upload result, or None to retain ordinary chat routing.

    Only current-message XML attachments, explicit upload follow-ups, or a
    pending selection activate this path. Previously uploaded files cannot
    silently replace a new ordinary chat question.
    """
    parts = (ctx.user_content.parts if ctx.user_content else None) or []
    xml_parts = [part for part in parts if is_xml_part(part)]
    text = " ".join(part.text for part in parts if part.text)
    previous = ctx.state.get(STATE_KEY) or {}
    followup = bool(
        previous
        and (
            OPTION_PATTERN.search(text)
            or re.search(
                r"\b(?:this|attached|uploaded|selected) (?:work\s*order|xml|file)\b",
                text,
                re.I,
            )
            or text.strip() in previous.get("available_wo_ids", [])
        )
    )
    if not xml_parts and not followup:
        return None

    state = {} if xml_parts else dict(previous)
    if xml_parts:
        ctx.state[STATE_KEY] = {}  # A rejected new upload must not reuse old evidence.
    try:
        options = _options(text)
        if xml_parts:
            explicit_options = {k: v for k, v in options.items() if k != "filename"}
            state = {
                "references": await _store_attachments(ctx, xml_parts),
                "options": dict(explicit_options),
                "explicit_options": dict(explicit_options),
            }
            # Keep valid uploaded bytes available when analysis options need
            # correction, without persisting those unvalidated options.
            ctx.state[STATE_KEY] = {
                "references": state["references"],
                "options": {},
                "explicit_options": {},
            }
        references = state.get("references", [])
        filename = options.pop("filename", None) or state.get("filename")
        if not filename and len(references) == 1:
            filename = references[0]["filename"]
        matches = [ref for ref in references if ref["filename"] == filename]
        if len(matches) != 1:
            ctx.state[STATE_KEY] = state
            return {
                "status": "selection_required",
                "message": 'Choose one XML file by replying filename="FILE.xml".',
                "choices": [ref["filename"] for ref in references],
            }
        reference = matches[0]
        filename_changed = filename != state.get("filename")
        # Pin the exact saved version, including version zero.
        payload = await load_current_session_artifact(
            ctx, filename, reference["version"]
        )
        parsed = await asyncio.to_thread(
            parse_workorders, payload, source_name=filename
        )
        if parsed.upload_hash != reference["upload_hash"]:
            raise WorkOrderAnalysisError(
                "The saved attachment no longer matches this upload. Attach it again.",
                code="artifact_changed",
            )
        # Keep candidate state local until the request is known to be valid.
        # This prevents a typo in a follow-up selection or option from making
        # every later turn inherit the bad value.
        if filename_changed:
            # A work-order selection and service defaults belong to the
            # previously selected file. Retain explicit user inputs while
            # dropping those per-file values when changing files.
            base_options = dict(state.get("explicit_options", state.get("options", {})))
            base_options.pop("selected_wo_id", None)
        else:
            base_options = dict(state.get("options", {}))
        merged = {**base_options, **options}
        if text.strip() in state.get("available_wo_ids", []):
            merged["selected_wo_id"] = text.strip()
        # Validate field values before a multiple-work-order prompt stores
        # them. The selected row is not needed for these checks.
        AnalysisInput(
            mode=merged.get("mode", "new_work_order"),
            analysis_as_of=merged.get("analysis_as_of", datetime.now(UTC).isoformat()),
            current_aircraft_tac=merged.get("current_aircraft_tac"),
            current_tac_source=merged.get("current_tac_source"),
            current_tac_observed_at=merged.get("current_tac_observed_at"),
            target_part_number=merged.get("target_part_number"),
        )
        try:
            row = WorkOrderAnalysisService._select(
                parsed.workorders, merged.get("selected_wo_id")
            )
        except WorkOrderAnalysisError as exc:
            if exc.code != "workorder_selection_required":
                raise
            choices = [
                str(r.get("workorder_number") or r.get("workorder_uuid"))
                for r in parsed.workorders
            ]
            state["filename"] = filename
            state["options"] = {
                k: v for k, v in merged.items() if k != "selected_wo_id"
            }
            state["explicit_options"] = {
                k: v for k, v in merged.items() if k != "selected_wo_id"
            }
            state["available_wo_ids"] = choices
            ctx.state[STATE_KEY] = state
            return {
                "status": "selection_required",
                "message": "Choose one work order by replying selected_wo_id=NUMBER.",
                "choices": choices,
            }
        if "analysis_as_of" not in options and re.search(
            r"\b(?:as of|replay at|before)\b", text, re.I
        ):
            return {
                "status": "missing_input",
                "message": "For an earlier replay, specify analysis_as_of=YYYY-MM-DDTHH:MM:SSZ.",
            }
        merged.setdefault(
            "mode", "historical_replay" if _is_closed(row) else "new_work_order"
        )
        merged.setdefault("analysis_as_of", datetime.now(UTC).isoformat())
        request = AnalysisInput(**merged)
        # This first integration uses the existing deterministic analysis
        # service without cloud history calls. Parallel evidence is a later slice.
        analysis = await asyncio.to_thread(
            WorkOrderAnalysisService(predictor=default_predictor()).analyze_xml,
            payload,
            request,
            source_name=filename,
            artifact_version=str(reference["version"]),
        )
        evidence = _recorded_evidence(row, request)
        explicit_options = dict(state.get("explicit_options", {}))
        explicit_options.update(
            {k: v for k, v in options.items() if k != "selected_wo_id"}
        )
        if "selected_wo_id" in options or (
            not filename_changed
            and "selected_wo_id" in state.get("explicit_options", {})
        ):
            explicit_options["selected_wo_id"] = merged["selected_wo_id"]
        state.update(
            filename=filename,
            options=asdict(request),
            explicit_options=explicit_options,
            available_wo_ids=[],
        )
        ctx.state[STATE_KEY] = state
        return {
            "status": "analyzed",
            "source": reference,
            "analysis": analysis,
            "uploaded_workorder": evidence,
        }
    except (WorkOrderAnalysisError, ParseError) as exc:
        return {"status": "error", "code": exc.code, "message": str(exc)}
    except Exception:
        return {
            "status": "error",
            "code": "artifact_unavailable",
            "message": "The session attachment could not be loaded or saved. Attach the XML again.",
        }


def _safe(value: object, limit: int = 2500) -> str:
    text = str(value if value is not None else "not recorded")
    if len(text) > limit:
        text = text[:limit] + " … [excerpt shortened]"
    return re.sub(r"([\\`*_\[\]<>|])", r"\\\1", text)


# Human-readable clauses for each `pma.reason` wire value (PMA-ONLINE-AGENT-plan.md
# §6.3). `PredictionResult.action` is never populated by the prediction core, so
# these fixed-string renderings are built here rather than read off the result.
_PMA_REASON_TEXT: dict[str, str] = {
    "empty_text": "no usable work-order text or part number was found",
    "embedding_failed": "the work-order text could not be embedded",
    "embedding_incompatible": "the embedding was not compatible with the reference space",
    "no_confident_component_match": "no focus component could be matched with confidence",
    "ambiguous_position": "the matched component's position could not be resolved",
    "component_not_in_focus_set": "this component is not one of the tracked focus components",
    "position_not_in_focus_set": "this component's position is not tracked",
    "aircraft_type_not_in_scope": "this aircraft type is not in scope",
    "no_lead_time_samples": "no historical lead-time samples exist for this component",
    "insufficient_samples": "too few historical samples exist for this component",
    "samples_not_symptom_to_replacement": (
        "past samples are mostly intervals between consecutive replacements, "
        "not symptom-to-replacement lead times"
    ),
    "prediction_disabled": "PMA prediction is disabled",
    "data_source_unavailable": "the prediction data source is currently unavailable",
}


def _pma_reason_text(reason: str | None) -> str:
    if reason in _PMA_REASON_TEXT:
        return _PMA_REASON_TEXT[reason]
    return (reason or "unknown reason").replace("_", " ")


def _cycles(value: Any) -> str:
    """Render a cycle/TAC count as an integer, e.g. for BigQuery
    ``PERCENTILE_CONT`` output that would otherwise print as
    ``1968.2000000000003`` (§7 requirement 7), rounded half-up like
    :func:`round_cycles` in the policy. Non-numeric/``None`` values fall
    through to :func:`_safe` unchanged."""
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float)):
        return _safe(value)
    return _safe(round_cycles(value))


# Option B: projected replacement window (approved 2026-09-24). It overrides
# plan OQ1 / BIGQUERY-AGENT-plan §8.5 "no absolute due TAC" only for this
# clearly-labelled fleet-pattern window; it never changes `decision`/`reason`.
_PMA_NO_PRIOR_REPLACEMENT_LINE = (
    "No earlier replacement of this component on this aircraft is recorded, "
    "so no aircraft-specific window is given."
)

_PMA_POSITION_TEXT: dict[str, str] = {
    "before_p50": "before the fleet median.",
    "between_p50_p90": "past the fleet median but inside p90.",
    "past_p90": "beyond the fleet p90; replacement is overdue against the fleet pattern.",
}


def _pma_supporting_line(supporting: dict[str, Any]) -> str:
    """Fixed string (a): the labelled fleet-pattern supporting interval."""
    return (
        "Fleet pattern (not a forecast): this component was replaced again after "
        f"p50 {_cycles(supporting.get('p50'))} / p90 {_cycles(supporting.get('p90'))} "
        f"cycles (n={_safe(supporting.get('n'))}, {_safe(supporting.get('aircraft'))} aircraft)."
    )


def _pma_window_line(window: dict[str, Any]) -> str:
    """Fixed string (b): the projected window for this aircraft."""
    return (
        "Projected window for this aircraft (fleet pattern, not a forecast): last "
        f"replaced at TAC {_cycles(window.get('last_replacement_tac'))} "
        f"({_safe(window.get('last_replacement_date'))}); if the pattern repeats, "
        f"next replacement around TAC {_cycles(window.get('tac_p50'))}"
        f"\u2013{_cycles(window.get('tac_p90'))}."
    )


def _pma_position_line(window: dict[str, Any]) -> str | None:
    """Fixed string (c): only rendered when ``cycles_since_last_replacement``
    (equivalently, ``position``) is present."""
    position = window.get("position")
    cycles_since = window.get("cycles_since_last_replacement")
    if position is None or cycles_since is None:
        return None
    suffix = _PMA_POSITION_TEXT.get(position)
    if suffix is None:
        return None
    return f"Latest known TAC is {_cycles(cycles_since)} cycles after that replacement, {suffix}"


def _pma_lines(pma: dict[str, Any]) -> list[str]:
    """Render the ``pma`` block using the fixed §6.5 strings, plus the
    Option B projected-window strings from requirement 7 (approved
    2026-09-24)."""
    lines: list[str] = []
    if pma.get("component_key"):
        gate_basis = (pma.get("match") or {}).get("gate_basis") or "unknown"
        lines.append(
            f"Matched component: {_safe(pma['component_key'])} ({_safe(gate_basis)})."
        )
    decision = pma.get("decision")
    if decision == "historical_interval" and pma.get("interval"):
        interval = pma["interval"]
        lines.append("**Observed historical interval (not a forecast)**")
        lines.append(
            f"Between closing TACs: p50 {_cycles(interval['p50'])} cycles, "
            f"p90 {_cycles(interval['p90'])} cycles (n={_safe(interval['n'])}, "
            f"{_safe(interval.get('aircraft'))} aircraft)."
        )
    elif decision == "out_of_scope":
        lines.append(
            f"Out of scope for PMA prediction: {_pma_reason_text(pma.get('reason'))}."
        )
    elif decision == "no_reliable_prediction":
        lines.append(f"No reliable prediction: {_pma_reason_text(pma.get('reason'))}.")
    supporting = pma.get("supporting_interval")
    if supporting:
        lines.append(_pma_supporting_line(supporting))
        window = pma.get("projected_window")
        if window:
            lines.append(_pma_window_line(window))
            position_line = _pma_position_line(window)
            if position_line:
                lines.append(position_line)
        elif "no_prior_replacement_on_aircraft" in (pma.get("limitations") or ()):
            lines.append(_PMA_NO_PRIOR_REPLACEMENT_LINE)
    return lines


def _pma_stale_tac_line(pma: dict[str, Any]) -> str | None:
    """Fixed string (e). When a ``projected_window`` is present the wording
    calls out that the window itself is not adjusted for cycles flown since;
    otherwise the old wording (no due TAC at all) still applies."""
    current_tac = pma.get("current_tac")
    if not current_tac or not current_tac.get("stale"):
        return None
    observed_at = _safe(current_tac.get("observed_at"))
    if pma.get("projected_window"):
        return (
            f"Current aircraft TAC is stale (last seen {observed_at}); the window "
            "is not adjusted for cycles flown since."
        )
    return (
        f"Current aircraft TAC is stale (last seen {observed_at}); no due TAC is given."
    )


# --------------------------------------------------------------------------
# Recommendation block (PMA-ONLINE-AGENT-plan.md-style §11.4 condensed
# recommendation; approved 2026-09-24, overrides BIGQUERY-AGENT-plan §8.5
# "no confidence tiers / no absolute due TAC" for this block only). Reads
# ``pma["recommendation"]`` - the serialised ``Recommendation`` dataclass a
# teammate is building in parallel in ``pm_agent/prediction/contracts.py`` -
# as a plain dict, so this module has no import-time dependency on that work
# landing first. When ``recommendation`` is null/missing every function here
# is a no-op and the rendered output is unchanged, per spec.
# --------------------------------------------------------------------------

_RECOMMENDATION_ACTION_TEXT: dict[str, str] = {
    "recommend_inspection_or_part_planning": "Plan inspection / part replacement",
    "monitor": "Monitor",
}

_RECOMMENDATION_BASIS_TEXT: dict[str, str] = {
    "similar_workorders": "similar work orders + lead-time history",
    "component_history": "component lead-time history",
    "fleet_replacement_interval": "fleet replacement pattern",
}


def _rounded_int(value: Any) -> int | None:
    """Round a TAC/cycle count for the JSON block the same way :func:`_cycles`
    rounds it for the text lines (§7 "integer rounding everywhere")."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round_cycles(value)


def _rounded_ratio(value: Any) -> float | None:
    """Similarity/CV rounded to 3 decimals for the JSON block (the text lines
    show 2); non-numeric values pass through as ``None``."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def _recommendation_status_text(predicted: dict[str, Any], reference_tac: int) -> str:
    """Where the latest known TAC sits against p50/p90/p95, most-overdue first."""
    p95 = predicted.get("tac_p95")
    if p95 is not None and reference_tac >= p95:
        return "already past p95"
    p90 = predicted.get("tac_p90")
    if p90 is not None and reference_tac >= p90:
        return "past p90"
    p50 = predicted.get("tac_p50")
    if p50 is not None and reference_tac >= p50:
        return "past p50"
    return f"{_cycles(predicted.get('lead_tac_p50'))} cycles before p50"


def _recommendation_header_line(rec: dict[str, Any]) -> str:
    action_text = _RECOMMENDATION_ACTION_TEXT.get(
        rec.get("action"), _safe(rec.get("action"))
    )
    return f"**Recommendation: {action_text} — {_safe(rec.get('component_key'))}**"


def _recommendation_replace_line(rec: dict[str, Any]) -> str:
    predicted = rec.get("predicted_replacement") or {}
    segments = [
        f"Replace around TAC {_cycles(predicted.get('tac_p50'))} (p50)",
        f"{_cycles(predicted.get('tac_p90'))} (p90)",
    ]
    if predicted.get("tac_p95") is not None:
        segments.append(f"{_cycles(predicted.get('tac_p95'))} (p95)")
    line = " · ".join(segments)
    reference_tac = rec.get("reference_tac")
    if reference_tac is None:
        return line + "."
    status = _recommendation_status_text(predicted, reference_tac)
    return f"{line}; latest known TAC {_cycles(reference_tac)} → {status}."


def _recommendation_confidence_line(rec: dict[str, Any]) -> str:
    confidence = rec.get("confidence") or {}
    detail = [f"n={_safe(confidence.get('sample_size'))}"]
    cv = confidence.get("cv")
    if isinstance(cv, (int, float)) and not isinstance(cv, bool):
        detail.append(f"CV {cv:.2f}")
    similarity = confidence.get("similarity")
    if isinstance(similarity, (int, float)) and not isinstance(similarity, bool):
        detail.append(f"similarity {similarity:.2f}")
    basis_text = _RECOMMENDATION_BASIS_TEXT.get(
        rec.get("basis"), _safe(rec.get("basis"))
    )
    return (
        f"Confidence: {_safe(confidence.get('level'))} ({', '.join(detail)}) "
        f"· {basis_text} · heuristic, not a calibrated forecast."
    )


def _recommendation_evidence_line(rec: dict[str, Any]) -> str | None:
    evidence = rec.get("evidence") or []
    if not evidence:
        return None
    items = []
    for item in evidence:
        sim = item.get("sim")
        sim_text = (
            f"{sim:.2f}"
            if isinstance(sim, (int, float)) and not isinstance(sim, bool)
            else _safe(sim)
        )
        items.append(f"WO {_safe(item.get('wo_id'))} ({sim_text})")
    return "Evidence: " + ", ".join(items)


def _recommendation_json_block(rec: dict[str, Any]) -> str:
    """The fenced ```json``` block, in the exact shape callers code against
    (not ``PredictionResult.to_dict()``'s flat ``pma`` shape): a top-level
    ``{"decision": "recommendation", "recommendations": [...]}`` envelope."""
    predicted = rec.get("predicted_replacement") or {}
    confidence = rec.get("confidence") or {}
    payload = {
        "decision": "recommendation",
        "recommendations": [
            {
                "component_key": rec.get("component_key"),
                "basis": rec.get("basis"),
                "predicted_replacement": {
                    "lead_tac_p50": _rounded_int(predicted.get("lead_tac_p50")),
                    "lead_tac_p90": _rounded_int(predicted.get("lead_tac_p90")),
                    "lead_tac_p95": _rounded_int(predicted.get("lead_tac_p95")),
                    "tac_p50": _rounded_int(predicted.get("tac_p50")),
                    "tac_p90": _rounded_int(predicted.get("tac_p90")),
                    "tac_p95": _rounded_int(predicted.get("tac_p95")),
                },
                "confidence": {
                    "level": confidence.get("level"),
                    "similarity": _rounded_ratio(confidence.get("similarity")),
                    "sample_size": confidence.get("sample_size"),
                    "cv": _rounded_ratio(confidence.get("cv")),
                },
                "evidence": [
                    {"wo_id": e.get("wo_id"), "sim": _rounded_ratio(e.get("sim"))}
                    for e in (rec.get("evidence") or [])
                ],
                "action": rec.get("action"),
            }
        ],
    }
    return "```json\n" + json.dumps(payload) + "\n```"


def _recommendation_block_lines(analysis: dict[str, Any]) -> list[str] | None:
    """The condensed recommendation block rendered before every other section
    of the answer, or ``None`` when ``pma["recommendation"]`` is null/missing
    (output is then unchanged, per spec)."""
    rec = (analysis.get("pma") or {}).get("recommendation")
    if not rec:
        return None
    lines = [
        _recommendation_header_line(rec),
        _recommendation_replace_line(rec),
        _recommendation_confidence_line(rec),
    ]
    evidence_line = _recommendation_evidence_line(rec)
    if evidence_line:
        lines.append(evidence_line)
    lines.append("")
    lines.append(_recommendation_json_block(rec))
    return lines


def _pma_component(analysis: dict[str, Any]) -> dict[str, Any] | None:
    """The PMA block when it matched a component, else ``None``."""
    pma = analysis.get("pma") or {}
    return pma if pma.get("component_key") else None


def _target_part_lines(analysis: dict[str, Any]) -> list[str]:
    """Configured targets, or the PMA-matched part when no target resolved.

    The IPC target list and the PMA focus set differ, so without this a matched
    PMA component sat under an empty "Target parts" heading.
    """
    lines = [
        f"- {_safe(t['part_number'])}: {_safe(t['description'])} ({_safe(t['resolution_status'])})."
        for t in analysis["target_parts"]
    ]
    pma = _pma_component(analysis)
    if not lines and pma:
        lines.append(
            f"- {_safe(pma.get('part_number') or pma['component_key'])}: resolved by the "
            f"PMA component match ({_safe(pma['component_key'])}); not in the IPC target list."
        )
    return lines


def _needs_target_selection(analysis: dict[str, Any]) -> bool:
    targets = analysis["target_parts"]
    if not targets and _pma_component(analysis):
        return False
    return len(targets) != 1 or any(
        t["resolution_status"] != "resolved" for t in targets
    )


def _current_tac_line(current: dict[str, Any], pma: dict[str, Any]) -> str:
    """Supplied current TAC, or the PMA's BigQuery closing TAC as context only."""
    known = pma.get("current_tac") or {}
    if current["status"] == "not_supplied" and known.get("value") is not None:
        return (
            "Current aircraft TAC: not supplied; last closing TAC in BigQuery "
            f"{_safe(known['value'])} at {_safe(known.get('observed_at'))} (context only)."
        )
    return (
        f"Current aircraft TAC: {_safe(current['value'])} ({_safe(current['status'])})."
    )


def render_uploaded_workorder_summary(
    result: dict[str, Any],
    *,
    include_pma: bool = True,
    include_recommendation: bool = True,
) -> str:
    """Render only what the uploaded XML itself establishes, plus the PMA
    prediction (``analysis["pma"]``) using the fixed §6.5 wording.

    Shares its header/target-parts/findings/actions/component-changes/TAC/PMA
    content with :func:`render_chat_upload`'s "analyzed" branch. This function
    is the one composed alongside real BigQuery/IPC branch results in
    :func:`pm_agent.nodes.evidence_branches.compose_evidence_answer`, which is
    the reason it exists as a separate, reusable piece from
    ``render_chat_upload``'s own (now effectively legacy) direct-display path.
    ``include_pma=False`` omits the PMA/stale-TAC lines for callers (the
    composed evidence answer) that render them in their own section.
    ``include_recommendation=False`` likewise omits the condensed
    recommendation block for that same caller, which renders it once at the
    very top of the whole composed answer instead of inside this section.
    """
    analysis, source = result["analysis"], result["source"]
    context = analysis["parsed_context"]
    row = result["uploaded_workorder"]
    aircraft = context.get("aircraft") or {}
    lines = [
        f"Analysed uploaded XML **{_safe(source['filename'])}** (version {source['version']}).",
        f"Work order **{_safe(row['workorder_number'])}**; aircraft {_safe(aircraft.get('full_registration'))}, {_safe(aircraft.get('variant'))}.",
        "Completed work order — historical analysis."
        if row.get("is_closed")
        else (
            "Historical replay of the uploaded snapshot."
            if analysis["input_mode"] == "historical_replay"
            else "Open work order — analysis of the uploaded snapshot."
        ),
        f"Analysis cutoff: {_safe(analysis['analysis_as_of'])}.",
        "",
        "**Target parts**",
    ]
    lines += _target_part_lines(analysis)
    pma = analysis["pma"] if include_pma else {}
    lines.extend(_pma_lines(pma))
    symptoms = context["symptoms"]
    lines.extend(["", "**Reported findings**"])
    for symptom in symptoms[:10]:
        lines.append(
            f"- {_safe(symptom.get('description') or symptom.get('headline'))}"
        )
    if not symptoms:
        lines.append("No symptom text is available at this cutoff.")
    if len(symptoms) > 10:
        lines.append(f"Showing 10 of {len(symptoms)} work-step findings.")
    actions = row["recorded_actions"]
    if actions:
        lines.extend(["", "**Recorded maintenance actions**"])
        for action in actions[:10]:
            lines.append(f"- {_safe(action['performed_at'])}: {_safe(action['text'])}")
        if len(actions) > 10:
            lines.append(f"Showing 10 of {len(actions)} actions.")
        changes = [c for a in actions for c in a["component_changes"]]
        if changes:
            lines.extend(
                [
                    "",
                    "**Recorded component changes**",
                    "",
                    "| Part off | Serial off | Part on | Serial on | Recorded position |",
                    "|---|---|---|---|---|",
                ]
            )
            for change in changes[:30]:
                lines.append(
                    "| "
                    + " | ".join(
                        _safe(change.get(k))
                        for k in (
                            "part_off_number",
                            "part_off_serial",
                            "part_on_number",
                            "part_on_serial",
                            "position",
                        )
                    )
                    + " |"
                )
            lines.append(
                f"\nShowing {min(30, len(changes))} of {len(changes)} changes. Recorded positions are not inferred from narrative nozzle numbers."
            )
    current = context["current_aircraft_tac"]
    closing = context["closing"]["tac"]
    lines.extend(
        [
            "",
            _current_tac_line(current, analysis.get("pma") or {}),
            f"Closing aircraft TAC: {_safe(closing['value'])}; this is not the current counter or component age.",
        ]
    )
    stale_tac_line = _pma_stale_tac_line(pma)
    if stale_tac_line:
        lines.append(stale_tac_line)
    if analysis["limitations"]:
        lines.extend(["", *[_safe(item) for item in analysis["limitations"]]])
    if _needs_target_selection(analysis):
        lines.append("\nTo select a target, reply target_part_number=PN.")
    lines.append(
        "\nFor a different historical cutoff, reply analysis_as_of=YYYY-MM-DDTHH:MM:SSZ."
    )
    body = "\n\n".join(lines[:4]) + "\n" + "\n".join(lines[4:])
    if not include_recommendation:
        return body
    rec_lines = _recommendation_block_lines(analysis)
    if not rec_lines:
        return body
    return "\n".join(rec_lines) + "\n\n" + body


def render_chat_upload(result: dict[str, Any]) -> str:
    """Render the parsed source evidence without an LLM inventing missing facts."""
    if result["status"] != "analyzed":
        lines = [result["message"]]
        if result.get("choices"):
            lines.extend(["", *[f"- {_safe(choice)}" for choice in result["choices"]]])
        return "\n".join(lines)
    analysis, source = result["analysis"], result["source"]
    context = analysis["parsed_context"]
    row = result["uploaded_workorder"]
    aircraft = context.get("aircraft") or {}
    lines = [
        f"Analysed uploaded XML **{_safe(source['filename'])}** (version {source['version']}).",
        f"Work order **{_safe(row['workorder_number'])}**; aircraft {_safe(aircraft.get('full_registration'))}, {_safe(aircraft.get('variant'))}.",
        "Completed work order — historical analysis."
        if row.get("is_closed")
        else (
            "Historical replay of the uploaded snapshot."
            if analysis["input_mode"] == "historical_replay"
            else "Open work order — analysis of the uploaded snapshot."
        ),
        f"Analysis cutoff: {_safe(analysis['analysis_as_of'])}.",
        "",
        "**Target parts**",
    ]
    lines += _target_part_lines(analysis)
    pma = analysis["pma"]
    lines.extend(_pma_lines(pma))
    symptoms = context["symptoms"]
    lines.extend(["", "**Reported findings**"])
    for symptom in symptoms[:10]:
        lines.append(
            f"- {_safe(symptom.get('description') or symptom.get('headline'))}"
        )
    if not symptoms:
        lines.append("No symptom text is available at this cutoff.")
    if len(symptoms) > 10:
        lines.append(f"Showing 10 of {len(symptoms)} work-step findings.")
    actions = row["recorded_actions"]
    if actions:
        lines.extend(["", "**Recorded maintenance actions**"])
        for action in actions[:10]:
            lines.append(f"- {_safe(action['performed_at'])}: {_safe(action['text'])}")
        if len(actions) > 10:
            lines.append(f"Showing 10 of {len(actions)} actions.")
        changes = [c for a in actions for c in a["component_changes"]]
        if changes:
            lines.extend(
                [
                    "",
                    "**Recorded component changes**",
                    "",
                    "| Part off | Serial off | Part on | Serial on | Recorded position |",
                    "|---|---|---|---|---|",
                ]
            )
            for change in changes[:30]:
                lines.append(
                    "| "
                    + " | ".join(
                        _safe(change.get(k))
                        for k in (
                            "part_off_number",
                            "part_off_serial",
                            "part_on_number",
                            "part_on_serial",
                            "position",
                        )
                    )
                    + " |"
                )
            lines.append(
                f"\nShowing {min(30, len(changes))} of {len(changes)} changes. Recorded positions are not inferred from narrative nozzle numbers."
            )
    current = context["current_aircraft_tac"]
    closing = context["closing"]["tac"]
    lines.extend(
        [
            "",
            _current_tac_line(current, analysis.get("pma") or {}),
            f"Closing aircraft TAC: {_safe(closing['value'])}; this is not the current counter or component age.",
        ]
    )
    stale_tac_line = _pma_stale_tac_line(pma)
    if stale_tac_line:
        lines.append(stale_tac_line)
    if analysis["limitations"]:
        lines.extend(["", *[_safe(item) for item in analysis["limitations"]]])
    if _needs_target_selection(analysis):
        lines.append("\nTo select a target, reply target_part_number=PN.")
    lines.append(
        "\nFor a different historical cutoff, reply analysis_as_of=YYYY-MM-DDTHH:MM:SSZ."
    )
    body = "\n\n".join(lines[:4]) + "\n" + "\n".join(lines[4:])
    rec_lines = _recommendation_block_lines(analysis)
    if not rec_lines:
        return body
    return "\n".join(rec_lines) + "\n\n" + body
