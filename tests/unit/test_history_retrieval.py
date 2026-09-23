import datetime as dt
import pytest

from amos_data.retrieval import HistoryQuery, LocalHistoryProvider
from amos_data.embeddings import EmbeddingConfig
from pm_agent.sub_agents.bq_analytics.history import BQHistoryProvider


AS_OF = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)


def doc(case, text, **extra):
    return {"document_id": case + "-d", "record_id": case, "workorder_id": case, "raw_text": text, "normalized_text": text, "source_namespace": "amos", "text_role": "symptom", "source_span": "step", "available_at": "2026-09-15T00:00:00+00:00", "availability_status": "available", "split": "train", "reference_corpus_version": "v1", **extra}


def test_local_filters_held_out_self_future_and_deduplicates_cases():
    provider = LocalHistoryProvider([doc("a", "oven trips breaker"), doc("a", "oven trips breaker again"), doc("final", "oven trips", split="final_test"), doc("future", "oven trips", available_at="2026-10-01T00:00:00+00:00")])
    result = provider.search(HistoryQuery("oven breaker", AS_OF, corpus_version="v1"))
    assert [case["case_id"] for case in result["cases"]] == ["amos:a"]
    assert result["cases"][0]["score_meaning"].startswith("retrieval rank")


def test_local_rejects_known_incompatible_family_but_reports_unknown():
    provider = LocalHistoryProvider([doc("good", "heat failure", aircraft_family="B737-8"), doc("bad", "heat failure", aircraft_family="A320"), doc("unknown", "heat failure")])
    result = provider.search(HistoryQuery("heat failure", AS_OF, aircraft_family="B737-8", method="keyword"))
    assert [case["case_id"] for case in result["cases"]] == ["amos:good", "amos:unknown"]
    assert result["applied_filters"]["excluded"]["incompatible_family"] == 1


class Job:
    def __init__(self, rows=(), timeout=False):
        self.rows, self.timeout, self.cancelled = rows, timeout, False
    def result(self, timeout, **_):
        if self.timeout:
            raise TimeoutError()
        return self.rows
    def cancel(self, **_): self.cancelled = True


class Client:
    def __init__(self, jobs=None):
        self.sql, self.calls, self.jobs = [], [], list(jobs or [])
    def query(self, sql, job_config, **kwargs):
        self.sql.append(sql)
        self.calls.append((job_config, kwargs))
        return self.jobs.pop(0) if self.jobs else Job()


class Embed:
    config = EmbeddingConfig(dimension=2, corpus_version="v1")
    def __call__(self, _): return [0.1, 0.2]


def test_bq_vector_sql_uses_physical_table_bruteforce_and_parameterized_filters():
    client = Client()
    provider = BQHistoryProvider(client, project="valid-project", dataset="pma_agent_analytics", embed_query=Embed())
    result = provider.search(HistoryQuery("heat failure", AS_OF, corpus_version="v1", method="hybrid", exclude_workorder_ids=frozenset({"self"})))
    assert result["status"] == "ok"
    vector_sql = next(sql for sql in client.sql if "VECTOR_SEARCH" in sql)
    assert "retrieval_embeddings" in vector_sql and "use_brute_force" in vector_sql and "query_column_to_search" in vector_sql
    assert "NOT IN UNNEST(@exclude_wos)" in vector_sql
    assert "split IN ('train', 'development')" in vector_sql
    config, kwargs = client.calls[-1]
    parameters = {p.name: p for p in config.query_parameters}
    assert parameters["dimension"].type_ == "INT64"
    assert parameters["embedding_task"].value == "RETRIEVAL_DOCUMENT"
    assert kwargs["location"] == "us-central1"
    assert config.maximum_bytes_billed and config.job_timeout_ms


