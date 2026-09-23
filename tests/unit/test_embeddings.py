from __future__ import annotations

import json
from pathlib import Path

import pytest

from amos_data.embeddings import (
    EmbeddingConfig,
    EmbeddingError,
    EmbeddingRun,
    BigQueryArtifactWriter,
    JsonlEmbeddingCache,
    VertexEmbeddingClient,
    hash_text,
    validate_vector,
)


def _document(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_id": "d1", "normalized_text": "fault symptom", "text_hash": hash_text("fault symptom"), "split": "train",
        "availability_status": "snapshot_only", "text_preparation_version": "prep", "reference_corpus_version": "corpus",
    }
    row.update(extra)
    return row


def test_rejects_wrong_dimension_and_nonfinite_vectors() -> None:
    with pytest.raises(EmbeddingError, match="wrong_dimension"):
        validate_vector([1.0], dimension=2)
    with pytest.raises(EmbeddingError, match="non_finite"):
        validate_vector([1.0, float("nan")], dimension=2)


def test_retry_cache_and_config_changes_are_isolated(tmp_path: Path) -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")
    attempts = 0

    def flaky(texts: object, *, task: str) -> list[list[float]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary")
        assert task == "RETRIEVAL_DOCUMENT"
        return [[0.1, 0.2]]

    cache_path = tmp_path / "cache.jsonl"
    run = EmbeddingRun(config=config, embed=flaky, cache=JsonlEmbeddingCache(cache_path, config), retry_delay_seconds=0)
    rows, counts = run.embed_documents([_document()])
    assert counts == {"success": 1, "cached": 0, "failed": 0, "skipped": 0}
    assert attempts == 2 and rows[0]["embedding"] == [0.1, 0.2]

    def must_not_call(texts: object, *, task: str) -> list[list[float]]:
        raise AssertionError("cache should satisfy matching configuration")

    cached_run = EmbeddingRun(config=config, embed=must_not_call, cache=JsonlEmbeddingCache(cache_path, config))
    _, cached_counts = cached_run.embed_documents([_document()])
    assert cached_counts["cached"] == 1

    changed = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="other")
    assert JsonlEmbeddingCache(cache_path, changed).get(hash_text("fault symptom"), changed.document_task) is None


def test_malformed_output_and_heldout_are_visible_not_silently_uploaded(tmp_path: Path) -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")

    def malformed(texts: object, *, task: str) -> list[list[float]]:
        return [[1.0]]

    run = EmbeddingRun(config=config, embed=malformed, cache=JsonlEmbeddingCache(tmp_path / "cache", config), retries=1)
    rows, counts = run.embed_documents([_document(), _document(document_id="final", split="final_test")])
    assert counts == {"success": 0, "cached": 0, "failed": 1, "skipped": 1}
    assert rows[0]["embedding_status"] == "failed"
    assert "wrong_dimension" in str(rows[0]["error_code"])


def test_rejects_zero_vector_and_hash_mismatch_before_cache(tmp_path: Path) -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")
    with pytest.raises(EmbeddingError, match="zero_vector"):
        validate_vector([0.0, 0.0], dimension=2)

    def must_not_embed(texts: object, *, task: str) -> list[list[float]]:
        raise AssertionError("hash mismatch must fail before cache/API")

    run = EmbeddingRun(config=config, embed=must_not_embed, cache=JsonlEmbeddingCache(tmp_path / "cache", config))
    rows, counts = run.embed_documents([_document(text_hash="wrong")])
    assert counts["failed"] == 1
    assert rows[0]["error_code"] == "text_hash_mismatch"


def test_context_only_document_never_enters_embedding_reference_rows(tmp_path: Path) -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")

    def must_not_embed(texts: object, *, task: str) -> list[list[float]]:
        raise AssertionError("unverified context must never be embedded")

    rows, counts = EmbeddingRun(
        config=config, embed=must_not_embed, cache=JsonlEmbeddingCache(tmp_path / "cache", config),
    ).embed_documents([_document(availability_status="context_only")])
    assert rows == []
    assert counts == {"success": 0, "cached": 0, "failed": 0, "skipped": 1}


