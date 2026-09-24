"""Read-only PMA curated-data go-live gate (PMA-ONLINE-AGENT-plan.md §8.4 item 11).

Setting ``PMA_PREDICTION_ENABLED=true`` in production is blocked until this
script passes *and* the T14 backtest's false-gate rate at the chosen
threshold is <= 5% (the second half of the go-live gate; not checked here -
see ``scripts/pma_backtest.py``).

Checks (each prints a ``[PASS]``/``[FAIL]`` line; any ``[FAIL]`` makes the
script exit non-zero):

1. **Curated build presence** - the 7 tables ``policy.REQUIRED_BUILD_TABLES``
   needs are all present in ``INFORMATION_SCHEMA.TABLES`` for
   ``pma_agent_curated`` (`pm_agent.prediction.repository.curated_build_info`).
2. **Build consistency** - `pm_agent.prediction.policy.check_build`: the 6
   downstream tables' creation times span no more than
   ``PMA_BUILD_MAX_SPREAD_S`` seconds among themselves, and no downstream
   table is older than ``fct_replacement_events`` (guards against reading a
   half-refreshed CTAS rebuild, §5.6). ``fct_replacement_events`` itself is
   excluded from the spread - see ``policy.check_build``'s docstring - so a
   legitimate downstream-only rebuild (2026-09-24: ``fct_replacement_events``
   last built 2026-09-23, everything downstream rebuilt together the next
   morning) does not fail this check.
3. **Embedding integrity (768-d/status)** - every row of ``wo_embeddings``
   and ``replacement_anchor_embeddings`` has ``status = ''`` and an embedding
   of exactly ``PMA_EMBEDDING_DIM`` (768) dimensions. The online SQL
   templates already filter on this (§5.3), but a nonzero count here means
   the curated build shipped bad vectors and the filtered result set may be
   silently thin.
4. **Anchor fan-out dedup count** - ``COUNT(DISTINCT replacement_wo_uuid)``
   over ``replacement_anchor_embeddings`` equals ``COUNT(DISTINCT
   replacement_wo_uuid)`` over ``dim_reference_set`` rows with a non-empty
   ``anchor_text`` (2,561 on both sides of the 2026-09-24 live profile, up
   from 718 on 2026-09-23). This is data-derived, not hard-coded: every
   anchor `dim_reference_set` built text for should have exactly one
   embedded counterpart and vice versa, regardless of how many parts are in
   scope, so it stays correct across curated rebuilds without a manual
   override.

Also reported, informational only (never fails the gate): the lead-sample
replacement-to-replacement share per focus component
(``LeadStats.replacement_interval_share``/``chained_sample_n``, §5.5 row 4) -
today's known-thin state (26/28 samples on ``473597-5|AFT`` are
closing-TAC-to-closing-TAC gaps between two consecutive replacements, not
symptom-to-replacement lead times, §"TL;DR"). This is not a pass/fail
condition; the decision policy (`policy.decide`) already routes a component
whose share exceeds ``PMA_MAX_REPLACEMENT_INTERVAL_SHARE`` to
``samples_not_symptom_to_replacement`` at request time. This script prints
it so a human reviews the number before flipping the flag.

Read-only: every check is a ``SELECT`` (or ``INFORMATION_SCHEMA`` read)
through the same allowlisted, dataset-scoped ``QueryRunner`` the online
predictor uses (`pm_agent.sub_agents.bq_analytics.queries`); nothing here
issues DDL/DML or touches Terraform. Import-time side effects are avoided
(``google.cloud.bigquery``/``pm_agent.config`` are only imported inside
``main()``), matching the rest of ``pm_agent.prediction``.

Usage::

    uv run python scripts/pma_data_gate.py
    uv run python scripts/pma_data_gate.py --as-of 2026-09-23T00:00:00Z
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pm_agent.prediction import policy
from pm_agent.prediction.contracts import PredictionError
from pm_agent.prediction.settings import PredictionSettings

_RAW_QUERY_TIMEOUT_S = 30
# Well under the 5 GB runner cap and the "small scans" hard rule - the
# heaviest of these (wo_embeddings.embedding, 4,639 rows x 768 floats) dry-ran
# at a few MB on the live profile.
_RAW_QUERY_MAX_BYTES_BILLED = 200_000_000


@dataclass
class GateCheck:
    name: str
    passed: bool
    detail: str
    informational: bool = False


def _build(
    settings: PredictionSettings,
) -> tuple[Any, Any]:
    """Construct a ``QueryRunner`` and a ``BigQueryPredictionRepository`` on
    top of it, mirroring ``repository.build_default_repository`` exactly but
    keeping the runner reference so this script can also issue the couple of
    read-only aggregate queries no ``pma_*.sql`` template covers (checks 3-4
    above)."""
    from google.cloud import bigquery

    from pm_agent.config import project_id
    from pm_agent.prediction.repository import BigQueryPredictionRepository
    from pm_agent.sub_agents.bq_analytics.queries import DATASET, QueryRunner

    project = settings.bq_project or project_id()
    runner = QueryRunner(
        bigquery.Client(project=project), project=project, dataset=DATASET
    )
    repository = BigQueryPredictionRepository(runner, settings)
    return runner, repository


def _run_raw(
    runner: Any, sql: str, parameters: list[tuple[str, str, Any]]
) -> list[dict[str, Any]]:
    """One read-only aggregate query outside the fixed ``pma_*.sql`` template
    set, through the same allowlisted runner/client and the same billed-bytes
    cap discipline as `QueryRunner.run` (§5, hard rules: SELECT only, small
    scans)."""
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, bq_type, value)
            for name, bq_type, value in parameters
        ],
        maximum_bytes_billed=_RAW_QUERY_MAX_BYTES_BILLED,
        use_query_cache=True,
    )
    job = runner.client.query(
        sql,
        job_config=job_config,
        location=runner.location,
        timeout=_RAW_QUERY_TIMEOUT_S,
    )
    return [dict(row) for row in job.result(timeout=_RAW_QUERY_TIMEOUT_S)]


# --------------------------------------------------------------------------
# Checks (§8.4 item 11)
# --------------------------------------------------------------------------


def check_build_presence(build_info: Any) -> GateCheck:
    """7 curated tables present (`policy.REQUIRED_BUILD_TABLES` against
    `INFORMATION_SCHEMA.TABLES`, via `repository.curated_build_info()`)."""
    creation_times = build_info.table_creation_times
    missing = sorted(t for t in policy.REQUIRED_BUILD_TABLES if t not in creation_times)
    if missing:
        return GateCheck(
            "curated build presence (7 tables)",
            False,
            f"missing from pma_agent_curated: {', '.join(missing)}",
        )
    return GateCheck(
        "curated build presence (7 tables)",
        True,
        f"all present: {', '.join(policy.REQUIRED_BUILD_TABLES)}",
    )


def check_build_consistency(
    build_info: Any, settings: PredictionSettings, *, presence_ok: bool
) -> GateCheck:
    """Build consistency, fail closed (`policy.check_build`, §5.6): the 6
    downstream tables' creation times span <= `PMA_BUILD_MAX_SPREAD_S` among
    themselves, and no downstream table is older than the
    `fct_replacement_events` anchor. `fct_replacement_events` is excluded
    from the spread itself (see `policy.check_build`'s docstring) - it is
    legitimately rebuilt on its own schedule, independent of the 6 tables
    computed from it."""
    name = "build consistency"
    if not presence_ok:
        return GateCheck(
            name, False, "skipped: required tables missing (see previous check)"
        )
    detail = policy.check_build(build_info, max_spread_s=settings.build_max_spread_s)
    if detail:
        return GateCheck(name, False, detail)
    times = {
        t: build_info.table_creation_times[t] for t in policy.REQUIRED_BUILD_TABLES
    }
    anchor = times["fct_replacement_events"]
    downstream_times = {
        t: ts for t, ts in times.items() if t != "fct_replacement_events"
    }
    spread_s = (
        max(downstream_times.values()) - min(downstream_times.values())
    ).total_seconds()
    return GateCheck(
        name,
        True,
        f"downstream creation-time spread {spread_s:.0f}s <= "
        f"{settings.build_max_spread_s}s; no downstream table older than "
        f"fct_replacement_events ({anchor.isoformat()})",
    )


def check_embedding_integrity(runner: Any, settings: PredictionSettings) -> GateCheck:
    """768-d/status checks: every `wo_embeddings.embedding` and
    `replacement_anchor_embeddings.anchor_embedding` row has `status = ''`
    and `ARRAY_LENGTH(result) = PMA_EMBEDDING_DIM` (schema per §3.1's live
    inventory: both columns are `STRUCT<result ARRAY<FLOAT64>, status
    STRING>`, same shape the online `pma_wo_neighbours.sql`/
    `pma_anchor_neighbours.sql` templates filter on)."""
    from pm_agent.sub_agents.bq_analytics.queries import CURATED_DATASET

    name = "embedding integrity (768-d/status)"
    dim = settings.embedding_dim
    per_table: list[tuple[str, int, int]] = []
    for table, column in (
        ("wo_embeddings", "embedding"),
        ("replacement_anchor_embeddings", "anchor_embedding"),
    ):
        fq_table = runner.table(table, dataset=CURATED_DATASET)
        sql = (
            f"SELECT COUNT(*) AS total_n, "
            f"COUNTIF({column}.status != '' OR {column}.result IS NULL "
            f"OR ARRAY_LENGTH({column}.result) != @dim) AS bad_n "
            f"FROM {fq_table}"
        )
        rows = _run_raw(runner, sql, [("dim", "INT64", dim)])
        row = rows[0]
        per_table.append((table, int(row["total_n"]), int(row["bad_n"])))

    detail = "; ".join(
        f"{table}: {bad_n}/{total_n} bad rows (dim={dim})"
        for table, total_n, bad_n in per_table
    )
    passed = all(bad_n == 0 for _table, _total_n, bad_n in per_table)
    return GateCheck(name, passed, detail)


def check_anchor_dedup(runner: Any) -> GateCheck:
    """Anchor fan-out dedup invariant (2026-09-24 data update): replaces the
    old hard-coded "expected 718" count, which broke the moment the curated
    build picked up more parts (2,561 distinct WOs on the current profile).
    `replacement_anchor_embeddings` fans one replacement WO out per
    registered serial; its `COUNT(DISTINCT replacement_wo_uuid)` must equal
    `dim_reference_set`'s `COUNT(DISTINCT replacement_wo_uuid)` restricted to
    rows with a non-empty `anchor_text` - every anchor `dim_reference_set`
    built text for should have exactly one embedded counterpart, and vice
    versa. Data-derived, so it stays correct across curated rebuilds without
    a manual override."""
    from pm_agent.sub_agents.bq_analytics.queries import CURATED_DATASET

    name = "anchor fan-out dedup count"
    anchor_table = runner.table(
        "replacement_anchor_embeddings", dataset=CURATED_DATASET
    )
    reference_table = runner.table("dim_reference_set", dataset=CURATED_DATASET)
    sql = (
        "SELECT "
        f"(SELECT COUNT(DISTINCT replacement_wo_uuid) FROM {anchor_table}) AS embedded_n, "
        "(SELECT COUNT(DISTINCT replacement_wo_uuid) FROM "
        f"{reference_table} WHERE anchor_text IS NOT NULL AND anchor_text != '') AS reference_n"
    )
    rows = _run_raw(runner, sql, [])
    row = rows[0]
    embedded_n = int(row["embedded_n"])
    reference_n = int(row["reference_n"])
    passed = embedded_n == reference_n
    return GateCheck(
        name,
        passed,
        f"replacement_anchor_embeddings COUNT(DISTINCT replacement_wo_uuid) = "
        f"{embedded_n}; dim_reference_set (non-empty anchor_text) "
        f"COUNT(DISTINCT replacement_wo_uuid) = {reference_n}",
    )


def report_lead_time_shares(repository: Any, as_of: datetime) -> GateCheck:
    """Informational, non-gating: per focus component, the lead-sample
    replacement-to-replacement share (`LeadStats.replacement_interval_share`/
    `chained_sample_n`, §5.5 row 4). A human should eyeball this before
    flipping `PMA_PREDICTION_ENABLED` - it is not itself a pass/fail
    condition of item 11; `policy.decide` already applies
    `PMA_MAX_REPLACEMENT_INTERVAL_SHARE` at request time."""
    name = "lead-sample replacement-to-replacement share (informational)"
    try:
        focus_components = repository.focus_components()
    except PredictionError as exc:
        return GateCheck(
            name,
            True,
            f"skipped: could not load focus components ({exc})",
            informational=True,
        )

    lines: list[str] = []
    for fc in focus_components:
        try:
            stats = repository.lead_time_stats(fc.component_key, as_of, [])
        except PredictionError as exc:
            lines.append(f"{fc.component_key}: ERROR reading lead_time_stats ({exc})")
            continue
        share = stats.replacement_interval_share
        share_str = f"{share:.2f}" if share is not None else "n/a"
        lines.append(
            f"{fc.component_key}: n={stats.n} replacement_n={stats.replacement_n} "
            f"chained_sample_n={stats.chained_sample_n} replacement_interval_share={share_str}"
        )
    detail = "\n".join(lines) if lines else "no focus components returned"
    return GateCheck(name, True, detail, informational=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_as_of(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _print_check(check: GateCheck) -> None:
    tag = "[INFO]" if check.informational else ("[PASS]" if check.passed else "[FAIL]")
    print(f"{tag} {check.name}")
    for line in check.detail.splitlines():
        print(f"    {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only PMA curated-data go-live gate "
            "(PMA-ONLINE-AGENT-plan.md Sec 8.4 item 11)."
        )
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help=(
            "ISO-8601 timestamp used as the cutoff for the informational "
            "lead-sample report (default: now, UTC)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        as_of = _parse_as_of(args.as_of)
    except ValueError as exc:
        print(f"[FAIL] invalid --as-of value: {exc}")
        return 2

    settings = PredictionSettings.from_env()

    print("PMA data gate (PMA-ONLINE-AGENT-plan.md Sec 8.4 item 11)")
    print(
        f"  embedding_dim={settings.embedding_dim} "
        f"build_max_spread_s={settings.build_max_spread_s} "
        f"as_of={as_of.isoformat()}"
    )
    print()

    try:
        runner, repository = _build(settings)
    except Exception as exc:  # fail closed, report exact cause
        print(
            f"[FAIL] could not construct BigQuery client (auth/ADC/project resolution): {exc}"
        )
        return 1

    checks: list[GateCheck] = []

    try:
        build_info = repository.curated_build_info()
    except PredictionError as exc:
        checks.append(
            GateCheck(
                "curated build presence (7 tables)",
                False,
                f"could not read pma_agent_curated.INFORMATION_SCHEMA: {exc}",
            )
        )
        checks.append(check_build_consistency(None, settings, presence_ok=False))
    else:
        presence_check = check_build_presence(build_info)
        checks.append(presence_check)
        checks.append(
            check_build_consistency(
                build_info, settings, presence_ok=presence_check.passed
            )
        )

    try:
        checks.append(check_embedding_integrity(runner, settings))
    except Exception as exc:  # fail closed, report exact cause
        checks.append(
            GateCheck(
                "embedding integrity (768-d/status)", False, f"query failed: {exc}"
            )
        )

    try:
        checks.append(check_anchor_dedup(runner))
    except Exception as exc:  # fail closed, report exact cause
        checks.append(
            GateCheck("anchor fan-out dedup count", False, f"query failed: {exc}")
        )

    checks.append(report_lead_time_shares(repository, as_of))

    for check in checks:
        _print_check(check)
        print()

    gating = [c for c in checks if not c.informational]
    failed = [c for c in gating if not c.passed]

    print("-" * 72)
    if failed:
        print(f"GATE: FAIL ({len(failed)}/{len(gating)} gating checks failed)")
        print(
            "Do not set PMA_PREDICTION_ENABLED=true until every gating check "
            "above passes and the T14 backtest's false-gate rate is <= 5%."
        )
        return 1

    print(f"GATE: PASS ({len(gating)}/{len(gating)} gating checks passed)")
    print(
        "Curated-data preconditions for go-live are met. This script does not "
        "check the T14 false-gate rate; confirm that separately before "
        "setting PMA_PREDICTION_ENABLED=true."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
