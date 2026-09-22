"""Safe, as-of work-order analysis built on the shared AMOS parser."""

from __future__ import annotations

import inspect
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from amos_data import ParseError, parse_workorders
from amos_data.parser import part_key

TARGET_PARTS: dict[str, dict[str, Any]] = {
    "2085M31G03": {
        "description": "Fuel-injection nozzle",
        "aliases": ("fuel injection nozzle", "fuel nozzle"),
        "ipc_references": [
            "data/ipc_part_numbers/M73-82/73-11___042.pdf, 73-11-05 Fig.01 item 20"
        ],
    },
    "62197301001": {
        "description": "Water boiler",
        "aliases": ("water boiler", "water heater"),
        "ipc_references": [
            "data/ipc_part_numbers/M73-82/25-32___042.pdf, 25-32-22 Fig.12M item 10"
        ],
    },
    "820111000001": {
        "description": "Convection oven assembly",
        "aliases": ("convection oven", "oven"),
        "ipc_references": [
            "MAX 25-32-03 Fig.33 item 20",
            "B737-8/25-31___124.pdf, 25-31-11 Fig.15E item 180",
        ],
    },
}
VALID_MODES = {"new_work_order", "historical_replay"}


class WorkOrderAnalysisError(ValueError):
    """A stable, safe input error for the upload surface."""

    def __init__(
        self, message: str, *, code: str, details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class HistoryProvider(Protocol):
    def search(self, query: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class AnalysisInput:
    mode: str
    analysis_as_of: str
    selected_wo_id: str | None = None
    current_aircraft_tac: int | None = None
    current_tac_source: str | None = None
    current_tac_observed_at: str | None = None
    target_part_number: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise WorkOrderAnalysisError(
                "mode must be new_work_order or historical_replay", code="invalid_mode"
            )
        _aware_timestamp(self.analysis_as_of, "analysis_as_of")
        if self.current_aircraft_tac is not None and (
            type(self.current_aircraft_tac) is not int or self.current_aircraft_tac < 0
        ):
            raise WorkOrderAnalysisError(
                "current_aircraft_tac must be a non-negative integer",
                code="invalid_tac",
            )
        if self.current_tac_observed_at:
            _aware_timestamp(self.current_tac_observed_at, "current_tac_observed_at")
        if (
            self.target_part_number
            and part_key(self.target_part_number) not in TARGET_PARTS
        ):
            raise WorkOrderAnalysisError(
                "target_part_number is outside the configured component scope",
                code="out_of_scope_target",
            )


def _aware_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise WorkOrderAnalysisError(
            f"{field} must be an ISO-8601 timestamp with timezone",
            code="invalid_timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkOrderAnalysisError(
            f"{field} must include a timezone", code="timezone_required"
        )
    return parsed.astimezone(UTC)


def _iso_at_or_before(value: str | None, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        return _aware_timestamp(value, "source timestamp") <= cutoff
    except WorkOrderAnalysisError:
        return False


def _counter(
    value: int | None, source: str | None, observed_at: str | None, status: str
) -> dict[str, Any]:
    return {
        "value": value,
        "source": source,
        "observed_at": observed_at,
        "status": status,
    }


class WorkOrderAnalysisService:
    """Parse one upload and produce retrieval-safe, explicit analysis context.

    This slice intentionally does not synthesize a risk, timing forecast, or
    replacement recommendation.  Those require a validated model and policy.
    """

    def __init__(self, history_provider: HistoryProvider | None = None):
        self.history_provider = history_provider

    def analyze_xml(
        self,
        xml_bytes: bytes,
        request: AnalysisInput,
        *,
        source_name: str | None = None,
        artifact_version: str | None = None,
    ) -> dict[str, Any]:
        try:
            parsed = parse_workorders(
                xml_bytes, source_name=source_name, artifact_version=artifact_version
            )
        except ParseError as exc:
            raise WorkOrderAnalysisError(
                str(exc), code=exc.code, details=exc.details
            ) from exc

        workorder = self._select(parsed.workorders, request.selected_wo_id)
        analysis_at = _aware_timestamp(request.analysis_as_of, "analysis_as_of")
        if (
            request.current_tac_observed_at
            and _aware_timestamp(
                request.current_tac_observed_at, "current_tac_observed_at"
            )
            > analysis_at
        ):
            raise WorkOrderAnalysisError(
                "current TAC observation is after analysis_as_of",
                code="future_current_tac",
            )
        if request.mode == "new_work_order" and _is_closed(workorder):
            raise WorkOrderAnalysisError(
                "a closed work-order export requires historical_replay mode",
                code="replay_required",
            )

        targets = self._resolve_targets(
            workorder, request.target_part_number, analysis_at, request.mode
        )
        context, query_text = self._context(workorder, request, analysis_at)
        history = self._history(query_text, context, targets, request, workorder)
        limitations = list(context.pop("limitations"))
        if not targets:
            limitations.append(
                "No configured target part was resolved from the uploaded work order."
            )
        if not query_text:
            limitations.append(
                "No symptom text is safely available at the requested analysis timestamp."
            )
        return {
            "request_id": parsed.upload_hash[:24],
            "upload_hash": parsed.upload_hash,
            "wo_id": workorder.get("workorder_uuid")
            or workorder.get("workorder_number"),
            "input_mode": request.mode,
            "analysis_as_of": request.analysis_as_of,
            "parsed_context": context,
            "target_parts": targets,
            "historical_cases": history["cases"],
            "retrieval": history["retrieval"],
            "replacement_links": [],
            "prediction": _prediction_unavailable(targets, context),
            "timing": _timing_unavailable(targets, context),
            "historical_summary": _unavailable(
                "unavailable",
                "No reviewed episode summary was returned for this request.",
            ),
            "replacement_recommendation": {
                "status": "unavailable",
                "reason": "No reviewed replacement policy or documented cycle limit is configured.",
                "value": None,
                "cycles_remaining": None,
                "due_counter": None,
                "basis": None,
                "applicability": None,
            },
            "manual_evidence": [
                {
                    "part_number": item["part_number"],
                    "ipc_references": TARGET_PARTS[item["part_key"]]["ipc_references"],
                    "status": "local_catalogue_reference",
                    "applicability": "unverified",
                }
                for item in targets
            ],
            "limitations": limitations,
            "parser_diagnostics": parsed.diagnostics,
        }

    @staticmethod
    def _select(rows: list[dict[str, Any]], selected: str | None) -> dict[str, Any]:
        if len(rows) == 1 and not selected:
            return rows[0]
        if not selected:
            raise WorkOrderAnalysisError(
                "selected_wo_id is required when the upload contains multiple work orders",
                code="workorder_selection_required",
                details={
                    "available_wo_ids": [
                        r.get("workorder_uuid") or r.get("workorder_number")
                        for r in rows
                    ]
                },
            )
        matches = [
            r
            for r in rows
            if selected in {r.get("workorder_uuid"), r.get("workorder_number")}
        ]
        if len(matches) != 1:
            raise WorkOrderAnalysisError(
                "selected_wo_id did not identify exactly one work order",
                code="unknown_workorder",
            )
        return matches[0]

    @staticmethod
    def _resolve_targets(
        row: dict[str, Any], requested: str | None, analysis_at: datetime, mode: str
    ) -> list[dict[str, Any]]:
        found: dict[str, set[str]] = {}

        def add(value: str | None, role: str) -> None:
            key = part_key(value)
            if key in TARGET_PARTS:
                found.setdefault(key, set()).add(role)

        closed_before_export = _is_closed(row) and not _iso_at_or_before(
            (row.get("envelope") or {}).get("envelope_ts"), analysis_at
        )
        if not closed_before_export:
            add(row.get("component", {}).get("part_number"), "component")
        for step in row.get("work_steps", []):
            is_available = _iso_at_or_before(step.get("ts"), analysis_at) or (
                mode == "new_work_order" and not step.get("ts")
            )
            if not closed_before_export and is_available:
                for part in step.get("required_parts", []):
                    add(part.get("part_number"), "requested")
            for action in step.get("actions", []):
                if (
                    mode == "historical_replay"
                    and not closed_before_export
                    and _iso_at_or_before(action.get("performed_ts"), analysis_at)
                ):
                    for change in action.get("component_changes", []):
                        add(change.get("part_off_number"), "off")
                        add(change.get("part_on_number"), "on")
        text_fields = (
            []
            if closed_before_export
            else [
                (step.get("description") or "") + " " + (step.get("headline") or "")
                for step in row.get("work_steps", [])
                if _iso_at_or_before(step.get("ts"), analysis_at)
                or (mode == "new_work_order" and not step.get("ts"))
            ]
        )
        for key in TARGET_PARTS:
            if any(_pn_in_text(key, value) for value in text_fields):
                found.setdefault(key, set()).add("mentioned")
            if any(
                re.search(
                    rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", value.lower()
                )
                for value in text_fields
                for alias in TARGET_PARTS[key]["aliases"]
            ):
                found.setdefault(key, set()).add("alias_candidate")
        if requested:
            key = part_key(requested)
            found.setdefault(key, set()).add("user_selected")
        return [
            {
                "part_number": _display_pn(key),
                "part_key": key,
                "description": TARGET_PARTS[key]["description"],
                "resolution_status": "candidate_alias"
                if roles == {"alias_candidate"}
                else "resolved",
                "roles": sorted(roles),
                "supporting_input": sorted(roles),
            }
            for key, roles in sorted(found.items())
        ]

    @staticmethod
    def _context(
        row: dict[str, Any], request: AnalysisInput, analysis_at: datetime
    ) -> tuple[dict[str, Any], str]:
        issue = dict(row.get("issue") or {})
        closing = dict(row.get("closing") or {})
        symptoms: list[dict[str, Any]] = []
        limitations: list[str] = []
        closed_before_export = _is_closed(row) and not _iso_at_or_before(
            (row.get("envelope") or {}).get("envelope_ts"), analysis_at
        )
        for step in row.get("work_steps", []):
            # A timestamp after the as-of cutoff cannot enter an as-of query.
            available = _iso_at_or_before(step.get("ts"), analysis_at)
            if step.get("ts") is None:
                available = request.mode == "new_work_order"
            if (
                available
                and not closed_before_export
                and (step.get("description") or step.get("headline"))
            ):
                symptoms.append(
                    {
                        "step_id": step.get("uuid"),
                        "description": step.get("description"),
                        "headline": step.get("headline"),
                        "observed_at": step.get("ts"),
                        "availability": "snapshot_only"
                        if _is_closed(row)
                        else "reported_observation_unverified",
                    }
                )
        if _is_closed(row):
            limitations.append(
                "Closed-export symptom text is an unverified snapshot of the original observation."
            )
        if closed_before_export:
            limitations.append(
                "Closed-export text is excluded because the requested replay precedes a verified export timestamp."
            )
        if request.mode == "historical_replay":
            limitations.append(
                "Replay excludes completed action and closing text from query and feature context."
            )
            if not _iso_at_or_before(closing.get("ts"), analysis_at):
                closing = {}
            if issue.get("ts") and not _iso_at_or_before(issue.get("ts"), analysis_at):
                issue = {}
        issue_status = "valid" if issue.get("tac") is not None else "missing_or_invalid"
        closing_status = (
            "valid"
            if closing.get("total_aircraft_cycles") is not None
            else "missing_or_invalid"
        )
        current_status = "not_supplied"
        if request.current_aircraft_tac is not None:
            current_status = (
                "valid_as_supplied"
                if request.current_tac_observed_at
                else "unavailable_missing_observed_at"
            )
            if (
                request.current_tac_observed_at
                and (
                    analysis_at
                    - _aware_timestamp(
                        request.current_tac_observed_at, "current_tac_observed_at"
                    )
                ).total_seconds()
                > 86_400
            ):
                current_status = "unavailable_stale_observation"
            if (
                issue.get("tac") is not None
                and request.current_aircraft_tac < issue["tac"]
            ):
                current_status = "invalid_counter_regression"
                limitations.append(
                    "Supplied current TAC is lower than the issue TAC and was not treated as current."
                )
        elif request.mode == "new_work_order":
            limitations.append(
                "Current aircraft TAC was not supplied; issue and closing TAC were not assumed current."
            )
        context = {
            "aircraft": row.get("aircraft"),
            "workorder_state": row.get("workorder_state"),
            "issue": {
                "timestamp": issue.get("ts"),
                "date": issue.get("date"),
                "tac": _counter(
                    issue.get("tac"),
                    "xml_issue",
                    issue.get("ts") or issue.get("date"),
                    issue_status,
                ),
            },
            "closing": {
                "timestamp": closing.get("ts"),
                "date": closing.get("date"),
                "tac": _counter(
                    closing.get("total_aircraft_cycles"),
                    "xml_closing",
                    closing.get("ts") or closing.get("date"),
                    closing_status,
                ),
            },
            "current_aircraft_tac": _counter(
                request.current_aircraft_tac,
                request.current_tac_source,
                request.current_tac_observed_at,
                current_status,
            ),
            "symptoms": symptoms,
            "limitations": limitations,
        }
        query_text = "\n".join(
            filter(None, [s.get("description") or s.get("headline") for s in symptoms])
        )
        return context, query_text

    def _history(
        self,
        text: str,
        context: dict[str, Any],
        targets: list[dict[str, Any]],
        request: AnalysisInput,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        empty = {
            "cases": [],
            "retrieval": {
                "status": "missing_input" if not text else "not_configured",
                "method": None,
                "corpus_version": None,
                "applied_filters": {"analysis_as_of": request.analysis_as_of},
                "returned_cases": 0,
            },
        }
        if not text or self.history_provider is None:
            return empty
        try:
            from amos_data.retrieval import (
                HistoryQuery,  # Added by the retrieval slice.
            )

            query = HistoryQuery(
                symptoms=text,
                analysis_as_of=_aware_timestamp(
                    request.analysis_as_of, "analysis_as_of"
                ),
                target_part_numbers=tuple(t["part_number"] for t in targets),
                aircraft_family=(context.get("aircraft") or {}).get("variant"),
                aircraft_id=(context.get("aircraft") or {}).get("full_registration"),
                ata_code=row.get("ata_code"),
                position=(row.get("position_info") or {}).get("position"),
                exclude_workorder_ids=frozenset(
                    filter(
                        None, [row.get("workorder_uuid"), row.get("workorder_number")]
                    )
                ),
                exclude_text_hashes=frozenset(
                    {_text_hash(text)}
                    | {
                        _text_hash(s.get("description") or s.get("headline") or "")
                        for s in context["symptoms"]
                    }
                ),
                corpus_version=os.getenv("PM_HISTORY_CORPUS"),
                method=os.getenv(
                    "PM_HISTORY_METHOD",
                    "hybrid"
                    if getattr(self.history_provider, "embed_query", None)
                    else "keyword",
                ),
            )
            result = self.history_provider.search(query)
            if inspect.isawaitable(result):
                # The HTTP route is synchronous at this boundary; runtime providers
                # are deliberately synchronous and bounded.
                return {
                    **empty,
                    "retrieval": {
                        **empty["retrieval"],
                        "status": "provider_async_unsupported",
                    },
                }
            if isinstance(result, dict):
                cases = list(result.get("cases", []))
                retrieval = {
                    key: value for key, value in result.items() if key != "cases"
                }
                retrieval.setdefault("status", "ok")
                retrieval.setdefault("method", "provider")
                retrieval.setdefault("returned_cases", len(cases))
                return {"cases": cases, "retrieval": retrieval}
            cases = list(
                getattr(result, "cases", result if isinstance(result, list) else [])
            )
            return {
                "cases": cases,
                "retrieval": {
                    "status": "ok",
                    "method": getattr(result, "method", "provider"),
                    "corpus_version": getattr(result, "corpus_version", None),
                    "applied_filters": {
                        "analysis_as_of": request.analysis_as_of,
                        "exclude_workorder_id": row.get("workorder_uuid"),
                    },
                    "returned_cases": len(cases),
                },
            }
        except (
            Exception
        ) as exc:  # Provider errors are surfaced without uploaded content.
            return {
                **empty,
                "retrieval": {
                    **empty["retrieval"],
                    "status": "query_error",
                    "error_code": type(exc).__name__,
                },
            }


def _unavailable(status: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "value": None,
        "model_version": None,
        "feature_version": None,
    }


def _is_closed(row: dict[str, Any]) -> bool:
    return (row.get("workorder_state") or "").upper() in {"C", "CLOSED", "CLOSE"}


def _display_pn(key: str) -> str:
    return {"62197301001": "62197-301-001", "820111000001": "8201-11-0000-01"}.get(
        key, key
    )


def _pn_in_text(key: str, value: str) -> bool:
    pattern = (
        r"(?<![A-Z0-9])" + r"[^A-Z0-9]*".join(map(re.escape, key)) + r"(?![A-Z0-9])"
    )
    return bool(re.search(pattern, value.upper()))


def _text_hash(value: str) -> str:
    from amos_data.documents import normalize_text, text_hash

    return text_hash(normalize_text(value))


def _prediction_unavailable(
    targets: list[dict[str, Any]], context: dict[str, Any]
) -> dict[str, Any]:
    current = context["current_aircraft_tac"]
    common = {
        "target_event": "confirmed_component_failure" if targets else None,
        "origin_timestamp": None,
        "origin_tac": None,
        "horizon_cycles": None,
        "probability": None,
        "training_cutoff": None,
        "applicability": None,
    }
    if not targets:
        return {
            **_unavailable("out_of_scope", "No configured target part was resolved."),
            **common,
        }
    if len(targets) != 1 or targets[0]["resolution_status"] != "resolved":
        return {
            **_unavailable(
                "missing_input", "Resolve the target component before prediction."
            ),
            **common,
        }
    if not context["symptoms"]:
        return {
            **_unavailable(
                "missing_input",
                "No symptom description is available at the analysis time.",
            ),
            **common,
        }
    if current["status"] != "valid_as_supplied":
        return {
            **_unavailable(
                "missing_input",
                "A valid, observed current aircraft TAC is required before timing features can be constructed.",
            ),
            **common,
        }
    return {
        **_unavailable(
            "model_unavailable",
            "No validated failure-risk model is configured for this fixed corpus.",
        ),
        **common,
        "origin_timestamp": current["observed_at"],
        "origin_tac": current["value"],
    }


def _timing_unavailable(
    targets: list[dict[str, Any]], context: dict[str, Any]
) -> dict[str, Any]:
    valid = (
        bool(targets)
        and context["current_aircraft_tac"]["status"] == "valid_as_supplied"
    )
    return {
        **_unavailable(
            "unavailable" if valid else "missing_input",
            "No validated time-to-failure model is configured."
            if valid
            else "A valid, observed current aircraft TAC is required for timing.",
        ),
        "unit": "aircraft_flight_cycles",
        "estimable_quantiles": None,
        "forecast": None,
    }


def default_history_provider() -> HistoryProvider | None:
    """Build the configured provider only at serving time, never on import."""
    corpus = os.getenv("PM_HISTORY_CORPUS")
    dataset = os.getenv("PM_HISTORY_DATASET", "pma_agent_analytics")
    project = os.getenv("PM_HISTORY_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not corpus:
        return None
    if not project:
        from pm_agent.config import project_id

        project = project_id()
    from google.cloud import bigquery

    from amos_data.embeddings import EmbeddingConfig, VertexEmbeddingClient
    from pm_agent.sub_agents.bq_analytics.history import BQHistoryProvider

    embed_query = None
    if os.getenv("PM_HISTORY_METHOD", "keyword") in {"vector", "hybrid"}:
        config = EmbeddingConfig(
            model_id=os.getenv("PM_EMBEDDING_MODEL", "gemini-embedding-001"),
            model_version=os.getenv("PM_EMBEDDING_VERSION", "001"),
            dimension=int(os.getenv("PM_EMBEDDING_DIMENSION", "3072")),
            corpus_version=corpus,
        )
        embed_query = VertexEmbeddingClient(
            config,
            project=project,
            location=os.getenv("PM_EMBEDDING_LOCATION", "global"),
        ).embed_query
    return BQHistoryProvider(
        bigquery.Client(project=project),
        project=project,
        dataset=dataset,
        embed_query=embed_query,
    )
