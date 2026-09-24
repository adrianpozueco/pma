"""Unit tests for the predefined BigQuery evidence tools (N2 / P2).

Uses a fake/mocked ``google.cloud.bigquery.Client`` throughout - no live
credentials, no network. The fake records the SQL text and query parameters
each tool actually sends, so tests can assert real BigQuery named parameters
are bound (never string-interpolated), that work-order/action/component-change
counts stay independent, and that recorded position/serial data passes
through verbatim.
"""

from __future__ import annotations

import pytest
from google.api_core import exceptions as gax_exceptions

from pm_agent.sub_agents.bq_analytics import queries
from pm_agent.sub_agents.bq_analytics.queries import (
    CURATED_DATASET,
    DATASET,
    WORKORDERS_TABLE,
    QueryRunner,
    get_component_changes,
    get_workorder,
    get_workorder_actions,
)
from pm_agent.workorders.evidence import SourceStatus


class Job:
    def __init__(
        self,
        rows=(),
        timeout=False,
        result_error=None,
        job_id="job-123",
        total_bytes_billed=10_485_760,
    ):
        self.rows = list(rows)
        self.timeout = timeout
        self.result_error = result_error
        self.cancelled = False
        self.job_id = job_id
        self.total_bytes_billed = total_bytes_billed

    def result(self, timeout, **_):
        if self.timeout:
            raise TimeoutError()
        if self.result_error is not None:
            raise self.result_error
        return list(self.rows)

    def cancel(self, **_):
        self.cancelled = True


class Client:
    def __init__(self, jobs=None, query_error=None):
        self.sql_calls: list[str] = []
        self.config_calls: list[tuple] = []
        self.jobs = list(jobs or [])
        self.query_error = query_error

    def query(self, sql, job_config, **kwargs):
        if self.query_error is not None:
            raise self.query_error
        self.sql_calls.append(sql)
        self.config_calls.append((job_config, kwargs))
        return self.jobs.pop(0) if self.jobs else Job()


def runner(client) -> QueryRunner:
    return QueryRunner(client, project="valid-project")


def action_row(uuid, **extra):
    return {
        "workorder_id": "wo-1",
        "workorder_number": "105177647",
        "action_uuid": uuid,
        "action_type": "REPLACE",
        "action_text": "remove and replace nozzle",
        "event_ts": "2026-08-01T00:00:00+00:00",
        **extra,
    }


def change_row(uuid, position="#2", **extra):
    return {
        "workorder_id": "wo-1",
        "workorder_number": "105177647",
        "component_change_uuid": uuid,
        "position": position,
        "part_off_number": "2085M31G03",
        "part_off_serial": f"OFF-{uuid}",
        "part_on_number": "2085M31G03",
        "part_on_serial": f"ON-{uuid}",
        **extra,
    }


# --- Real named parameters, never interpolated -----------------------------


def test_get_workorder_binds_real_named_parameters_not_interpolated():
    client = Client(jobs=[Job(rows=[{"workorder_id": "wo-1", "workorder_number": "WO-SECRET-999"}])])
    result = get_workorder(runner(client), "WO-SECRET-999")

    assert result.status == SourceStatus.SUCCESS
    sql = client.sql_calls[0]
    assert "@workorder_number" in sql
    assert "WO-SECRET-999" not in sql  # the value never lands in the SQL text

    config, kwargs = client.config_calls[0]
    params = {p.name: p for p in config.query_parameters}
    assert params["workorder_number"].value == "WO-SECRET-999"
    assert params["workorder_number"].type_ == "STRING"
    assert params["limit"].type_ == "INT64"
    assert kwargs["location"] == "us-central1"
    assert config.maximum_bytes_billed and config.job_timeout_ms


def test_component_changes_part_number_is_normalized_before_binding():
    client = Client(jobs=[Job(rows=[])])
    get_component_changes(runner(client), "105177647", "2085-M31-G03")

    config, _ = client.config_calls[0]
    part_param = next(p for p in config.query_parameters if p.name == "part_number")
    assert part_param.value == "2085M31G03"
    assert "2085-M31-G03" not in client.sql_calls[0]


def test_component_changes_without_part_number_binds_null_not_missing_param():
    client = Client(jobs=[Job(rows=[])])
    get_component_changes(runner(client), "105177647")

    config, _ = client.config_calls[0]
    part_param = next(p for p in config.query_parameters if p.name == "part_number")
    assert part_param.value is None


