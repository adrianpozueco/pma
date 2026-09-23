"""Read-only, parameterized historical search over pinned BigQuery artifacts."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from amos_data.embeddings import EmbeddingConfig, validate_vector
from amos_data.retrieval import HistoryQuery, reciprocal_rank_fusion

LOCATION = "us-central1"
DATASET = "pma_agent_analytics"


class BQHistoryProvider:
    def __init__(
        self,
        client: Any,
        *,
        project: str,
        dataset: str = DATASET,
        embed_query: Callable[[str], list[float]] | None = None,
        embedding_config: EmbeddingConfig | None = None,
        timeout_seconds: int = 20,
        maximum_bytes_billed: int = 5_000_000_000,
    ) -> None:
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project)
            or dataset != DATASET
        ):
            raise ValueError(
                "use a configured project and the maintenance analytics dataset"
            )
        if (
            not 1 <= timeout_seconds <= 60
            or not 1 <= maximum_bytes_billed <= 5_000_000_000
        ):
            raise ValueError("query budgets must be positive and within serving limits")
        self.client, self.project, self.dataset = client, project, dataset
        self.embed_query = embed_query
        self.embedding_config = embedding_config or getattr(
            getattr(embed_query, "__self__", embed_query), "config", None
        )
        self.timeout_seconds, self.maximum_bytes_billed = (
            timeout_seconds,
            maximum_bytes_billed,
        )

    def search(self, query: HistoryQuery) -> dict[str, Any]:
        if not query.corpus_version:
            return self._error("reference_corpus_unconfigured")
        semantic = query.method in {"vector", "hybrid"}
        if semantic and (self.embed_query is None or self.embedding_config is None):
            return self._error("vector_embedding_provider_unconfigured")
        try:
            keyword = (
                self._query(*self._sql(query))
                if query.method in {"keyword", "hybrid"}
                else []
            )
            vector = (
                self._query(*self._vector_sql(query, self.embed_query(query.symptoms)))
                if semantic
                else []
            )
        except TimeoutError:
            return self._error("query_timeout")
        except Exception as exc:
            return self._error(f"query_error:{type(exc).__name__}")
        scores = reciprocal_rank_fusion(
            (r["case_id"] for r in keyword), (r["case_id"] for r in vector)
        )
        records = {r["case_id"]: r for r in reversed(vector + keyword)}
        ordered = sorted(scores, key=lambda key: (-scores[key], key))
        cases = []
        for rank, key in enumerate(ordered[: query.limit], 1):
            record = dict(records[key])
            record.update(
                rank=rank,
                relevance_score=scores[key],
                score_meaning="retrieval relevance; not failure probability",
            )
            record["history_scope"] = (
                "public_report"
                if record["source_namespace"] == "faa_sdr"
                else "own_aircraft"
                if query.aircraft_id and record.get("aircraft_id") == query.aircraft_id
                else "comparable_amos"
            )
            cases.append(record)
        return {
            "cases": cases,
            "status": "ok",
            "errors": [],
            "method": query.method,
            "truncation": {
                "requested_limit": query.limit,
                "returned": len(cases),
                "truncated": len(ordered) > query.limit,
                "candidate_limit_reached": max(len(keyword), len(vector))
                >= self._candidate_limit(query),
            },
            "applied_filters": {
                "as_of": query.analysis_as_of.isoformat(),
                "aircraft_family": query.aircraft_family,
                "position": query.position,
                "ata_code": query.ata_code,
                "component_class": query.component_class,
                "excluded_workorders": sorted(query.exclude_workorder_ids),
            },
            "versions": {
                "corpus_version": query.corpus_version,
                "embedding_config": self.embedding_config.config_hash
                if semantic
                else None,
            },
            "source_counts": {
                source: sum(r["source_namespace"] == source for r in cases)
                for source in ("amos", "faa_sdr")
            },
        }

    @staticmethod
    def _candidate_limit(query: HistoryQuery) -> int:
        return min(query.limit * 5 + 1, 251)

    def _query(self, sql: str, params: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        job = self.client.query(
            sql,
            job_config=self._job_config(params),
            location=LOCATION,
            timeout=self.timeout_seconds,
            retry=None,
            job_retry=None,
        )
        try:
            return [
                dict(row)
                for row in job.result(timeout=self.timeout_seconds, retry=None)
            ]
        except TimeoutError:
            job.cancel(timeout=self.timeout_seconds, retry=None)
            raise

    def _filters(
        self, query: HistoryQuery, alias: str
    ) -> tuple[list[str], list[tuple[str, Any]]]:
        a = alias
        where = [
            f"{a}.split IN ('train', 'development')",
            f"{a}.source_namespace IN ('amos', 'faa_sdr')",
            f"{a}.available_at < @as_of",
            f"{a}.availability_status IN ('available', 'snapshot_only')",
            f"{a}.reference_corpus_version = @corpus_version",
        ]
        params: list[tuple[str, Any]] = [
            ("as_of", query.analysis_as_of),
            ("corpus_version", query.corpus_version),
            ("candidate_limit", self._candidate_limit(query)),
        ]
        for name, value in (
            ("aircraft_family", query.aircraft_family),
            ("position", query.position),
            ("ata_code", query.ata_code),
            ("component_class", query.component_class),
        ):
            if value:
                where.append(
                    f"(NULLIF({a}.{name}, '') IS NULL OR {a}.{name} = @{name})"
                )
                params.append((name, value))
        if query.exclude_workorder_ids:
            where.extend(
                f"COALESCE({a}.{field}, '') NOT IN UNNEST(@exclude_wos)"
                for field in ("workorder_id", "workorder_number", "record_id")
            )
            params.append(("exclude_wos", sorted(query.exclude_workorder_ids)))
        if query.exclude_text_hashes:
            where.append(f"COALESCE({a}.text_hash, '') NOT IN UNNEST(@exclude_hashes)")
            params.append(("exclude_hashes", sorted(query.exclude_text_hashes)))
        parts = [re.sub(r"[^A-Z0-9]", "", p.upper()) for p in query.target_part_numbers]
        params.append(("part_keys", parts))
        return where, params

    @staticmethod
    def _projection(alias: str = "d") -> str:
        a = alias
        return f"""CONCAT({a}.source_namespace, ':', {a}.record_id) AS case_id,
          {a}.document_id, {a}.source_namespace, {a}.record_id, {a}.workorder_id,
          {a}.workorder_number, {a}.aircraft_id, {a}.aircraft_family, {a}.text_role,
          {a}.raw_text AS excerpt, {a}.source_span AS citation, {a}.available_at,
          {a}.availability_status,
          CASE WHEN EXISTS(SELECT 1 FROM UNNEST({a}.part_keys) p WHERE p IN UNNEST(@part_keys))
            THEN 'exact_pn_application_unverified' ELSE 'related_or_unknown_part' END AS compatibility"""

    def _sql(self, query: HistoryQuery) -> tuple[str, list[tuple[str, Any]]]:
        where, params = self._filters(query, "d")
        tokens = sorted(set(re.findall(r"[a-z0-9]{2,}", query.symptoms.lower())))[:128]
        params.append(("tokens", tokens))
        # STRPOS accepts a parameter-derived token; CONTAINS_SUBSTR requires a constant.
        # Keep negation in the excerpt. Lexical ranking does not classify defects.
        sql = f"""WITH scored AS (
          SELECT d.*, (SELECT COUNT(*) FROM UNNEST(@tokens) token
             WHERE STRPOS(LOWER(d.normalized_text), token) > 0) AS keyword_score,
             IF(EXISTS(SELECT 1 FROM UNNEST(d.part_keys) p WHERE p IN UNNEST(@part_keys)), 1, 0) AS pn_match
          FROM `{self.project}.{self.dataset}.retrieval_documents` d
          WHERE {" AND ".join(where)}
        ) SELECT {self._projection()}, d.keyword_score
          FROM scored d WHERE d.keyword_score > 0
          QUALIFY ROW_NUMBER() OVER (PARTITION BY d.source_namespace, d.record_id
             ORDER BY d.pn_match DESC, d.keyword_score DESC, d.document_id) = 1
          ORDER BY d.pn_match DESC, d.keyword_score DESC, case_id LIMIT @candidate_limit"""
        return sql, params

    def _vector_sql(
        self, query: HistoryQuery, embedding: list[float]
    ) -> tuple[str, list[tuple[str, Any]]]:
        config = self.embedding_config
        if config is None or config.corpus_version != query.corpus_version:
            raise ValueError(
                "query embedding configuration must match the reference corpus"
            )
        embedding = validate_vector(embedding, dimension=config.dimension)
        if not any(embedding):
            raise ValueError("query embedding must be nonzero")
        where, params = self._filters(query, "e")
        where.extend(
            [
                "e.embedding_status = 'success'",
                "e.model_id = @model_id",
                "e.model_version = @model_version",
                "e.dimension = @dimension",
                "e.embedding_task = @embedding_task",
                "e.text_preparation_version = @prep_version",
            ]
        )
        params.extend(
            [
                ("query_embedding", embedding),
                ("model_id", config.model_id),
                ("model_version", config.model_version),
                ("dimension", config.dimension),
                ("embedding_task", config.document_task),
                ("prep_version", config.preparation_version),
            ]
        )
        base = f"SELECT * FROM `{self.project}.{self.dataset}.retrieval_embeddings` e WHERE {' AND '.join(where)}"
        sql = f"""SELECT {self._projection()}, v.distance
          FROM VECTOR_SEARCH(({base}), 'embedding', (SELECT @query_embedding AS embedding),
            query_column_to_search => 'embedding', top_k => @candidate_limit,
            distance_type => 'COSINE', options => '{{"use_brute_force":true}}') v
          JOIN `{self.project}.{self.dataset}.retrieval_documents` d
            ON d.document_id = v.base.document_id AND d.text_hash = v.base.text_hash
            AND d.reference_corpus_version = v.base.reference_corpus_version
            AND d.text_preparation_version = v.base.text_preparation_version
          WHERE d.split IN ('train', 'development') AND d.available_at < @as_of
            AND d.availability_status IN ('available', 'snapshot_only')
          QUALIFY ROW_NUMBER() OVER (PARTITION BY d.source_namespace, d.record_id
             ORDER BY v.distance, d.document_id) = 1
          ORDER BY v.distance, case_id"""
        return sql, params

    def _job_config(self, params: list[tuple[str, Any]]) -> Any:
        from google.cloud import bigquery

        values = []
        for name, value in params:
            if isinstance(value, list):
                parameter = bigquery.ArrayQueryParameter(
                    name, "FLOAT64" if name == "query_embedding" else "STRING", value
                )
            else:
                kind = (
                    "TIMESTAMP"
                    if name == "as_of"
                    else "INT64"
                    if isinstance(value, int)
                    else "STRING"
                )
                parameter = bigquery.ScalarQueryParameter(name, kind, value)
            values.append(parameter)
        return bigquery.QueryJobConfig(
            query_parameters=values,
            maximum_bytes_billed=self.maximum_bytes_billed,
            job_timeout_ms=self.timeout_seconds * 1000,
            use_query_cache=True,
        )

    @staticmethod
    def _error(error: str) -> dict[str, Any]:
        return {
            "cases": [],
            "status": "error",
            "errors": [error],
            "truncation": {"truncated": False},
            "applied_filters": {},
            "versions": {},
        }
