import importlib.util
from pathlib import Path

import pytest
from amos_data.embeddings import EmbeddingConfig


SPEC = importlib.util.spec_from_file_location("history_eval", Path("scripts/evaluate_history_retrieval.py"))
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def document(source, record, text, **extra):
    return {"document_id": f"{source}-{record}", "source_namespace": source, "record_id": record, "workorder_id": record, "raw_text": text, "normalized_text": text, "available_at": "2026-01-01T00:00:00+00:00", "availability_status": "available", "split": "train", "reference_corpus_version": "v1", "part_keys": ["2085M31G03"], **extra}


def label(**extra):
    return {"query_id": "q1", "target_part_number": "2085M31G03", "symptoms": "oven breaker", "analysis_as_of": "2026-02-01T00:00:00+00:00", "corpus_version": "v1", "expected_relevant_case_ids": ["amos:one"], "review_status": "reviewed", **extra}


def test_reports_per_pn_metrics_and_amos_only_comparison():
    documents = [document("amos", "one", "oven breaker opens"), document("faa_sdr", "two", "oven breaker opens")]
    combined = MODULE.evaluate(documents, [label()], k=1)
    amos_only = MODULE.evaluate(documents, [label()], k=1, amos_only=True)
    assert combined["per_pn"]["2085M31G03"]["recall_at_1"] == 1
    assert amos_only["source_scope"] == "amos_only"
    assert combined["predictive_metrics"].startswith("unavailable")


def test_refuses_unreviewed_or_semantic_without_explicit_provider():
    with pytest.raises(ValueError, match="not independently reviewed"):
        MODULE.evaluate([document("amos", "one", "oven breaker")], [label(review_status="heuristic")])
    with pytest.raises(ValueError, match="semantic evaluation"):
        MODULE.evaluate([document("amos", "one", "oven breaker")], [label()], method="vector")


def test_reviewed_no_match_is_scored_and_self_exclusion_is_forwarded():
    result = MODULE.evaluate([document("amos", "self", "oven breaker")], [label(expected_relevant_case_ids=[], exclude_workorder_ids=["self"])], k=1)
    assert result["overall"]["reviewed_no_match_queries"] == 1
    assert result["overall"]["no_match_accuracy"] == 1
    assert result["overall"]["recall_at_1"] is None


def test_semantic_uses_reviewed_precomputed_query_vector_only():
    config = EmbeddingConfig(dimension=2, corpus_version="v1")
    metadata = {**config.as_metadata(config.document_task), "text_preparation_version": config.preparation_version, "embedding_status": "success", "embedding": [1.0, 0.0]}
    result = MODULE.evaluate([document("amos", "one", "unrelated words", **metadata)], [label(query_embedding=[1.0, 0.0], query_embedding_metadata=config.as_metadata(config.query_task))], method="vector", k=1, embedding_config=config)
    assert result["overall"]["recall_at_1"] == 1


def test_missing_vector_corpus_and_bad_case_ids_cannot_score_as_no_matches():
    with pytest.raises(ValueError, match="source namespace"):
        MODULE.evaluate([document("amos", "one", "oven")], [label(expected_relevant_case_ids=["one"])])
    with pytest.raises(ValueError, match="reference corpus"):
        MODULE.evaluate([document("amos", "one", "oven")], [label(corpus_version="missing")])
    config = EmbeddingConfig(dimension=2, corpus_version="v1")
    with pytest.raises(ValueError, match="retrieval failed"):
        MODULE.evaluate([document("amos", "one", "oven")], [label(query_embedding=[1., 0.],
            query_embedding_metadata=config.as_metadata(config.query_task))], method="vector", embedding_config=config)