# --- SQL templates: single parent-child chain, no cross products -----------


def _strip_sql_comments(sql: str) -> str:
    return "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )


def test_actions_sql_never_unnests_component_changes():
    sql = _strip_sql_comments((queries._SQL_DIR / "get_workorder_actions.sql").read_text())
    assert "component_changes" not in sql
    assert sql.count("CROSS JOIN UNNEST") == 2  # work_steps -> actions only


def test_component_changes_sql_unnests_single_parent_child_chain():
    sql = (queries._SQL_DIR / "get_component_changes.sql").read_text()
    assert sql.count("CROSS JOIN UNNEST") == 3  # work_steps -> actions -> component_changes
    assert "UNNEST(work_steps)" in sql
    assert "UNNEST(step.actions)" in sql
    assert "UNNEST(action.component_changes)" in sql


def test_get_workorder_sql_unnests_nothing():
    sql = (queries._SQL_DIR / "get_workorder.sql").read_text()
    assert "UNNEST" not in sql


# --- Work-order / action / component-change counts stay separate -----------


def test_actions_and_component_change_counts_are_independent_not_merged():
    action_rows = [action_row("a1"), action_row("a2")]
    change_rows = [change_row(f"c{i}") for i in range(4)]
    client = Client(jobs=[Job(rows=action_rows), Job(rows=change_rows)])
    r = runner(client)

    actions_result = get_workorder_actions(r, "105177647")
    changes_result = get_component_changes(r, "105177647")

    assert actions_result.counts == {"action_rows": 2}
    assert changes_result.counts["component_change_rows"] == 4
    assert changes_result.counts["distinct_workorder_ids"] == 1
    assert "component_change_rows" not in actions_result.counts
    assert "action_rows" not in changes_result.counts
    assert actions_result.source == "bigquery.get_workorder_actions"
    assert changes_result.source == "bigquery.get_component_changes"


def test_component_changes_preserve_recorded_position_and_serial_pairs_verbatim():
    # Mirrors WO 105177647: four structured component changes all recorded at
    # position "#2" with distinct serial pairs - never renumbered to 1/2/7/8.
    rows = [change_row(f"c{i}") for i in range(4)]
    client = Client(jobs=[Job(rows=rows)])

    result = get_component_changes(runner(client), "105177647")

    assert result.status == SourceStatus.SUCCESS
    assert len(result.records) == 4
    assert {record["position"] for record in result.records} == {"#2"}
    pairs = [(record["part_off_serial"], record["part_on_serial"]) for record in result.records]
    assert pairs == [(f"OFF-c{i}", f"ON-c{i}") for i in range(4)]


# --- Status mapping: never a failure reported as NO_MATCH -------------------


def test_zero_rows_is_no_match_not_a_failure():
    client = Client(jobs=[Job(rows=[])])
    result = get_workorder(runner(client), "UNKNOWN-WO")
    assert result.status == SourceStatus.NO_MATCH
    assert result.error_detail is None


def test_permission_error_on_submit_is_not_reported_as_no_match():
    client = Client(query_error=gax_exceptions.Forbidden("nope"))
    result = get_workorder(runner(client), "WO-1")
    assert result.status == SourceStatus.PERMISSION_DENIED
    assert result.error_detail == "permission_denied:Forbidden"


def test_timeout_cancels_job_and_is_not_no_match():
    job = Job(timeout=True)
    client = Client(jobs=[job])
    result = get_workorder_actions(runner(client), "WO-1")
    assert result.status == SourceStatus.TIMEOUT
    assert job.cancelled


def test_service_unavailable_on_result_maps_to_unavailable():
    client = Client(jobs=[Job(result_error=gax_exceptions.ServiceUnavailable("down"))])
    result = get_component_changes(runner(client), "WO-1")
    assert result.status == SourceStatus.UNAVAILABLE


def test_unexpected_error_maps_to_error_not_no_match():
    client = Client(jobs=[Job(result_error=RuntimeError("boom"))])
    result = get_workorder(runner(client), "WO-1")
    assert result.status == SourceStatus.ERROR
    assert result.error_detail == "error:RuntimeError"


