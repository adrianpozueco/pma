"""Session-scoped XML attachment analysis for ADK chat.

This first upload slice is deterministic: uploaded work orders are never
replaced by a database lookup and their text is not sent to a chat model.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from google.adk.artifacts.artifact_util import parse_artifact_uri
from google.genai import types

from amos_data.parser import MAX_XML_BYTES, ParseError, parse_workorders
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
            WorkOrderAnalysisService().analyze_xml,
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
    lines += [
        f"- {_safe(t['part_number'])}: {_safe(t['description'])} ({_safe(t['resolution_status'])})."
        for t in analysis["target_parts"]
    ]
    if not analysis["target_parts"]:
        lines.append("No configured target part could be resolved at this cutoff.")
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
            f"Current aircraft TAC: {_safe(current['value'])} ({_safe(current['status'])}).",
            f"Closing aircraft TAC: {_safe(closing['value'])}; this is not the current counter or component age.",
            "",
            "Failure probability, remaining life and replacement deadline are unavailable. No validated model or replacement policy is configured.",
            "",
            "Source: the uploaded XML. BigQuery and the knowledge base were not queried for this attachment analysis.",
        ]
    )
    if analysis["limitations"]:
        lines.extend(["", *[_safe(item) for item in analysis["limitations"]]])
    if len(analysis["target_parts"]) != 1 or any(
        t["resolution_status"] != "resolved" for t in analysis["target_parts"]
    ):
        lines.append("\nTo select a target, reply target_part_number=PN.")
    lines.append(
        "\nFor a different historical cutoff, reply analysis_as_of=YYYY-MM-DDTHH:MM:SSZ."
    )
    return "\n\n".join(lines[:4]) + "\n" + "\n".join(lines[4:])
