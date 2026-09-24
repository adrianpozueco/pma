"""Unit tests for `BigQueryPredictionRepository` (PMA-ONLINE-AGENT-plan.md
§7/§8.1, task T08).

Uses a fake/mocked `google.cloud.bigquery.Client` throughout, mirroring
`tests/unit/test_bq_predefined_queries.py` - no live credentials, no
network. The fake records the real BigQuery named parameters and job config
each call sends, so tests can assert `@query_embedding` is bound as
`ARRAY<FLOAT64>`, UUID lists as `ARRAY<STRING>`, that no bound value is ever
`None`, that `k`/`limit` reach the runner as `limit=`, and that failures are
mapped to `DataSourceUnavailable`/`EmbeddingFailed` rather than leaking raw
BigQuery exceptions or being silently treated as empty results.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from google.api_core import exceptions as gax_exceptions

from pm_agent.prediction.contracts import (
    CurrentTac,
    DataSourceUnavailable,
    EmbeddingFailed,
    LastReplacement,
    NeighbourLeadSample,
)
from pm_agent.prediction.repository import BigQueryPredictionRepository
from pm_agent.prediction.settings import PredictionSettings
from pm_agent.sub_agents.bq_analytics.queries import QueryRunner

AS_OF = datetime(2026, 6, 1, tzinfo=UTC)


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


def make_runner(client) -> QueryRunner:
    return QueryRunner(client, project="valid-project")


def make_repo(client, **settings_overrides) -> BigQueryPredictionRepository:
    settings = PredictionSettings(**settings_overrides)
    return BigQueryPredictionRepository(make_runner(client), settings)


def _param(config_calls, index, name):
    config, _ = config_calls[index]
    return next(p for p in config.query_parameters if p.name == name)


# --- focus_components --------------------------------------------------------


def test_focus_components_parses_rows_and_uses_allowlisted_curated_tables():
    rows = [
        {
            "component_key": "2085M31G03|#1/#2",
            "part_number": "2085M31G03",
            "position": "#1/#2",
            "part_key": "2085M31G03",
            "part_aliases": ["2085M31G03", "2085M31G04"],
            "freq_rank": 1,
            "replacement_count": 42,
            "aircraft_with_replacement": 12,
        }
    ]
    client = Client(jobs=[Job(rows=rows)])
    repo = make_repo(client)

    result = repo.focus_components()

    assert len(result) == 1
    fc = result[0]
    assert fc.component_key == "2085M31G03|#1/#2"
    assert fc.part_aliases == ("2085M31G03", "2085M31G04")
    assert fc.freq_rank == 1
    sql = client.sql_calls[0]
    assert "`valid-project.pma_agent_curated.dim_focus_components`" in sql
    assert "`valid-project.pma_agent_curated.fct_replacement_events`" in sql


def test_focus_components_is_cached_within_ttl_not_requeried():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    first = repo.focus_components()
    second = repo.focus_components()

    assert first == second == []
    assert len(client.sql_calls) == 1  # second call hit the cache


def test_focus_components_requeries_once_ttl_is_zero():
    client = Client(jobs=[Job(rows=[]), Job(rows=[])])
    repo = make_repo(client, focus_cache_ttl_s=0)

    repo.focus_components()
    repo.focus_components()

    assert len(client.sql_calls) == 2


# --- curated_build_info -------------------------------------------------------


def test_curated_build_info_parses_creation_times_and_is_cached():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "table_name": "dim_focus_components",
            "creation_time": created,
            "labels": None,
        },
        {
            "table_name": "fct_replacement_events",
            "creation_time": created,
            "labels": None,
        },
    ]
    client = Client(jobs=[Job(rows=rows)])
    repo = make_repo(client)

    info = repo.curated_build_info()
    again = repo.curated_build_info()

    assert info.table_creation_times == {
        "dim_focus_components": created,
        "fct_replacement_events": created,
    }
    assert again is info
    assert len(client.sql_calls) == 1
    sql = client.sql_calls[0]
    assert "`valid-project.pma_agent_curated`.INFORMATION_SCHEMA.TABLES" in sql
    assert "`valid-project.pma_agent_curated`.INFORMATION_SCHEMA.TABLE_OPTIONS" in sql


# --- embed_query ---------------------------------------------------------------


def test_embed_query_binds_wo_text_parameter_and_embedding_endpoint_constant():
    embedding = [0.1] * 768
    client = Client(jobs=[Job(rows=[{"embedding": embedding, "status": ""}])])
    repo = make_repo(client)

    result = repo.embed_query("PUMP LEAKING")

    assert result == embedding
    text_param = _param(client.config_calls, 0, "wo_text")
    assert text_param.value == "PUMP LEAKING"
    assert text_param.type_ == "STRING"
    assert "text-embedding-005" in client.sql_calls[0]
    assert "PUMP LEAKING" not in client.sql_calls[0]


def test_embed_query_nonempty_status_raises_embedding_failed():
    client = Client(
        jobs=[Job(rows=[{"embedding": [0.1] * 768, "status": "error: bad input"}])]
    )
    repo = make_repo(client)

    with pytest.raises(EmbeddingFailed):
        repo.embed_query("anything")


def test_embed_query_wrong_dimension_raises_embedding_failed():
    client = Client(jobs=[Job(rows=[{"embedding": [0.1] * 5, "status": ""}])])
    repo = make_repo(client)

    with pytest.raises(EmbeddingFailed):
        repo.embed_query("anything")


# --- anchor_neighbours / wo_neighbours ----------------------------------------


def test_anchor_neighbours_binds_array_float64_array_string_and_limit_plus_one():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    repo.anchor_neighbours(
        emb=[0.1, 0.2, 0.3],
        as_of=AS_OF,
        exclude=["wo-1", None, "", "wo-2"],
        k=5,
        min_sim=0.85,
    )

    emb_param = _param(client.config_calls, 0, "query_embedding")
    assert emb_param.array_type == "FLOAT64"
    assert list(emb_param.values) == [0.1, 0.2, 0.3]

    exclude_param = _param(client.config_calls, 0, "exclude_wo_uuids")
    assert exclude_param.array_type == "STRING"
    assert None not in list(exclude_param.values)
    assert list(exclude_param.values) == ["wo-1", "wo-2"]

    limit_param = _param(client.config_calls, 0, "limit")
    assert limit_param.value == 6  # k + 1, to detect truncation


def test_anchor_neighbours_parses_rows_into_neighbour():
    rows = [
        {
            "replacement_wo_uuid": "wo-uuid-1",
            "replacement_wo_id": "105177647",
            "component_key": "2085M31G03|#1/#2",
            "aircraft_reg": "SP-REG00374",
            "replacement_tac": 4200,
            "replacement_date": date(2026, 3, 1),
            "snippet": "pump replaced",
            "sim": 0.91,
        }
    ]
    client = Client(jobs=[Job(rows=rows)])
    repo = make_repo(client)

    result = repo.anchor_neighbours([0.1], AS_OF, [], k=20, min_sim=0.85)

    assert len(result) == 1
    n = result[0]
    assert n.wo_uuid == "wo-uuid-1"
    assert n.component_key == "2085M31G03|#1/#2"
    assert n.replacement_tac == 4200
    assert n.replacement_date == date(2026, 3, 1)
    assert n.sim == 0.91


def test_wo_neighbours_parses_rows_and_uses_analytics_and_curated_tables():
    rows = [
        {
            "wo_uuid": "wo-uuid-2",
            "wo_id": "190306536",
            "aircraft_reg": "EI-REG00263",
            "ata_chapter": "33-21",
            "tac": 6072,
            "closing_date": date(2026, 2, 1),
            "snippet": "window light replaced",
            "sim": 0.82,
        }
    ]
    client = Client(jobs=[Job(rows=rows)])
    repo = make_repo(client)

    result = repo.wo_neighbours([0.1], AS_OF, [], k=10, min_sim=0.80)

    assert len(result) == 1
    n = result[0]
    assert n.wo_uuid == "wo-uuid-2"
    assert n.ata_chapter == "33-21"
    assert n.tac == 6072
    assert n.closing_date == date(2026, 2, 1)
    sql = client.sql_calls[0]
    assert "`valid-project.pma_agent_curated.wo_embeddings`" in sql
    assert "`valid-project.pma_agent_analytics.wo_workorders`" in sql


# --- lead_time_stats -----------------------------------------------------------


def test_lead_time_stats_zero_samples_maps_to_n_zero():
    row = {
        "component_key": "K1",
        "n": 0,
        "aircraft_n": 0,
        "replacement_n": 0,
        "lead_min": None,
        "lead_max": None,
        "lead_p50": None,
        "lead_p90": None,
        "lead_mean": None,
        "lead_sd": None,
        "cv": None,
        "replacement_interval_share": None,
        "chained_sample_n": 0,
        "replacement_wo_uuids": [],
    }
    client = Client(jobs=[Job(rows=[row])])
    repo = make_repo(client)

    stats = repo.lead_time_stats("K1", AS_OF, [])

    assert stats.n == 0
    assert stats.lead_p50 is None
    assert stats.replacement_wo_uuids == ()


def test_lead_time_stats_never_uses_no_match_status_as_n_zero_signal():
    # Even if the runner somehow reports zero rows (it shouldn't - the SQL
    # is an unconditional aggregate), n=0 must come from row absence, not
    # from branching on outcome.status == NO_MATCH.
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    stats = repo.lead_time_stats("K1", AS_OF, [])

    assert stats.n == 0
    assert stats.component_key == "K1"


def test_lead_time_stats_parses_full_row():
    row = {
        "component_key": "K1",
        "n": 12,
        "aircraft_n": 8,
        "replacement_n": 10,
        "lead_min": 5,
        "lead_max": 400,
        "lead_p50": 120.0,
        "lead_p90": 300.0,
        "lead_mean": 150.5,
        "lead_sd": 40.2,
        "cv": 0.27,
        "replacement_interval_share": 0.1,
        "chained_sample_n": 2,
        "replacement_wo_uuids": ["wo-a", "wo-b"],
    }
    client = Client(jobs=[Job(rows=[row])])
    repo = make_repo(client)

    stats = repo.lead_time_stats("K1", AS_OF, ["excl-1"])

    assert stats.n == 12
    assert stats.aircraft_n == 8
    assert stats.replacement_wo_uuids == ("wo-a", "wo-b")
    exclude_param = _param(client.config_calls, 0, "exclude_wo_uuids")
    assert list(exclude_param.values) == ["excl-1"]


# --- precursor_evidence ---------------------------------------------------------


def test_precursor_evidence_binds_neighbour_uuids_array_and_parses_rows():
    row = {
        "component_key": "K1",
        "aircraft_reg": "SP-REG00374",
        "precursor_wo_id": "wo-100",
        "precursor_wo_uuid": "prec-uuid-1",
        "precursor_tac": 4100,
        "replacement_wo_uuid": "wo-uuid-1",
        "replacement_tac": 4200,
        "replacement_date": date(2026, 3, 1),
        "lead_cycles": 100,
        "sim": 0.9,
        "reason": "same symptom",
        "precursor_snippet": "pump warning",
        "is_neighbour_hit": True,
    }
    client = Client(jobs=[Job(rows=[row])])
    repo = make_repo(client)

    result = repo.precursor_evidence(
        "K1", AS_OF, exclude=[], neighbour_uuids=["prec-uuid-1", None], limit=5
    )

    assert len(result) == 1
    evidence = result[0]
    assert evidence.precursor_wo_uuid == "prec-uuid-1"
    assert evidence.is_neighbour_hit is True
    neighbour_param = _param(client.config_calls, 0, "neighbour_wo_uuids")
    assert list(neighbour_param.values) == ["prec-uuid-1"]
    assert None not in list(neighbour_param.values)
    limit_param = _param(client.config_calls, 0, "limit")
    assert limit_param.value == 6


# --- neighbour_lead_samples (condensed recommendation, USER REQUEST: base ------
# --- it on wo_embeddings neighbour matching + fct_lead_time_samples) -----------


def test_neighbour_lead_samples_binds_uuid_array_and_parses_rows():
    row = {
        "component_key": "473597-5|AFT",
        "precursor_wo_uuid": "prec-uuid-1",
        "aircraft_reg": "SP-REG00374",
        "lead_cycles": 156,
    }
    client = Client(jobs=[Job(rows=[row])])
    repo = make_repo(client)

    result = repo.neighbour_lead_samples(
        ["prec-uuid-1", None, ""], AS_OF, "self-uuid"
    )

    assert result == [
        NeighbourLeadSample(
            component_key="473597-5|AFT",
            precursor_wo_uuid="prec-uuid-1",
            aircraft_reg="SP-REG00374",
            lead_cycles=156,
        )
    ]
    neighbour_param = _param(client.config_calls, 0, "neighbour_wo_uuids")
    assert list(neighbour_param.values) == ["prec-uuid-1"]
    assert None not in list(neighbour_param.values)


def test_neighbour_lead_samples_binds_empty_string_not_none_for_exclude_uuid():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    repo.neighbour_lead_samples(["prec-uuid-1"], AS_OF, "")

    exclude_param = _param(client.config_calls, 0, "exclude_wo_uuid")
    assert exclude_param.value == ""
    assert exclude_param.value is not None


def test_neighbour_lead_samples_sql_uses_v_work_orders_and_fct_lead_time_samples():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    repo.neighbour_lead_samples(["prec-uuid-1"], AS_OF, "self-uuid")

    sql = client.sql_calls[0]
    assert "`valid-project.pma_agent_curated.fct_lead_time_samples`" in sql
    assert "v_work_orders`" in sql
    assert "{" not in sql


def test_neighbour_lead_samples_returns_empty_list_when_no_matches():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    assert repo.neighbour_lead_samples(["prec-uuid-1"], AS_OF, "") == []


def test_neighbour_lead_samples_raises_data_source_unavailable_on_failure():
    client = Client(query_error=gax_exceptions.Forbidden("nope"))
    repo = make_repo(client)

    with pytest.raises(DataSourceUnavailable):
        repo.neighbour_lead_samples(["prec-uuid-1"], AS_OF, "")


# --- latest_closing_tac / latest_closing_max_tac_before_cutoff -----------------


def test_latest_closing_tac_returns_none_when_no_eligible_prior_wo():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    assert repo.latest_closing_tac("SP-REG00374", AS_OF, "") is None


def test_latest_closing_tac_returns_raw_candidate_and_companion_exposes_max_tac():
    row = {
        "workorder_number": "105177647",
        "tac": 4180,
        "observed_at": datetime(2026, 5, 1, tzinfo=UTC),
        "max_tac_before_cutoff": 4300,
    }
    client = Client(jobs=[Job(rows=[row]), Job(rows=[row])])
    repo = make_repo(client)

    current = repo.latest_closing_tac("SP-REG00374", AS_OF, "wo-uuid-self")
    max_before_cutoff = repo.latest_closing_max_tac_before_cutoff(
        "SP-REG00374", AS_OF, "wo-uuid-self"
    )

    assert current == CurrentTac(
        value=4180, source="latest_closing_tac", observed_at=row["observed_at"]
    )
    assert current.stale is False
    assert current.counter_inconsistent is False
    assert max_before_cutoff == 4300


def test_latest_closing_tac_binds_empty_string_not_none_for_exclude_uuid():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    repo.latest_closing_tac("SP-REG00374", AS_OF, "")

    exclude_param = _param(client.config_calls, 0, "exclude_wo_uuid")
    assert exclude_param.value == ""
    assert exclude_param.value is not None


# --- last_replacement (Option B projected window, approved 2026-09-24) ----------


def test_last_replacement_returns_none_when_no_eligible_prior_replacement():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    assert repo.last_replacement("473597-5|AFT", "SP-REG00374", AS_OF, "") is None


def test_last_replacement_parses_row_into_dataclass():
    row = {
        "replacement_tac": 17938,
        "replacement_date": date(2026, 4, 25),
        "replacement_wo_id": "105177647",
        "replacement_wo_uuid": "wo-uuid-old",
    }
    client = Client(jobs=[Job(rows=[row])])
    repo = make_repo(client)

    result = repo.last_replacement("473597-5|AFT", "SP-REG00374", AS_OF, "self-uuid")

    assert result == LastReplacement(
        tac=17938,
        replacement_date=date(2026, 4, 25),
        wo_id="105177647",
        wo_uuid="wo-uuid-old",
    )


def test_last_replacement_binds_empty_string_not_none_for_exclude_uuid():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    repo.last_replacement("473597-5|AFT", "SP-REG00374", AS_OF, "")

    exclude_param = _param(client.config_calls, 0, "exclude_wo_uuid")
    assert exclude_param.value == ""
    assert exclude_param.value is not None
    component_param = _param(client.config_calls, 0, "component_key")
    assert component_param.value == "473597-5|AFT"
    reg_param = _param(client.config_calls, 0, "aircraft_reg")
    assert reg_param.value == "SP-REG00374"


def test_last_replacement_sql_uses_timestamp_cutoff_and_exclude_guard():
    client = Client(jobs=[Job(rows=[])])
    repo = make_repo(client)

    repo.last_replacement("473597-5|AFT", "SP-REG00374", AS_OF, "self-uuid")

    sql = client.sql_calls[0]
    # Same cut-off predicate as pma_latest_closing_tac, so a replacement closed
    # earlier on the analysis day is visible to both queries.
    assert (
        "COALESCE(w.closing_ts, TIMESTAMP(r.replacement_date)) < @analysis_as_of" in sql
    )
    assert "IFNULL(r.replacement_wo_uuid, '') != @exclude_wo_uuid" in sql
    assert "`valid-project.pma_agent_curated.fct_replacement_events`" in sql
    assert "v_work_orders`" in sql
    assert "{" not in sql


def test_last_replacement_raises_data_source_unavailable_on_failure():
    client = Client(query_error=gax_exceptions.Forbidden("nope"))
    repo = make_repo(client)

    with pytest.raises(DataSourceUnavailable):
        repo.last_replacement("473597-5|AFT", "SP-REG00374", AS_OF, "")


# --- failure mapping -------------------------------------------------------------


def test_forbidden_on_submit_raises_data_source_unavailable():
    client = Client(query_error=gax_exceptions.Forbidden("nope"))
    repo = make_repo(client)

    with pytest.raises(DataSourceUnavailable):
        repo.focus_components()


def test_timeout_raises_data_source_unavailable():
    client = Client(jobs=[Job(timeout=True)])
    repo = make_repo(client)

    with pytest.raises(DataSourceUnavailable):
        repo.anchor_neighbours([0.1], AS_OF, [], k=5, min_sim=0.85)


def test_service_unavailable_raises_data_source_unavailable():
    client = Client(jobs=[Job(result_error=gax_exceptions.ServiceUnavailable("down"))])
    repo = make_repo(client)

    with pytest.raises(DataSourceUnavailable):
        repo.wo_neighbours([0.1], AS_OF, [], k=5, min_sim=0.80)


# --- job_log ----------------------------------------------------------------------


def test_job_log_collects_job_id_and_total_bytes_billed_across_calls():
    client = Client(
        jobs=[
            Job(rows=[], job_id="job-focus", total_bytes_billed=1000),
            Job(
                rows=[
                    {
                        "table_name": "dim_focus_components",
                        "creation_time": AS_OF,
                        "labels": None,
                    }
                ],
                job_id="job-build",
                total_bytes_billed=2000,
            ),
        ]
    )
    repo = make_repo(client)

    repo.focus_components()
    repo.curated_build_info()

    assert [j.job_id for j in repo.job_log] == ["job-focus", "job-build"]
    assert [j.total_bytes_billed for j in repo.job_log] == [1000, 2000]


def test_job_log_records_entry_even_on_failure():
    client = Client(jobs=[Job(result_error=RuntimeError("boom"), job_id="job-failed")])
    repo = make_repo(client)

    with pytest.raises(DataSourceUnavailable):
        repo.focus_components()

    assert [j.job_id for j in repo.job_log] == ["job-failed"]