def test_blank_workorder_number_is_rejected_before_querying_bigquery():
    client = Client()
    result = get_workorder(runner(client), "   ")
    assert result.status == SourceStatus.ERROR
    assert not client.sql_calls


# --- Budgets / truncation ----------------------------------------------------


def test_result_truncated_when_more_rows_than_limit_returned():
    rows = [change_row(str(i)) for i in range(5)]
    client = Client(jobs=[Job(rows=rows)])

    result = get_component_changes(runner(client), "WO-1", limit=3)

    assert result.truncated is True
    assert len(result.records) == 3
    assert result.limit == 3
    config, _ = client.config_calls[0]
    limit_param = next(p for p in config.query_parameters if p.name == "limit")
    assert limit_param.value == 4  # requested limit + 1, to detect truncation


def test_not_truncated_when_rows_fit_within_limit():
    rows = [change_row(str(i)) for i in range(2)]
    client = Client(jobs=[Job(rows=rows)])

    result = get_component_changes(runner(client), "WO-1", limit=3)

    assert result.truncated is False
    assert len(result.records) == 2


# --- Allowlisted identifiers --------------------------------------------------


def test_project_must_match_allowlisted_shape():
    with pytest.raises(ValueError):
        QueryRunner(Client(), project="Invalid_Project")


def test_dataset_must_be_allowlisted():
    with pytest.raises(ValueError):
        QueryRunner(Client(), project="valid-project", dataset="other_dataset")


def test_table_lookup_is_allowlisted():
    r = runner(Client())
    assert r.table(WORKORDERS_TABLE) == "`valid-project.pma_agent_analytics.wo_workorders`"
    with pytest.raises(ValueError):
        r.table("some_other_table")


def test_budgets_must_be_within_serving_limits():
    with pytest.raises(ValueError):
        QueryRunner(Client(), project="valid-project", timeout_seconds=0)
    with pytest.raises(ValueError):
        QueryRunner(Client(), project="valid-project", maximum_bytes_billed=0)


# --- Dataset-scoped allowlist (T02) ------------------------------------------


def test_table_resolves_second_allowlisted_dataset_via_dataset_kwarg():
    r = runner(Client())
    assert (
        r.table("dim_focus_components", dataset=CURATED_DATASET)
        == "`valid-project.pma_agent_curated.dim_focus_components`"
    )
    # default dataset resolution is unchanged
    assert r.table(WORKORDERS_TABLE) == f"`valid-project.{DATASET}.wo_workorders`"


def test_table_rejects_table_not_allowlisted_for_given_dataset():
    r = runner(Client())
    with pytest.raises(ValueError):
        r.table(WORKORDERS_TABLE, dataset=CURATED_DATASET)
    with pytest.raises(ValueError):
        r.table("dim_focus_components", dataset=DATASET)


def test_table_rejects_unknown_dataset_passed_explicitly():
    r = runner(Client())
    with pytest.raises(ValueError):
        r.table("dim_focus_components", dataset="some_other_dataset")


def test_table_quotes_information_schema_pseudo_tables_correctly():
    r = runner(Client())
    assert (
        r.table("INFORMATION_SCHEMA.TABLES", dataset=CURATED_DATASET)
        == "`valid-project.pma_agent_curated`.INFORMATION_SCHEMA.TABLES"
    )


# --- Array parameters (T02) ---------------------------------------------------


def test_job_config_builds_array_query_parameter_for_array_float64():
    r = runner(Client())
    config = r._job_config([("embedding", "ARRAY<FLOAT64>", [0.1, 0.2, 0.3])])
    (param,) = config.query_parameters
    assert param.name == "embedding"
    assert param.array_type == "FLOAT64"
    assert list(param.values) == [0.1, 0.2, 0.3]


def test_job_config_builds_array_query_parameter_for_array_string():
    r = runner(Client())
    config = r._job_config([("exclude_uuids", "ARRAY<STRING>", ["a", "b"])])
    (param,) = config.query_parameters
    assert param.name == "exclude_uuids"
    assert param.array_type == "STRING"
    assert list(param.values) == ["a", "b"]


def test_job_config_rejects_none_as_whole_array_value():
    r = runner(Client())
    with pytest.raises(ValueError):
        r._job_config([("embedding", "ARRAY<FLOAT64>", None)])


