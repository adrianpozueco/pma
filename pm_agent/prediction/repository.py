"""Live BigQuery ``Repository`` implementation (§7 of the plan).

``BigQueryPredictionRepository`` composes (never subclasses) a
``bq_analytics.queries.QueryRunner``: every method here fills one of the
fixed ``sql/pma_*.sql`` templates through ``runner.table(...)``/``runner.run(
...)`` and maps the resulting rows onto the frozen value objects in
``contracts.py``. It never binds SQL text from model/user input - only real
BigQuery named parameters travel with a value, and only the runner's own
allowlisted, code-owned table identifiers are substituted into a template's
``{placeholder}``.

Import-time side effects are forbidden (mirrors ``contracts.py``/
``settings.py``): no ``google.auth``, no ``google.cloud.bigquery`` and no
``pm_agent.config`` at module scope. ``build_default_repository()`` is the
only function that touches any of those, and only when called.

``max_tac_before_cutoff`` side channel
---------------------------------------
The frozen ``Repository.latest_closing_tac(reg, as_of, exclude_uuid) ->
CurrentTac | None`` Protocol has no field for the ``MAX(tac) OVER ()`` value
``pma_latest_closing_tac.sql`` also returns on every row (needed by
``policy.resolve_current_tac``'s ``latest_closing_max_tac_before_cutoff``
keyword to detect ``counter_inconsistent``). Rather than smuggle it onto
``CurrentTac`` (frozen, owned by T04) or a stateful instance attribute (racy
if a caller ever reuses one repository across concurrent requests), this
class exposes it as a second, Protocol-external method,
``latest_closing_max_tac_before_cutoff(reg, as_of, exclude_uuid)``, with the
exact same signature and semantics (``None`` when there is no eligible prior
work order). The orchestrator (T09) calls both after computing the same
``reg``/``as_of``/``exclude_uuid`` triple; the runner's ``use_query_cache``
means the second call is a cache hit, not a second billed scan.

``latest_closing_tac`` itself returns a *raw* candidate: only ``.value`` and
``.observed_at`` are meaningful (``stale``/``counter_inconsistent`` are left
at their dataclass defaults of ``False``). Per ``policy.py``'s module
docstring, ``policy.resolve_current_tac`` is the one true place that turns a
raw candidate plus ``latest_closing_max_tac_before_cutoff`` into the final
``stale``/``counter_inconsistent`` flags - this repository does not
pre-compute them.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from pm_agent.prediction.contracts import (
    BuildInfo,
    CurrentTac,
    DataSourceUnavailable,
    EmbeddingFailed,
    FocusComponent,
    JobInfo,
    LastReplacement,
    LeadStats,
    Neighbour,
    NeighbourLeadSample,
    PrecursorEvidence,
)
from pm_agent.prediction.settings import PredictionSettings
from pm_agent.sub_agents.bq_analytics.queries import (
    CURATED_DATASET,
    DATASET,
    VIEW_WORK_ORDERS_TABLE,
    WORKORDERS_TABLE,
    QueryRunner,
)
from pm_agent.workorders.evidence import SourceStatus

__all__ = ["BigQueryPredictionRepository", "build_default_repository"]

# `pma_curated_build_info.sql`/`pma_focus_components.sql` never take a
# meaningful `@limit` (the templates say so in their own header comments);
# these are just generous, well-inside-``MAX_LIMIT`` values passed through
# `runner.run(..., limit=...)` because the runner always binds one.
_BUILD_INFO_LIMIT = 10
_FOCUS_COMPONENTS_LIMIT = 100
_EMBED_LIMIT = 1
_LEAD_STATS_LIMIT = 1
_LATEST_CLOSING_TAC_LIMIT = 1
_LAST_REPLACEMENT_LIMIT = 1
# Condensed recommendation (approved 2026-09-24): one neighbour set
# (`rec_neighbour_k`, default 100) can each contribute multiple
# `fct_lead_time_samples` rows, so this is generous rather than 1:1 with k.
_NEIGHBOUR_LEAD_SAMPLES_LIMIT = 1000

_FAILURE_STATUSES: frozenset[SourceStatus] = frozenset(
    {
        SourceStatus.PERMISSION_DENIED,
        SourceStatus.TIMEOUT,
        SourceStatus.UNAVAILABLE,
        SourceStatus.ERROR,
    }
)


def _clean_uuids(values: list[str]) -> list[str]:
    """Never bind ``None``/blank elements into an ``ARRAY<STRING>`` parameter."""
    return [v for v in values if v]


class BigQueryPredictionRepository:
    """The live ``Repository`` (structurally satisfies
    ``pm_agent.prediction.contracts.Repository``; no explicit subclassing -
    the Protocol is checked structurally).

    ``runner`` is an already-constructed ``QueryRunner`` (production code
    goes through :func:`build_default_repository`; tests inject a runner
    built on a fake client, exactly like ``test_bq_predefined_queries.py``).
    """

    def __init__(self, runner: QueryRunner, settings: PredictionSettings) -> None:
        self._runner = runner
        self._settings = settings
        self._job_log: list[JobInfo] = []
        self._focus_cache: tuple[float, list[FocusComponent]] | None = None
        self._build_info_cache: tuple[float, BuildInfo] | None = None

    # -- Repository Protocol -------------------------------------------------

    def curated_build_info(self) -> BuildInfo:
        cached = self._build_info_cache
        if cached is not None:
            cached_at, value = cached
            if time.monotonic() - cached_at < self._settings.focus_cache_ttl_s:
                return value
        outcome = self._run(
            "pma_curated_build_info",
            [],
            limit=_BUILD_INFO_LIMIT,
            table_placeholders={
                "curated_information_schema_tables": self._runner.table(
                    "INFORMATION_SCHEMA.TABLES", dataset=CURATED_DATASET
                ),
                "curated_information_schema_table_options": self._runner.table(
                    "INFORMATION_SCHEMA.TABLE_OPTIONS", dataset=CURATED_DATASET
                ),
            },
        )
        table_creation_times = {
            row["table_name"]: row["creation_time"] for row in outcome.rows
        }
        value = BuildInfo(table_creation_times=table_creation_times)
        self._build_info_cache = (time.monotonic(), value)
        return value

    def focus_components(self) -> list[FocusComponent]:
        cached = self._focus_cache
        if cached is not None:
            cached_at, value = cached
            if time.monotonic() - cached_at < self._settings.focus_cache_ttl_s:
                return value
        outcome = self._run(
            "pma_focus_components",
            [],
            limit=_FOCUS_COMPONENTS_LIMIT,
            table_placeholders={
                "dim_focus_components": self._runner.table(
                    "dim_focus_components", dataset=CURATED_DATASET
                ),
                "fct_replacement_events": self._runner.table(
                    "fct_replacement_events", dataset=CURATED_DATASET
                ),
            },
        )
        value = [
            FocusComponent(
                component_key=row["component_key"],
                part_number=row["part_number"],
                position=row.get("position"),
                part_key=row["part_key"],
                part_aliases=tuple(row.get("part_aliases") or ()),
                freq_rank=row["freq_rank"],
                replacement_count=row["replacement_count"],
                aircraft_with_replacement=row["aircraft_with_replacement"],
            )
            for row in outcome.rows
        ]
        self._focus_cache = (time.monotonic(), value)
        return value

    def embed_query(self, wo_text: str) -> list[float]:
        outcome = self._run(
            "pma_embed_query",
            [("wo_text", "STRING", wo_text)],
            limit=_EMBED_LIMIT,
            template_constants={
                "embedding_endpoint": self._settings.embedding_endpoint
            },
        )
        if not outcome.rows:
            raise EmbeddingFailed("AI.EMBED returned no row", detail="no_row")
        row = outcome.rows[0]
        status = row.get("status") or ""
        embedding = list(row.get("embedding") or [])
        if status != "":
            raise EmbeddingFailed(
                "AI.EMBED returned a non-empty status", detail=f"status:{status}"
            )
        if len(embedding) != self._settings.embedding_dim:
            raise EmbeddingFailed(
                "AI.EMBED returned the wrong dimension",
                detail=f"dim:{len(embedding)}",
            )
        return embedding

    def anchor_neighbours(
        self,
        emb: list[float],
        as_of: datetime,
        exclude: list[str],
        k: int,
        min_sim: float,
    ) -> list[Neighbour]:
        outcome = self._run(
            "pma_anchor_neighbours",
            [
                ("query_embedding", "ARRAY<FLOAT64>", list(emb)),
                ("exclude_wo_uuids", "ARRAY<STRING>", _clean_uuids(list(exclude))),
                ("analysis_as_of", "TIMESTAMP", as_of),
                ("min_sim", "FLOAT64", min_sim),
            ],
            limit=k,
            table_placeholders={
                "replacement_anchor_embeddings": self._runner.table(
                    "replacement_anchor_embeddings", dataset=CURATED_DATASET
                ),
                "dim_reference_set": self._runner.table(
                    "dim_reference_set", dataset=CURATED_DATASET
                ),
            },
        )
        return [
            Neighbour(
                wo_uuid=row["replacement_wo_uuid"],
                wo_id=row.get("replacement_wo_id"),
                aircraft_reg=row.get("aircraft_reg"),
                sim=row["sim"],
                snippet=row.get("snippet") or "",
                component_key=row.get("component_key"),
                replacement_tac=row.get("replacement_tac"),
                replacement_date=_as_date(row.get("replacement_date")),
            )
            for row in outcome.rows
        ]

    def wo_neighbours(
        self,
        emb: list[float],
        as_of: datetime,
        exclude: list[str],
        k: int,
        min_sim: float,
    ) -> list[Neighbour]:
        outcome = self._run(
            "pma_wo_neighbours",
            [
                ("query_embedding", "ARRAY<FLOAT64>", list(emb)),
                ("exclude_wo_uuids", "ARRAY<STRING>", _clean_uuids(list(exclude))),
                ("analysis_as_of", "TIMESTAMP", as_of),
                ("min_sim", "FLOAT64", min_sim),
            ],
            limit=k,
            table_placeholders={
                "wo_embeddings": self._runner.table(
                    "wo_embeddings", dataset=CURATED_DATASET
                ),
                "wo_workorders": self._runner.table(WORKORDERS_TABLE),
            },
        )
        return [
            Neighbour(
                wo_uuid=row["wo_uuid"],
                wo_id=row.get("wo_id"),
                aircraft_reg=row.get("aircraft_reg"),
                sim=row["sim"],
                snippet=row.get("snippet") or "",
                ata_chapter=row.get("ata_chapter"),
                tac=row.get("tac"),
                closing_date=_as_date(row.get("closing_date")),
            )
            for row in outcome.rows
        ]

    def lead_time_stats(
        self, component_key: str, as_of: datetime, exclude: list[str]
    ) -> LeadStats:
        outcome = self._run(
            "pma_lead_time_stats",
            [
                ("component_key", "STRING", component_key),
                ("analysis_as_of", "TIMESTAMP", as_of),
                ("exclude_wo_uuids", "ARRAY<STRING>", _clean_uuids(list(exclude))),
            ],
            limit=_LEAD_STATS_LIMIT,
            table_placeholders={
                "fct_lead_time_samples": self._runner.table(
                    "fct_lead_time_samples", dataset=CURATED_DATASET
                ),
                "fct_replacement_events": self._runner.table(
                    "fct_replacement_events", dataset=CURATED_DATASET
                ),
            },
        )
        # The template always returns exactly one row (aggregate, no GROUP
        # BY); n=0/NULL stats is the "no samples" signal here, never the
        # runner's NO_MATCH status (see the SQL file's own header comment).
        if not outcome.rows:
            return LeadStats(component_key=component_key, n=0)
        row = outcome.rows[0]
        return LeadStats(
            component_key=component_key,
            n=row.get("n") or 0,
            aircraft_n=row.get("aircraft_n") or 0,
            replacement_n=row.get("replacement_n") or 0,
            lead_min=row.get("lead_min"),
            lead_max=row.get("lead_max"),
            lead_p50=row.get("lead_p50"),
            lead_p90=row.get("lead_p90"),
            lead_p95=row.get("lead_p95"),
            lead_mean=row.get("lead_mean"),
            lead_sd=row.get("lead_sd"),
            cv=row.get("cv"),
            replacement_interval_share=row.get("replacement_interval_share"),
            chained_sample_n=row.get("chained_sample_n") or 0,
            replacement_wo_uuids=tuple(row.get("replacement_wo_uuids") or ()),
        )

    def precursor_evidence(
        self,
        component_key: str,
        as_of: datetime,
        exclude: list[str],
        neighbour_uuids: list[str],
        limit: int,
    ) -> list[PrecursorEvidence]:
        outcome = self._run(
            "pma_precursor_evidence",
            [
                ("component_key", "STRING", component_key),
                ("analysis_as_of", "TIMESTAMP", as_of),
                ("exclude_wo_uuids", "ARRAY<STRING>", _clean_uuids(list(exclude))),
                (
                    "neighbour_wo_uuids",
                    "ARRAY<STRING>",
                    _clean_uuids(list(neighbour_uuids)),
                ),
            ],
            limit=limit,
            table_placeholders={
                "fct_lead_time_samples": self._runner.table(
                    "fct_lead_time_samples", dataset=CURATED_DATASET
                ),
                "adjudicated_precursors": self._runner.table(
                    "adjudicated_precursors", dataset=CURATED_DATASET
                ),
                "fct_replacement_events": self._runner.table(
                    "fct_replacement_events", dataset=CURATED_DATASET
                ),
            },
        )
        return [
            PrecursorEvidence(
                component_key=row["component_key"],
                aircraft_reg=row.get("aircraft_reg"),
                precursor_wo_id=row.get("precursor_wo_id"),
                precursor_wo_uuid=row["precursor_wo_uuid"],
                precursor_tac=row.get("precursor_tac"),
                replacement_wo_uuid=row["replacement_wo_uuid"],
                replacement_tac=row.get("replacement_tac"),
                replacement_date=_as_date(row.get("replacement_date")),
                lead_cycles=row.get("lead_cycles"),
                sim=row.get("sim"),
                reason=row.get("reason"),
                precursor_snippet=row.get("precursor_snippet"),
                is_neighbour_hit=bool(row.get("is_neighbour_hit") or False),
            )
            for row in outcome.rows
        ]

    def neighbour_lead_samples(
        self, uuids: list[str], as_of: datetime, exclude_uuid: str
    ) -> list[NeighbourLeadSample]:
        """Condensed recommendation (approved 2026-09-24, USER REQUEST: base
        the recommendation on `wo_embeddings` neighbour matching and
        `fct_lead_time_samples`). `uuids` is the recommendation's own,
        wider-net `wo_neighbours(...)` call (`rec_neighbour_k`/
        `rec_neighbour_min_sim`), not the O6 evidence neighbour list."""
        outcome = self._run(
            "pma_neighbour_lead_samples",
            [
                (
                    "neighbour_wo_uuids",
                    "ARRAY<STRING>",
                    _clean_uuids(list(uuids)),
                ),
                ("analysis_as_of", "TIMESTAMP", as_of),
                ("exclude_wo_uuid", "STRING", exclude_uuid or ""),
            ],
            limit=_NEIGHBOUR_LEAD_SAMPLES_LIMIT,
            table_placeholders={
                "fct_lead_time_samples": self._runner.table(
                    "fct_lead_time_samples", dataset=CURATED_DATASET
                ),
                "v_work_orders": self._runner.table(VIEW_WORK_ORDERS_TABLE),
            },
        )
        return [
            NeighbourLeadSample(
                component_key=row["component_key"],
                precursor_wo_uuid=row["precursor_wo_uuid"],
                aircraft_reg=row.get("aircraft_reg"),
                lead_cycles=row.get("lead_cycles"),
            )
            for row in outcome.rows
        ]

    def latest_closing_tac(
        self, reg: str, as_of: datetime, exclude_uuid: str
    ) -> CurrentTac | None:
        row = self._latest_closing_row(reg, as_of, exclude_uuid)
        if row is None:
            return None
        return CurrentTac(
            value=row["tac"],
            source="latest_closing_tac",
            observed_at=row.get("observed_at"),
        )

    def latest_closing_max_tac_before_cutoff(
        self, reg: str, as_of: datetime, exclude_uuid: str
    ) -> int | None:
        """Protocol-external companion to :meth:`latest_closing_tac` - see
        the module docstring's "max_tac_before_cutoff side channel" section.
        """
        row = self._latest_closing_row(reg, as_of, exclude_uuid)
        if row is None:
            return None
        return row.get("max_tac_before_cutoff")

    def last_replacement(
        self, component_key: str, reg: str, as_of: datetime, exclude_uuid: str
    ) -> LastReplacement | None:
        """Option B projected window (approved 2026-09-24): this aircraft's
        own most recent earlier replacement of ``component_key``. ``None``
        means "no eligible prior replacement", not a failure."""
        outcome = self._run(
            "pma_last_replacement",
            [
                ("component_key", "STRING", component_key),
                ("aircraft_reg", "STRING", reg),
                ("analysis_as_of", "TIMESTAMP", as_of),
                ("exclude_wo_uuid", "STRING", exclude_uuid or ""),
            ],
            limit=_LAST_REPLACEMENT_LIMIT,
            table_placeholders={
                "fct_replacement_events": self._runner.table(
                    "fct_replacement_events", dataset=CURATED_DATASET
                ),
                "v_work_orders": self._runner.table(VIEW_WORK_ORDERS_TABLE),
            },
        )
        if not outcome.rows:
            return None
        row = outcome.rows[0]
        return LastReplacement(
            tac=row["replacement_tac"],
            replacement_date=_as_date(row.get("replacement_date")),
            wo_id=row.get("replacement_wo_id"),
            wo_uuid=row.get("replacement_wo_uuid"),
        )

    @property
    def job_log(self) -> list[JobInfo]:
        return list(self._job_log)

    # -- internal --------------------------------------------------------

    def _latest_closing_row(
        self, reg: str, as_of: datetime, exclude_uuid: str
    ) -> dict[str, Any] | None:
        outcome = self._run(
            "pma_latest_closing_tac",
            [
                ("aircraft_reg", "STRING", reg),
                ("analysis_as_of", "TIMESTAMP", as_of),
                ("exclude_wo_uuid", "STRING", exclude_uuid or ""),
            ],
            limit=_LATEST_CLOSING_TAC_LIMIT,
            table_placeholders={
                "v_work_orders": self._runner.table(VIEW_WORK_ORDERS_TABLE)
            },
        )
        if not outcome.rows:
            return None
        return outcome.rows[0]

    def _run(
        self,
        sql_name: str,
        parameters: list[tuple[str, str, Any]],
        *,
        limit: int,
        table_placeholders: dict[str, str] | None = None,
        template_constants: dict[str, str] | None = None,
    ) -> Any:
        outcome = self._runner.run(
            sql_name,
            parameters,
            limit=limit,
            table_placeholders=table_placeholders,
            template_constants=template_constants,
        )
        self._job_log.append(
            JobInfo(
                job_id=outcome.job_id, total_bytes_billed=outcome.total_bytes_billed
            )
        )
        if outcome.status in _FAILURE_STATUSES:
            raise DataSourceUnavailable(
                f"{sql_name} failed: {outcome.status.value}",
                detail=outcome.error_detail,
            )
        return outcome


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return value


def build_default_repository(
    settings: PredictionSettings | None = None,
) -> BigQueryPredictionRepository:
    """Construct a repository from Application Default Credentials.

    Mirrors ``bq_analytics.queries.build_default_runner`` /
    ``pm_agent.workorders.service.default_history_provider``: built only at
    serving time (never at import time). ``settings.bq_project`` defaults to
    ``None`` ("unresolved") - resolving it via ``pm_agent.config.project_id()``
    when unset is this function's job (see ``settings.py``'s module
    docstring).
    """
    from google.cloud import bigquery

    from pm_agent.config import project_id

    settings = settings or PredictionSettings.from_env()
    project = settings.bq_project or project_id()
    runner = QueryRunner(
        bigquery.Client(project=project), project=project, dataset=DATASET
    )
    return BigQueryPredictionRepository(runner, settings)