def test_duplicate_chunks_cannot_inflate_scores_or_merge_source_namespaces():
    base = [doc("a", "oven heat"), doc("b", "oven heat"), doc("a", "oven heat", source_namespace="faa_sdr", workorder_id=None)]
    query = HistoryQuery("oven heat", AS_OF, method="keyword")
    once = LocalHistoryProvider(base).search(query)
    repeated = LocalHistoryProvider(base + [base[1]] * 20).search(query)
    assert [(c["case_id"], c["relevance_score"]) for c in once["cases"]] == [(c["case_id"], c["relevance_score"]) for c in repeated["cases"]]
    assert {c["case_id"] for c in repeated["cases"]} == {"amos:a", "amos:b", "faa_sdr:a"}


def test_timeout_cancels_job_and_is_not_reported_as_no_matches():
    job = Job(timeout=True)
    provider = BQHistoryProvider(Client([job]), project="valid-project")
    result = provider.search(HistoryQuery("oven", AS_OF, corpus_version="v1", method="keyword"))
    assert job.cancelled
    assert result["status"] == "error" and result["errors"] == ["query_timeout"]


def test_missing_corpus_and_mismatched_embedding_config_cannot_query_unpinned_vectors():
    client = Client()
    provider = BQHistoryProvider(client, project="valid-project", embed_query=Embed())
    assert provider.search(HistoryQuery("oven", AS_OF))["status"] == "error"
    assert not client.sql
    result = provider.search(HistoryQuery("oven", AS_OF, method="vector", corpus_version="other"))
    assert result["status"] == "error" and not client.sql


def test_keyword_only_never_calls_embedding_provider():
    def forbidden(_):
        pytest.fail("keyword retrieval must not request embeddings")
    result = LocalHistoryProvider([doc("one", "oven")], embed_query=forbidden).search(HistoryQuery("oven", AS_OF, method="keyword"))
    assert result["cases"]


def test_best_matching_chunk_is_the_cited_excerpt_and_exact_pn_ranks_first():
    documents = [doc("a", "unrelated pump", document_id="a-0"),
                 doc("a", "oven breaker heat", document_id="a-1"),
                 doc("b", "oven", part_keys=["820111000001"])]
    result = LocalHistoryProvider(documents).search(HistoryQuery("oven breaker heat", AS_OF,
        target_part_numbers=("8201-11-0000-01",), method="keyword"))
    assert [c["case_id"] for c in result["cases"]] == ["amos:b", "amos:a"]
    assert result["cases"][1]["excerpt"] == "oven breaker heat"
    assert result["cases"][0]["compatibility"] == "exact_pn_application_unverified"


def test_local_vectors_require_matching_configuration_and_cite_vector_winner():
    config = EmbeddingConfig(dimension=2, corpus_version="v1")
    metadata = {**config.as_metadata(config.document_task), "text_preparation_version": config.preparation_version,
                "embedding_status": "success"}
    documents = [doc("a", "oven lexical", document_id="a-0", embedding=[0., 1.], **metadata),
                 doc("a", "heater trips", document_id="a-1", embedding=[1., 0.], **metadata),
                 doc("bad", "oven", embedding=[1., 0.], **{**metadata, "model_version": "other"})]
    query = HistoryQuery("oven", AS_OF, method="vector", corpus_version="v1")
    provider = LocalHistoryProvider(documents, embed_query=lambda _: [1., 0.], embedding_config=config)
    result = provider.search(query)
    assert result["status"] == "ok"
    assert [c["case_id"] for c in result["cases"]] == ["amos:a"]
    assert result["cases"][0]["excerpt"] == "heater trips"
    no_match = provider.search(HistoryQuery("oven", AS_OF, method="vector", corpus_version="v1",
        exclude_workorder_ids=frozenset({"a", "bad"})))
    assert no_match["status"] == "ok" and not no_match["cases"]
    unconfigured = LocalHistoryProvider(documents, embed_query=lambda _: [1., 0.]).search(query)
    assert not unconfigured["cases"] and unconfigured["errors"]
