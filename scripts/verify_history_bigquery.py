"""Verify production retrieval SQL with synthetic BigQuery session temporary tables.

Uses the configured project/ADC and a 100 MB billing cap per script. Creates no
permanent resources and reads no source records. Explicitly run with uv after
configuring Google credentials; keyword, vector and hybrid checks must all pass.
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path

import google.auth
from dotenv import load_dotenv
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amos_data.embeddings import EmbeddingConfig  # noqa: E402
from amos_data.retrieval import HistoryQuery  # noqa: E402
from pm_agent.sub_agents.bq_analytics.history import BQHistoryProvider  # noqa: E402


def main() -> None:
    load_dotenv(ROOT / ".env")
    credentials, adc_project = google.auth.default()
    project = os.getenv("GOOGLE_CLOUD_PROJECT") or adc_project
    client = bigquery.Client(
        project=project, credentials=credentials, location="us-central1"
    )
    config = EmbeddingConfig(dimension=3, corpus_version="synthetic-live-v1")

    def literal(value):
        if value is None:
            return "NULL"
        if isinstance(value, list):
            return "[" + ", ".join(literal(x) for x in value) + "]"
        if isinstance(value, (int, float)):
            return str(value)
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def fixture(table, rows):
        schema = json.loads(
            (ROOT / f"deployment/terraform/shared/{table}_schema.json").read_text()
        )
        fields = {field["name"] for field in schema}
        types = [
            f"`{f['name']}` "
            + (f"ARRAY<{f['type']}>" if f["mode"] == "REPEATED" else f["type"])
            for f in schema
        ]
        sql = f"CREATE TEMP TABLE {table} ({', '.join(types)});\n"
        for row in rows:
            row = {k: v for k, v in row.items() if k in fields}
            sql += f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join(literal(v) for v in row.values())});\n"
        return sql

    base = {
        "source_namespace": "amos",
        "workorder_id": "uuid-good",
        "workorder_number": "wo-good",
        "source_snapshot": "synthetic",
        "record_id": "good",
        "document_id": "good-1",
        "text_role": "symptom",
        "source_span": "synthetic:step:1",
        "raw_text": "oven trips breaker",
        "normalized_text": "oven trips breaker",
        "text_hash": "good-hash",
        "text_preparation_version": config.preparation_version,
        "available_at": "2026-01-01T00:00:00Z",
        "availability_status": "snapshot_only",
        "split": "train",
        "reference_corpus_version": config.corpus_version,
        "part_keys": ["820111000001"],
        "aircraft_family": "737-800",
        "aircraft_id": "synthetic-aircraft",
    }
    documents = [base]
    for name, extra in [
        ("heldout", {"split": "final_test"}),
        ("future", {"available_at": "2027-01-01T00:00:00Z"}),
        ("self", {"workorder_number": "self-number"}),
        ("duplicate", {"text_hash": "excluded-hash"}),
        ("family", {"aircraft_family": "A320"}),
        ("unavailable", {"availability_status": "context_only"}),
        ("corpus", {"reference_corpus_version": "other"}),
        (
            "faa",
            {
                "source_namespace": "faa_sdr",
                "workorder_id": None,
                "workorder_number": None,
                "aircraft_family": None,
            },
        ),
    ]:
        documents.append(dict(base, record_id=name, document_id=name + "-1", **extra))
    embeddings = [
        dict(
            d,
            model_id=config.model_id,
            model_version=config.model_version,
            dimension=config.dimension,
            embedding_task=config.document_task,
            embedding_status="success",
            embedding=[1.0, 0.1, 0.2],
        )
        for d in documents
    ]
    setup = fixture("retrieval_documents", documents) + fixture(
        "retrieval_embeddings", embeddings
    )

    class SyntheticClient:
        def query(self, sql, **kwargs):
            for table in ("retrieval_documents", "retrieval_embeddings"):
                sql = sql.replace(
                    f"`{project}.pma_agent_analytics.{table}`", f"_SESSION.{table}"
                )
            return client.query(setup + sql, **kwargs)

    provider = BQHistoryProvider(
        SyntheticClient(),
        project=project,
        embed_query=lambda _: [1.0, 0.1, 0.2],
        embedding_config=config,
        timeout_seconds=60,
        maximum_bytes_billed=100_000_000,
    )
    for method in ("keyword", "vector", "hybrid"):
        query = HistoryQuery(
            "oven breaker",
            dt.datetime(2026, 9, 22, tzinfo=dt.UTC),
            corpus_version=config.corpus_version,
            method=method,
            target_part_numbers=("8201-11-0000-01",),
            aircraft_family="737-800",
            aircraft_id="synthetic-aircraft",
            exclude_workorder_ids=frozenset({"self-number"}),
            exclude_text_hashes=frozenset({"excluded-hash"}),
        )
        result = provider.search(query)
        print(
            json.dumps(
                {
                    "method": method,
                    "status": result["status"],
                    "errors": result["errors"],
                    "case_ids": [c["case_id"] for c in result["cases"]],
                }
            ),
            flush=True,
        )
        assert result["status"] == "ok", result
        assert {c["case_id"] for c in result["cases"]} == {
            "amos:good",
            "faa_sdr:faa",
        }, result
        assert {c["history_scope"] for c in result["cases"]} == {
            "own_aircraft",
            "public_report",
        }, result


if __name__ == "__main__":
    main()