def test_vertex_client_uses_query_task_and_disables_auto_truncation() -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")
    captured: dict[str, object] = {}

    class Embedding:
        values = [0.3, 0.4]

    class Models:
        def embed_content(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return type("Response", (), {"embeddings": [Embedding()]})()

    client = VertexEmbeddingClient(config, project="valid-project-123", client=type("Client", (), {"models": Models()})())
    assert client.embed_query("CB opens during heating") == [0.3, 0.4]
    request_config = captured["config"]
    assert captured["model"] == "gemini-embedding-001"
    assert getattr(request_config, "task_type") == "RETRIEVAL_QUERY"
    assert getattr(request_config, "auto_truncate") is False
    assert getattr(request_config.http_options, "timeout") == 20_000
    assert getattr(request_config.http_options.retry_options, "attempts") == 1


def test_vertex_client_rejects_unbounded_request_timeout() -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")
    with pytest.raises(ValueError, match="request_timeout_ms"):
        VertexEmbeddingClient(config, project="valid-project-123", request_timeout_ms=60_001)


def test_bounded_bigquery_merge_writer_uses_artifact_keys(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class Job:
        def result(self, **kwargs: object) -> None:
            calls.append(("result", kwargs))
            return None

    class FakeClient:
        def load_table_from_json(self, rows: object, table: str, job_config: object, **kwargs: object) -> Job:
            calls.append(("load", (list(rows), table, job_config, kwargs)))
            return Job()

        def query(self, sql: str, **kwargs: object) -> Job:
            calls.append(("query", (sql, kwargs)))
            return Job()

        def delete_table(self, table: str, not_found_ok: bool, **kwargs: object) -> None:
            calls.append(("delete", (table, not_found_ok, kwargs)))

    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")
    base, _ = EmbeddingRun(
        config=config, embed=lambda texts, task: [[0.1, 0.2]],
        cache=JsonlEmbeddingCache(tmp_path / "cache", config),
    ).embed_documents([_document()])
    rows = [{**base[0], "document_id": f"d{number}"} for number in range(3)]
    writer = BigQueryArtifactWriter(
        project="valid-project-123", dataset="pma_agent_analytics", table="retrieval_embeddings",
        artifact="retrieval_embeddings", client=FakeClient(),
    )
    assert writer.write(rows, batch_size=2) == 3
    loads = [call for call in calls if call[0] == "load"]
    assert [len(call[1][0]) for call in loads] == [2, 1]
    load_config = loads[0][1][2]
    assert {field.name for field in load_config.schema} == set(rows[0])
    assert next(field for field in load_config.schema if field.name == "embedding").mode == "REPEATED"
    assert loads[0][1][3] == {"num_retries": 0, "location": "us-central1", "timeout": 20}
    sql, query_kwargs = next(call[1] for call in calls if call[0] == "query")
    assert "`document_id` = S.`document_id`" in sql
    assert "`embedding_task` = S.`embedding_task`" in sql
    assert "`text_preparation_version` = S.`text_preparation_version`" in sql
    assert "`text_hash` = S.`text_hash`" in sql
    assert query_kwargs["location"] == "us-central1"
    assert query_kwargs["retry"] is None and query_kwargs["job_retry"] is None
    assert query_kwargs["job_config"].maximum_bytes_billed == 1_000_000_000
    assert query_kwargs["job_config"].job_timeout_ms == "60000"
    assert calls[-1][0] == "delete"

    first_staging = loads[0][1][1]
    assert writer.write(rows[:1]) == 1
    second_staging = [call for call in calls if call[0] == "load"][-1][1][1]
    assert first_staging != second_staging


def test_writer_rejects_budget_schema_and_malformed_embedding_metadata(tmp_path: Path) -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")
    rows, _ = EmbeddingRun(
        config=config, embed=lambda texts, task: [[0.1, 0.2]],
        cache=JsonlEmbeddingCache(tmp_path / "cache", config),
    ).embed_documents([_document()])
    writer = BigQueryArtifactWriter(
        project="valid-project-123", dataset="pma_agent_analytics", table="retrieval_embeddings",
        artifact="retrieval_embeddings", client=object(),
    )
    with pytest.raises(ValueError, match="row budget"):
        writer.write([rows[0], dict(rows[0])], max_rows=1)
    with pytest.raises(ValueError, match="row keys"):
        writer.write([{**rows[0], "unexpected": "field"}])
    with pytest.raises(ValueError, match="immutable embedding configuration"):
        writer.write([rows[0], {**rows[0], "model_version": "different"}])
    with pytest.raises(EmbeddingError, match="zero_vector"):
        writer.write([{**rows[0], "embedding": [0.0, 0.0]}])
    with pytest.raises(ValueError, match="duplicate immutable MERGE key"):
        writer.write([rows[0], dict(rows[0])])


def test_embedding_row_matches_committed_bigquery_schema(tmp_path: Path) -> None:
    config = EmbeddingConfig(dimension=2, preparation_version="prep", corpus_version="corpus")

    def embed(texts: object, *, task: str) -> list[list[float]]:
        return [[0.1, 0.2]]

    rows, _ = EmbeddingRun(config=config, embed=embed, cache=JsonlEmbeddingCache(tmp_path / "cache", config)).embed_documents([_document()])
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "deployment/terraform/shared/retrieval_embeddings_schema.json").read_text())
    assert set(rows[0]) == {field["name"] for field in schema}
