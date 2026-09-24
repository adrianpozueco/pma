"""Bounded, parameterized BigQuery evidence tools (N2 / P2).

This module is the "predefined BigQuery tools" surface described in
``docs/adk-parallel-evidence-plan.md``: fixed SQL templates, packaged under
``sql/`` in this same package, executed through real BigQuery named
parameters. The model supplies tool arguments; it never writes or extends
SQL. Project/dataset/table identifiers come only from the code-owned
allowlists below, never from model or user input.

:class:`QueryRunner` reuses ``BQHistoryProvider``'s conventions from
``bq_analytics/history.py``: the caller injects an already-constructed
``google.cloud.bigquery.Client``, project/dataset are validated up front,
and every query carries a byte budget, a timeout and cancellation-on-timeout.
It is the runner interface later part-history/coverage tools (N3) should
reuse rather than duplicate.

Each tool wraps one query as a :class:`~pm_agent.workorders.evidence.SourceResult`:
``source`` is ``"bigquery.<tool_name>"``; a successful zero-row query is
``NO_MATCH`` (never treated as a failure); permission/timeout/service errors
are mapped to ``PERMISSION_DENIED``/``TIMEOUT``/``UNAVAILABLE``/``ERROR`` and
never silently reported as ``NO_MATCH``; ``error_detail`` carries only an
exception type name, never a message (which can contain SQL text, project
numbers or other credential-adjacent detail).

``get_workorder_actions`` and ``get_component_changes`` are deliberately
separate queries/tools: work orders, actions and component changes are
counted independently (see the per-SQL-file comments in ``sql/`` for how
each avoids cross-producting sibling arrays), and completed actions are kept
out of the component-change/serial-pair evidence used elsewhere as
pre-event symptom features.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from amos_data.parser import part_key as _normalize_part_key
from pm_agent.workorders.evidence import SourceResult, SourceStatus

__all__ = [
    "DATASET",
    "DEFAULT_LIMIT",
    "LOCATION",
    "MAX_LIMIT",
    "WORKORDERS_TABLE",
    "QueryRunner",
    "build_default_runner",
    "get_component_changes",
    "get_workorder",
    "get_workorder_actions",
]

LOCATION = "us-central1"
DATASET = "pma_agent_analytics"
CURATED_DATASET = "pma_agent_curated"
WORKORDERS_TABLE = "wo_workorders"
FAA_TABLE = "faa_sdr_wo_parts"
VIEW_WORK_ORDERS_TABLE = "v_work_orders"

# Code-owned allowlists. These are never populated from model or user input:
# QueryRunner raises rather than building a query against anything outside
# them. N3's get_part_coverage counts FAA reports through this same runner
# (bq_analytics/adapters.py), it does not bypass it. Keyed by dataset so a
# table name is only ever resolved within the dataset it actually lives in
# (the PMA online prediction core reads a second, read-only dataset,
# ``pma_agent_curated``, alongside the existing ``pma_agent_analytics``).
_ALLOWED_TABLES: dict[str, frozenset[str]] = {
    DATASET: frozenset({WORKORDERS_TABLE, FAA_TABLE, VIEW_WORK_ORDERS_TABLE}),
    CURATED_DATASET: frozenset(
        {
            "dim_focus_components",
            "fct_replacement_events",
            "dim_reference_set",
            "replacement_anchor_embeddings",
            "wo_embeddings",
            "adjudicated_precursors",
            "fct_lead_time_samples",
            "INFORMATION_SCHEMA.TABLES",
            "INFORMATION_SCHEMA.TABLE_OPTIONS",
        }
    ),
}
_ALLOWED_DATASETS = frozenset(_ALLOWED_TABLES)

# Template placeholders that are not table identifiers but still must never
# be filled from model/user input - validated against a fixed value set
# before substitution, the same fail-closed pattern as the table allowlist.
_ALLOWED_TEMPLATE_CONSTANTS: dict[str, frozenset[str]] = {
    "embedding_endpoint": frozenset({"text-embedding-005"}),
}

_PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,61}[a-z0-9]")

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAXIMUM_BYTES_BILLED = 5_000_000_000
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

_SQL_DIR = Path(__file__).parent / "sql"


def _load_sql(name: str) -> str:
    """Read one fixed, checked-in SQL template.

    Only the runner's own allowlisted table identifier is ever substituted
    into a template's ``{placeholder}``; user/model-supplied values always
    travel as real BigQuery query parameters, never as text substituted here.
    """
    return (_SQL_DIR / f"{name}.sql").read_text()


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """Internal: one executed query's outcome, before it is shaped into the
    tool-specific :class:`SourceResult`."""

    status: SourceStatus
    rows: tuple[dict[str, Any], ...] = ()
    truncated: bool = False
    error_detail: str | None = None
    job_id: str | None = None
    total_bytes_billed: int | None = None


def _classify_exception(exc: Exception) -> SourceStatus:
    """Map a client/job exception to the right non-NO_MATCH failure status."""
    if isinstance(exc, TimeoutError):
        return SourceStatus.TIMEOUT
    from google.api_core import exceptions as gax_exceptions

    if isinstance(exc, gax_exceptions.Forbidden):
        return SourceStatus.PERMISSION_DENIED
    if isinstance(exc, gax_exceptions.DeadlineExceeded):
        return SourceStatus.TIMEOUT
    if isinstance(
        exc, (gax_exceptions.ServiceUnavailable, gax_exceptions.TooManyRequests)
    ):
        return SourceStatus.UNAVAILABLE
    return SourceStatus.ERROR


class QueryRunner:
    """Bounded, parameterized BigQuery execution shared by every predefined
    evidence tool in this module.

    The caller constructs and injects the ``google.cloud.bigquery.Client``
    (production code should go through :func:`build_default_runner`; tests
    inject a fake/mocked client). ``project``/``dataset`` are validated
    against the allowlists above at construction time, matching
    ``BQHistoryProvider``'s fail-fast behavior in ``bq_analytics/history.py``.
    """

    def __init__(
        self,
        client: Any,
        *,
        project: str,
        dataset: str = DATASET,
        location: str = LOCATION,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        maximum_bytes_billed: int = DEFAULT_MAXIMUM_BYTES_BILLED,
    ) -> None:
        if not _PROJECT_RE.fullmatch(project):
            raise ValueError("use a configured, allowlisted project id")
        if dataset not in _ALLOWED_DATASETS:
            raise ValueError(f"dataset {dataset!r} is not allowlisted")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be within serving limits")
        if not 1 <= maximum_bytes_billed <= 5_000_000_000:
            raise ValueError("maximum_bytes_billed must be within serving limits")
        self.client = client
        self.project = project
        self.dataset = dataset
        self.location = location
        self.timeout_seconds = timeout_seconds
        self.maximum_bytes_billed = maximum_bytes_billed

    def table(self, name: str, *, dataset: str | None = None) -> str:
        """Fully-qualified, backtick-quoted identifier for an allowlisted
        table name. ``dataset`` defaults to this runner's own dataset
        (backward compatible with the single-dataset callers in
        ``adapters.py``); passing a second allowlisted dataset (for example
        ``pma_agent_curated``) lets one runner instance read read-only
        curated tables alongside its primary analytics dataset. Raises for
        anything else, including a name or dataset a caller assembled
        without going through the allowlist."""
        resolved_dataset = self.dataset if dataset is None else dataset
        allowed_names = _ALLOWED_TABLES.get(resolved_dataset)
        if allowed_names is None:
            raise ValueError(f"dataset {resolved_dataset!r} is not allowlisted")
        if name not in allowed_names:
            raise ValueError(
                f"table {name!r} is not allowlisted for dataset {resolved_dataset!r}"
            )
        if name.startswith("INFORMATION_SCHEMA."):
            return f"`{self.project}.{resolved_dataset}`.{name}"
        return f"`{self.project}.{resolved_dataset}.{name}`"

    def run(
        self,
        sql_name: str,
        parameters: list[tuple[str, str, Any]],
        *,
        limit: int,
        table_placeholders: dict[str, str] | None = None,
        template_constants: dict[str, str] | None = None,
    ) -> _RunOutcome:
        """Execute one fixed SQL template with real named parameters.

        ``limit`` is enforced by requesting ``limit + 1`` rows (bound to the
        template's own ``@limit`` parameter) and trimming: this is how
        ``truncated`` is derived without a second COUNT query. A timed-out
        job is cancelled, never left running; every other client/job
        exception is classified to PERMISSION_DENIED/UNAVAILABLE/ERROR. A
        successful query that returns zero rows is NO_MATCH, never a failure
        status.

        ``table_placeholders`` values must come from :meth:`table` (or
        another allowlisted, backtick-quoted identifier); ``template_constants``
        fills a non-table ``{placeholder}`` (for example the embedding
        endpoint) only after checking it against
        ``_ALLOWED_TEMPLATE_CONSTANTS``, so neither ever carries model- or
        user-supplied text into the SQL itself.
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        sql = _load_sql(sql_name)
        for placeholder, value in (table_placeholders or {}).items():
            sql = sql.replace("{" + placeholder + "}", value)
        for placeholder, value in (template_constants or {}).items():
            allowed_values = _ALLOWED_TEMPLATE_CONSTANTS.get(placeholder)
            if allowed_values is None or value not in allowed_values:
                raise ValueError(
                    f"template constant {placeholder!r}={value!r} is not allowlisted"
                )
            sql = sql.replace("{" + placeholder + "}", value)
        bound = [*parameters, ("limit", "INT64", limit + 1)]
        try:
            job = self.client.query(
                sql,
                job_config=self._job_config(bound),
                location=self.location,
                timeout=self.timeout_seconds,
                retry=None,
                job_retry=None,
            )
        except Exception as exc:
            return self._failure(exc)
        try:
            rows = [
                dict(row) for row in job.result(timeout=self.timeout_seconds, retry=None)
            ]
        except TimeoutError as exc:
            job.cancel(timeout=self.timeout_seconds, retry=None)
            return self._failure(exc, job=job)
        except Exception as exc:
            return self._failure(exc, job=job)
        truncated = len(rows) > limit
        rows = rows[:limit]
        status = SourceStatus.SUCCESS if rows else SourceStatus.NO_MATCH
        return _RunOutcome(
            status=status,
            rows=tuple(rows),
            truncated=truncated,
            job_id=getattr(job, "job_id", None),
            total_bytes_billed=getattr(job, "total_bytes_billed", None),
        )

    def _job_config(self, parameters: list[tuple[str, str, Any]]) -> Any:
        from google.cloud import bigquery

        query_parameters: list[Any] = []
        for name, bq_type, value in parameters:
            if bq_type.startswith("ARRAY<") and bq_type.endswith(">"):
                if value is None or any(element is None for element in value):
                    raise ValueError(
                        f"array parameter {name!r} must not be, or contain, None"
                    )
                inner_type = bq_type[len("ARRAY<") : -1]
                query_parameters.append(
                    bigquery.ArrayQueryParameter(name, inner_type, value)
                )
            else:
                query_parameters.append(
                    bigquery.ScalarQueryParameter(name, bq_type, value)
                )
        return bigquery.QueryJobConfig(
            query_parameters=query_parameters,
            maximum_bytes_billed=self.maximum_bytes_billed,
            job_timeout_ms=self.timeout_seconds * 1000,
            use_query_cache=True,
        )

    @staticmethod
    def _failure(exc: Exception, *, job: Any | None = None) -> _RunOutcome:
        status = _classify_exception(exc)
        return _RunOutcome(
            status=status,
            # Exception type name only - never str(exc), which can carry SQL
            # text or other credential-adjacent detail.
            error_detail=f"{status.value}:{type(exc).__name__}",
            job_id=getattr(job, "job_id", None) if job is not None else None,
        )


def build_default_runner() -> QueryRunner:
    """Construct a runner from Application Default Credentials.

    Mirrors ``pm_agent.workorders.service.default_history_provider``: built
    only at serving time (never at import time), and reuses the same
    project-resolution helper.
    """
    from google.cloud import bigquery

    from pm_agent.config import project_id

    project = project_id()
    return QueryRunner(bigquery.Client(project=project), project=project)


def _distinct(rows: tuple[dict[str, Any], ...], key: str) -> tuple[str, ...]:
    return tuple(sorted({str(row[key]) for row in rows if row.get(key)}))


def _blank_parameter_result(source: str, executed_parameters: dict[str, Any]) -> SourceResult:
    return SourceResult(
        source=source,
        status=SourceStatus.ERROR,
        executed_parameters=executed_parameters,
        error_detail="error:blank_workorder_number",
    )


def get_workorder(
    runner: QueryRunner, workorder_number: str, *, limit: int = DEFAULT_LIMIT
) -> SourceResult:
    """Header and recorded top-level descriptions for one work order, with
    source provenance (``source_file``/``source_row_hash``).

    ``source`` is ``"bigquery.get_workorder"``. ``counts["workorder_header_rows"]``
    is the number of header rows returned; it is not combined with action or
    component-change counts from the other two tools in this module.
    """
    workorder_number = (workorder_number or "").strip()
    executed_parameters: dict[str, Any] = {
        "workorder_number": workorder_number,
        "limit": limit,
    }
    if not workorder_number:
        return _blank_parameter_result("bigquery.get_workorder", executed_parameters)
    outcome = runner.run(
        "get_workorder",
        [("workorder_number", "STRING", workorder_number)],
        limit=limit,
        table_placeholders={"workorders_table": runner.table(WORKORDERS_TABLE)},
    )
    return SourceResult(
        source="bigquery.get_workorder",
        status=outcome.status,
        records=outcome.rows,
        source_ids=_distinct(outcome.rows, "workorder_id"),
        executed_parameters=executed_parameters,
        counts={"workorder_header_rows": len(outcome.rows)},
        truncated=outcome.truncated,
        limit=limit,
        error_detail=outcome.error_detail,
    )


def get_workorder_actions(
    runner: QueryRunner, workorder_number: str, *, limit: int = DEFAULT_LIMIT
) -> SourceResult:
    """Recorded action text and times for one work order, kept separate from
    component-change rows and from symptom features.

    ``source`` is ``"bigquery.get_workorder_actions"``.
    ``counts["action_rows"]`` counts actions only; it is independent of
    ``get_component_changes``'s ``component_change_rows``, so a work order
    with e.g. 2 actions and 4 component changes never gets reported as a
    single merged count.
    """
    workorder_number = (workorder_number or "").strip()
    executed_parameters: dict[str, Any] = {
        "workorder_number": workorder_number,
        "limit": limit,
    }
    if not workorder_number:
        return _blank_parameter_result("bigquery.get_workorder_actions", executed_parameters)
    outcome = runner.run(
        "get_workorder_actions",
        [("workorder_number", "STRING", workorder_number)],
        limit=limit,
        table_placeholders={"workorders_table": runner.table(WORKORDERS_TABLE)},
    )
    return SourceResult(
        source="bigquery.get_workorder_actions",
        status=outcome.status,
        records=outcome.rows,
        source_ids=_distinct(outcome.rows, "action_uuid"),
        executed_parameters=executed_parameters,
        counts={"action_rows": len(outcome.rows)},
        truncated=outcome.truncated,
        limit=limit,
        error_detail=outcome.error_detail,
    )


def get_component_changes(
    runner: QueryRunner,
    workorder_number: str,
    part_number: str | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
) -> SourceResult:
    """Exact on/off part-number and serial pairs for one work order.

    ``position`` in each returned record is copied through verbatim from the
    source (``component_changes.position``, e.g. ``"#2"``); it is never
    renumbered or remapped to a narrative position, and several structured
    positions can legitimately be identical for the same work order.

    ``part_number`` is optional and normalized with the same
    ``amos_data.parser.part_key`` normalization used elsewhere in the
    evidence contracts before it is bound as a query parameter; it is matched
    against both the outgoing and incoming part number of each change.

    ``source`` is ``"bigquery.get_component_changes"``.
    ``counts["component_change_rows"]`` counts component-change rows only,
    independent of ``get_workorder_actions``'s action count.
    """
    workorder_number = (workorder_number or "").strip()
    normalized_part = _normalize_part_key(part_number) if part_number else None
    executed_parameters: dict[str, Any] = {
        "workorder_number": workorder_number,
        "part_number": normalized_part,
        "limit": limit,
    }
    if not workorder_number:
        return _blank_parameter_result("bigquery.get_component_changes", executed_parameters)
    outcome = runner.run(
        "get_component_changes",
        [
            ("workorder_number", "STRING", workorder_number),
            ("part_number", "STRING", normalized_part),
        ],
        limit=limit,
        table_placeholders={"workorders_table": runner.table(WORKORDERS_TABLE)},
    )
    return SourceResult(
        source="bigquery.get_component_changes",
        status=outcome.status,
        records=outcome.rows,
        source_ids=_distinct(outcome.rows, "component_change_uuid"),
        executed_parameters=executed_parameters,
        counts={
            "component_change_rows": len(outcome.rows),
            "distinct_workorder_ids": len(_distinct(outcome.rows, "workorder_id")),
        },
        truncated=outcome.truncated,
        limit=limit,
        error_detail=outcome.error_detail,
    )