def test_job_config_rejects_none_element_within_array():
    r = runner(Client())
    with pytest.raises(ValueError):
        r._job_config([("exclude_uuids", "ARRAY<STRING>", ["a", None])])


def test_job_config_scalar_none_still_binds_null_not_rejected():
    # Existing, intentional behavior for optional scalar parameters (e.g.
    # part_number) must keep working - only ARRAY parameters reject None.
    r = runner(Client())
    config = r._job_config([("part_number", "STRING", None)])
    (param,) = config.query_parameters
    assert param.value is None


# --- Job metadata (T02) -------------------------------------------------------


def test_run_outcome_captures_job_id_and_total_bytes_billed_on_success():
    client = Client(jobs=[Job(rows=[{"workorder_id": "wo-1", "workorder_number": "WO-1"}])])
    result = get_workorder(runner(client), "WO-1")
    assert result.status == SourceStatus.SUCCESS
    # SourceResult itself does not carry job metadata, so exercise the
    # runner's _RunOutcome directly.
    r = runner(Client(jobs=[Job(rows=[], job_id="job-abc", total_bytes_billed=42)]))
    outcome = r.run(
        "get_workorder",
        [("workorder_number", "STRING", "WO-1")],
        limit=5,
        table_placeholders={"workorders_table": r.table(WORKORDERS_TABLE)},
    )
    assert outcome.job_id == "job-abc"
    assert outcome.total_bytes_billed == 42


def test_run_outcome_captures_job_id_on_result_error_failure_path():
    job = Job(result_error=RuntimeError("boom"), job_id="job-failed")
    r = runner(Client(jobs=[job]))
    outcome = r.run(
        "get_workorder",
        [("workorder_number", "STRING", "WO-1")],
        limit=5,
        table_placeholders={"workorders_table": r.table(WORKORDERS_TABLE)},
    )
    assert outcome.status == SourceStatus.ERROR
    assert outcome.job_id == "job-failed"


def test_run_outcome_job_id_is_none_on_submit_failure():
    r = runner(Client(query_error=gax_exceptions.Forbidden("nope")))
    outcome = r.run(
        "get_workorder",
        [("workorder_number", "STRING", "WO-1")],
        limit=5,
        table_placeholders={"workorders_table": r.table(WORKORDERS_TABLE)},
    )
    assert outcome.job_id is None


# --- Template constants (T02) -------------------------------------------------


def test_template_constants_accepts_allowlisted_embedding_endpoint():
    r = runner(Client(jobs=[Job(rows=[])]))
    # No template actually contains {embedding_endpoint} yet in this test
    # fixture set, so exercise validation directly via a minimal SQL name
    # that does exist and confirm no exception for an allowlisted value.
    outcome = r.run(
        "get_workorder",
        [("workorder_number", "STRING", "WO-1")],
        limit=5,
        table_placeholders={"workorders_table": r.table(WORKORDERS_TABLE)},
        template_constants={"embedding_endpoint": "text-embedding-005"},
    )
    assert outcome.status == SourceStatus.NO_MATCH


def test_template_constants_rejects_non_allowlisted_value():
    r = runner(Client(jobs=[Job(rows=[])]))
    with pytest.raises(ValueError):
        r.run(
            "get_workorder",
            [("workorder_number", "STRING", "WO-1")],
            limit=5,
            table_placeholders={"workorders_table": r.table(WORKORDERS_TABLE)},
            template_constants={"embedding_endpoint": "not-a-real-model"},
        )


def test_template_constants_rejects_unknown_placeholder_name():
    r = runner(Client(jobs=[Job(rows=[])]))
    with pytest.raises(ValueError):
        r.run(
            "get_workorder",
            [("workorder_number", "STRING", "WO-1")],
            limit=5,
            table_placeholders={"workorders_table": r.table(WORKORDERS_TABLE)},
            template_constants={"not_a_real_constant": "anything"},
        )


# --- No leaked lab/course identifiers in new curated SQL templates ----------


def test_no_pma_sql_template_contains_qwiklabs_placeholder_text():
    pma_sql_files = list(queries._SQL_DIR.glob("pma_*.sql"))
    # T01 owns creating these files in a parallel wave; tolerate none
    # existing yet while still failing loudly if one is ever checked in with
    # a leaked lab/course placeholder.
    for path in pma_sql_files:
        assert "qwiklabs" not in path.read_text().lower(), path
