"""Pinned Vertex embedding configuration, validation, and resumable cache.

This module intentionally does not import the application's chat agent.  The
embedding model is a separate, explicit configuration and document/query task
types are kept symmetric with the retrieval path.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"


class EmbeddingError(RuntimeError):
    """A non-retryable malformed embedding result."""


@dataclass(frozen=True)
class EmbeddingConfig:
    """The whole vector-space contract, including task and corpus identity."""

    model_id: str = "gemini-embedding-001"
    model_version: str = "001"
    dimension: int = 3072
    document_task: str = DOCUMENT_TASK
    query_task: str = QUERY_TASK
    preparation_version: str = "f2-normalize-v1"
    corpus_version: str = "unbound"

    def __post_init__(self) -> None:
        if not self.model_id or not self.model_version:
            raise ValueError("embedding model ID and version are required")
        if self.dimension < 1:
            raise ValueError("embedding dimension must be positive")
        if self.document_task != DOCUMENT_TASK or self.query_task != QUERY_TASK:
            raise ValueError("only retrieval document/query task symmetry is supported")
        if not self.preparation_version or not self.corpus_version:
            raise ValueError("preparation and corpus versions are required")

    @property
    def config_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def cache_key(self, text_hash: str, task: str) -> str:
        if task not in {self.document_task, self.query_task}:
            raise ValueError(f"unsupported embedding task: {task}")
        return hashlib.sha256(
            f"{text_hash}|{task}|{self.config_hash}".encode()
        ).hexdigest()

    def as_metadata(self, task: str) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "embedding_task": task,
            "dimension": self.dimension,
        }


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_vector(value: object, *, dimension: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != dimension:
        actual = len(value) if isinstance(value, (list, tuple)) else "not_a_vector"
        raise EmbeddingError(f"wrong_dimension:{actual}; expected:{dimension}")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise EmbeddingError("non_numeric_vector")
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise EmbeddingError("non_numeric_vector") from exc
        if not math.isfinite(number):
            raise EmbeddingError("non_finite_vector")
        result.append(number)
    if not any(result):
        raise EmbeddingError("zero_vector")
    return result


class EmbedCallable(Protocol):
    def __call__(
        self, texts: Sequence[str], *, task: str
    ) -> Sequence[Sequence[float]]: ...


class VertexEmbeddingClient:
    """Small adapter for the official ``google-genai`` Vertex embed endpoint.

    The SDK import is deferred so local document preparation and unit tests do
    not require credentials.  ``auto_truncate=False`` prevents hidden loss of
    long maintenance narratives.
    """

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        project: str,
        location: str = "global",
        request_timeout_ms: int = 20_000,
        client: Any = None,
    ) -> None:
        if not project:
            raise ValueError("Vertex project is required")
        if (
            isinstance(request_timeout_ms, bool)
            or not 1 <= request_timeout_ms <= 60_000
        ):
            raise ValueError("request_timeout_ms must be between 1 and 60000")
        self.config = config
        self._project = project
        self._location = location
        self._request_timeout_ms = request_timeout_ms
        self._client = client

    def _sdk_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self._project, location=self._location
            )
        return self._client

    def __call__(self, texts: Sequence[str], *, task: str) -> Sequence[Sequence[float]]:
        if not texts:
            return []
        if len(texts) > 250:
            raise ValueError(
                "Vertex embed_content accepts at most 250 texts per request"
            )
        if task not in {self.config.document_task, self.config.query_task}:
            raise ValueError(f"unsupported embedding task: {task}")
        # The SDK's Python field is auto_truncate; the REST API calls this
        # autoTruncate. This must remain false to reject oversize input.
        try:
            from google.genai.types import (
                EmbedContentConfig,
                HttpOptions,
                HttpRetryOptions,
            )

            request_config: Any = EmbedContentConfig(
                task_type=task,
                output_dimensionality=self.config.dimension,
                auto_truncate=False,
                # Keep each SDK request bounded and prevent layered retries:
                # EmbeddingRun is the only batch retry owner.
                http_options=HttpOptions(
                    timeout=self._request_timeout_ms,
                    retry_options=HttpRetryOptions(attempts=1),
                ),
            )
        except ImportError as exc:  # pragma: no cover - exercised with installed SDK
            raise RuntimeError(
                "google-genai SDK is required for Vertex embeddings"
            ) from exc
        response = self._sdk_client().models.embed_content(
            model=self.config.model_id, contents=list(texts), config=request_config
        )
        embeddings = getattr(response, "embeddings", None) or []
        vectors = [
            item.get("values")
            if isinstance(item, Mapping)
            else getattr(item, "values", None)
            for item in embeddings
        ]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"response_count:{len(vectors)}; expected:{len(texts)}"
            )
        return [
            validate_vector(vector, dimension=self.config.dimension)
            for vector in vectors
        ]

    def embed_query(self, text: str) -> list[float]:
        """Embed a runtime query using the same pinned vector space."""
        if not text or not text.strip():
            raise ValueError("query text must not be blank")
        vectors = self([text], task=self.config.query_task)
        return validate_vector(vectors[0], dimension=self.config.dimension)


class JsonlEmbeddingCache:
    """Append-only successful-result cache keyed by text, task, and config."""

    def __init__(self, path: Path, config: EmbeddingConfig) -> None:
        self.path = path
        self.config = config
        self._items: dict[str, list[float]] = {}
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if (
                        row.get("config_hash") != config.config_hash
                        or row.get("status") != "success"
                    ):
                        continue
                    key = str(row.get("cache_key") or "")
                    try:
                        vector = validate_vector(
                            row.get("embedding"), dimension=config.dimension
                        )
                    except EmbeddingError:
                        continue
                    if key:
                        self._items[key] = vector

    def get(self, text_hash: str, task: str) -> list[float] | None:
        return self._items.get(self.config.cache_key(text_hash, task))

    def put(self, text_hash: str, task: str, vector: Sequence[float]) -> None:
        checked = validate_vector(vector, dimension=self.config.dimension)
        key = self.config.cache_key(text_hash, task)
        if key in self._items:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "cache_key": key,
            "config_hash": self.config.config_hash,
            "task": task,
            "text_hash": text_hash,
            "embedding": checked,
            "status": "success",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._items[key] = checked


@dataclass
class EmbeddingRun:
    config: EmbeddingConfig
    embed: EmbedCallable
    cache: JsonlEmbeddingCache
    retries: int = 2
    retry_delay_seconds: float = 0.25

    def embed_documents(
        self, documents: Iterable[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        rows: list[dict[str, Any]] = []
        counts = {"success": 0, "cached": 0, "failed": 0, "skipped": 0}
        for document in documents:
            if (
                document.get("text_preparation_version")
                != self.config.preparation_version
            ):
                raise ValueError(
                    "document preparation version does not match embedding configuration"
                )
            if document.get("reference_corpus_version") != self.config.corpus_version:
                raise ValueError(
                    "document corpus version does not match embedding configuration"
                )
            if document.get("split") in {"final_test", "purged"}:
                counts["skipped"] += 1
                continue
            if document.get("availability_status") not in {
                "snapshot_only",
                "available",
            }:
                counts["skipped"] += 1
                continue
            raw = str(document.get("normalized_text") or "")
            if not raw.strip():
                rows.append(self._failure_row(document, "empty_text"))
                counts["failed"] += 1
                continue
            source_hash = str(document.get("text_hash") or "")
            if source_hash != hash_text(raw):
                rows.append(self._failure_row(document, "text_hash_mismatch"))
                counts["failed"] += 1
                continue
            cached = self.cache.get(source_hash, self.config.document_task)
            if cached is not None:
                rows.append(self._success_row(document, cached))
                counts["cached"] += 1
                continue
            try:
                vector = self._with_retry(raw, self.config.document_task)
                self.cache.put(source_hash, self.config.document_task, vector)
                rows.append(self._success_row(document, vector))
                counts["success"] += 1
            except Exception as exc:  # a failure row is an auditable result
                rows.append(self._failure_row(document, self._error_code(exc)))
                counts["failed"] += 1
        return rows, counts

    def _with_retry(self, text: str, task: str) -> list[float]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.embed([text], task=task)
                if len(response) != 1:
                    raise EmbeddingError(f"response_count:{len(response)}; expected:1")
                return validate_vector(response[0], dimension=self.config.dimension)
            except EmbeddingError:
                raise
            except Exception as exc:  # transient SDK/network errors may retry
                last_error = exc
                if attempt < self.retries:
                    time.sleep(
                        self.retry_delay_seconds * (2**attempt)
                        + random.uniform(0, 0.05)
                    )
        assert last_error is not None
        raise last_error

    def _success_row(
        self, document: Mapping[str, Any], vector: Sequence[float]
    ) -> dict[str, Any]:
        row = {
            key: document.get(key)
            for key in (
                "document_id",
                "source_namespace",
                "source_snapshot",
                "record_id",
                "workorder_id",
                "workorder_number",
                "step_or_segment_id",
                "chunk_id",
                "text_role",
                "source_span",
                "text_hash",
                "text_preparation_version",
                "available_at",
                "availability_status",
                "split",
                "reference_corpus_version",
                "part_number_raw",
                "part_number_key",
                "component_part_number_raw",
                "component_part_number_key",
                "part_keys",
                "aircraft_family",
                "aircraft_id",
                "component_class",
                "ata_chapter",
                "ata_code",
                "position",
            )
        }
        row.update(
            self.config.as_metadata(self.config.document_task),
            embedding=list(vector),
            embedding_status="success",
            error_code=None,
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        return row

    def _failure_row(self, document: Mapping[str, Any], code: str) -> dict[str, Any]:
        row = {
            key: document.get(key)
            for key in (
                "document_id",
                "source_namespace",
                "source_snapshot",
                "record_id",
                "workorder_id",
                "workorder_number",
                "step_or_segment_id",
                "chunk_id",
                "text_role",
                "source_span",
                "text_hash",
                "text_preparation_version",
                "available_at",
                "availability_status",
                "split",
                "reference_corpus_version",
                "part_number_raw",
                "part_number_key",
                "component_part_number_raw",
                "component_part_number_key",
                "part_keys",
                "aircraft_family",
                "aircraft_id",
                "component_class",
                "ata_chapter",
                "ata_code",
                "position",
            )
        }
        row.update(
            self.config.as_metadata(self.config.document_task),
            embedding=[],
            embedding_status="failed",
            error_code=code,
            generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        return row

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, EmbeddingError):
            return str(exc)
        return f"{exc.__class__.__name__}:{str(exc)[:160]}"


def validate_bigquery_target(
    project: str, dataset: str, table: str, *, artifact: str
) -> str:
    import re

    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project or ""):
        raise ValueError("project must be a valid explicit Google Cloud project ID")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,1023}", dataset or ""):
        raise ValueError("dataset must be a configured BigQuery identifier")
    if table != artifact:
        raise ValueError(f"F2 writer only permits the {artifact} table")
    return f"{project}.{dataset}.{table}"


def artifact_schema_columns(artifact: str) -> frozenset[str]:
    """Load the Terraform-owned physical schema used by the batch writer."""
    if artifact not in {"retrieval_documents", "retrieval_embeddings"}:
        raise ValueError("unsupported F2 artifact")
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "deployment"
        / "terraform"
        / "shared"
        / f"{artifact}_schema.json"
    )
    fields = json.loads(schema_path.read_text(encoding="utf-8"))
    return frozenset(str(field["name"]) for field in fields)


def artifact_schema_fields(artifact: str) -> list[Any]:
    """Convert the checked-in JSON schema to explicit BigQuery SchemaFields."""
    from google.cloud import bigquery

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "deployment"
        / "terraform"
        / "shared"
        / f"{artifact}_schema.json"
    )

    def field(spec: Mapping[str, Any]) -> Any:
        return bigquery.SchemaField(
            spec["name"],
            spec["type"],
            mode=spec.get("mode", "NULLABLE"),
            fields=tuple(field(child) for child in spec.get("fields", ())),
        )

    return [field(spec) for spec in json.loads(schema_path.read_text(encoding="utf-8"))]


class BigQueryArtifactWriter:
    """Bounded staging+MERGE writer for one approved immutable F2 artifact."""

    def __init__(
        self,
        *,
        project: str,
        dataset: str,
        table: str,
        artifact: str,
        location: str = "us-central1",
        request_timeout_seconds: int = 20,
        job_timeout_seconds: int = 60,
        maximum_bytes_billed: int = 1_000_000_000,
        client: Any = None,
    ) -> None:
        self.target = validate_bigquery_target(
            project, dataset, table, artifact=artifact
        )
        if (
            not location
            or not 1 <= request_timeout_seconds <= 60
            or not 1 <= job_timeout_seconds <= 300
        ):
            raise ValueError(
                "BigQuery writer timeouts must be bounded (request <=60s, job <=300s)"
            )
        if not 1 <= maximum_bytes_billed <= 10_000_000_000:
            raise ValueError("maximum_bytes_billed must be between 1 and 10000000000")
        self.artifact = artifact
        self._project = project
        self._dataset = dataset
        self._table = table
        self._location = location
        self._request_timeout_seconds = request_timeout_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._maximum_bytes_billed = maximum_bytes_billed
        self._client = client

    def _client_or_default(self) -> Any:
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(
                project=self._project, location=self._location
            )
        return self._client

    def _merge_key_columns(self) -> list[str]:
        keys = [
            "document_id",
            "reference_corpus_version",
            "text_preparation_version",
            "text_hash",
        ]
        if self.artifact == "retrieval_embeddings":
            keys.extend(["model_id", "model_version", "embedding_task", "dimension"])
        return keys

    def write(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        batch_size: int = 500,
        max_rows: int = 10_000,
    ) -> int:
        if not rows:
            return 0
        if not 1 <= batch_size <= 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        if not 1 <= max_rows <= 1_000_000:
            raise ValueError("max_rows must be between 1 and 1000000")
        if len(rows) > max_rows:
            raise ValueError(f"row budget exceeded: {len(rows)} > {max_rows}")
        expected_columns = artifact_schema_columns(self.artifact)
        for row in rows:
            if set(row) != expected_columns:
                raise ValueError(
                    "row keys do not exactly match the configured BigQuery schema"
                )
        corpus = {str(row.get("reference_corpus_version") or "") for row in rows}
        if len(corpus) != 1 or not next(iter(corpus)):
            raise ValueError(
                "writer accepts exactly one nonempty corpus version per run"
            )
        if any(
            not isinstance(row.get("document_id"), str) or not row["document_id"]
            for row in rows
        ):
            raise ValueError("document_id is required for every artifact row")
        if self.artifact == "retrieval_documents":
            for row in rows:
                normalized = row.get("normalized_text")
                if not isinstance(normalized, str) or row.get("text_hash") != hash_text(
                    normalized
                ):
                    raise ValueError(
                        "document text hash does not match normalized text"
                    )
        if self.artifact == "retrieval_embeddings":
            config_values = {
                (
                    row.get("model_id"),
                    row.get("model_version"),
                    row.get("embedding_task"),
                    row.get("dimension"),
                    row.get("text_preparation_version"),
                )
                for row in rows
            }
            if len(config_values) != 1:
                raise ValueError(
                    "writer accepts one immutable embedding configuration per run"
                )
            model_id, model_version, task, dimension, preparation = next(
                iter(config_values)
            )
            if (
                not isinstance(model_id, str)
                or not model_id
                or not isinstance(model_version, str)
                or not model_version
                or task != DOCUMENT_TASK
                or isinstance(dimension, bool)
                or not isinstance(dimension, int)
                or dimension < 1
                or not isinstance(preparation, str)
                or not preparation
            ):
                raise ValueError(
                    "embedding metadata is malformed or not a document configuration"
                )
            if any(row.get("embedding_status") != "success" for row in rows):
                raise ValueError("writer refuses failed or unvalidated embedding rows")
            for row in rows:
                validate_vector(row.get("embedding"), dimension=dimension)
        key_columns = self._merge_key_columns()
        keys = [tuple(row[column] for column in key_columns) for row in rows]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate immutable MERGE key in one write")
        # Per-invocation staging prevents concurrent identical-corpus writers
        # from truncating or deleting one another's temporary table.
        staging = f"{self._project}.{self._dataset}._f2_{self.artifact}_stage_{uuid.uuid4().hex}"
        client = self._client_or_default()
        try:
            schema = artifact_schema_fields(self.artifact)
            for offset in range(0, len(rows), batch_size):
                batch = [dict(row) for row in rows[offset : offset + batch_size]]
                # ``load_table_from_json`` avoids an oversized insert request;
                # WRITE_TRUNCATE makes re-runs idempotent before the MERGE.
                from google.cloud import bigquery

                config = bigquery.LoadJobConfig(
                    schema=schema,
                    autodetect=False,
                    write_disposition=(
                        "WRITE_TRUNCATE" if offset == 0 else "WRITE_APPEND"
                    ),
                )
                client.load_table_from_json(
                    batch,
                    staging,
                    job_config=config,
                    num_retries=0,
                    location=self._location,
                    timeout=self._request_timeout_seconds,
                ).result(timeout=self._job_timeout_seconds)
            columns = sorted(rows[0])
            predicate = " AND ".join(f"T.`{key}` = S.`{key}`" for key in key_columns)
            updates = ", ".join(
                f"`{column}` = S.`{column}`"
                for column in columns
                if column not in key_columns
            )
            insert_columns = ", ".join(f"`{column}`" for column in columns)
            insert_values = ", ".join(f"S.`{column}`" for column in columns)
            sql = (
                f"MERGE `{self.target}` T USING `{staging}` S ON {predicate} "
                f"WHEN MATCHED THEN UPDATE SET {updates} "
                f"WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})"
            )
            query_config = bigquery.QueryJobConfig(
                maximum_bytes_billed=self._maximum_bytes_billed,
                job_timeout_ms=self._job_timeout_seconds * 1000,
            )
            client.query(
                sql,
                job_config=query_config,
                location=self._location,
                retry=None,
                job_retry=None,
                timeout=self._request_timeout_seconds,
            ).result(timeout=self._job_timeout_seconds)
            return len(rows)
        finally:
            client.delete_table(
                staging,
                not_found_ok=True,
                retry=None,
                timeout=self._request_timeout_seconds,
            )


__all__ = [
    "DOCUMENT_TASK",
    "QUERY_TASK",
    "BigQueryArtifactWriter",
    "EmbeddingConfig",
    "EmbeddingError",
    "EmbeddingRun",
    "JsonlEmbeddingCache",
    "VertexEmbeddingClient",
    "artifact_schema_columns",
    "artifact_schema_fields",
    "hash_text",
    "validate_bigquery_target",
    "validate_vector",
]
