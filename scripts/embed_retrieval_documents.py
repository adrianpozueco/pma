"""Embed canonical retrieval documents with a resumable JSONL cache.

Dry run is the default.  ``--run`` makes Vertex calls; ``--write-bigquery`` is
an additional explicit action and writes only validated rows to the supplied
fully configured project/dataset/table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amos_data.embeddings import (  # noqa: E402
    BigQueryArtifactWriter,
    EmbeddingConfig,
    EmbeddingRun,
    JsonlEmbeddingCache,
    VertexEmbeddingClient,
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument(
        "--cache", type=Path, default=Path("/tmp/retrieval_embedding_cache.jsonl")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/retrieval_embeddings.jsonl")
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--run", action="store_true", help="Make bounded Vertex embedding calls"
    )
    parser.add_argument("--project")
    parser.add_argument("--location", default="global")
    parser.add_argument("--model-id", default="gemini-embedding-001")
    parser.add_argument("--model-version", default="001")
    parser.add_argument("--dimension", type=int, default=3072)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--write-bigquery",
        action="store_true",
        help="Write validated successful rows after --run",
    )
    parser.add_argument("--dataset", default="pma_agent_analytics")
    parser.add_argument("--table", default="retrieval_embeddings")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-write-rows", type=int, default=10_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    documents = read_jsonl(args.documents)
    if args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    eligible = [
        document
        for document in documents
        if document.get("split") not in {"final_test", "purged"}
        and document.get("availability_status") in {"snapshot_only", "available"}
    ]
    documents = eligible if args.limit == 0 else eligible[: args.limit]
    corpus = {str(row.get("reference_corpus_version") or "") for row in documents}
    prep = {str(row.get("text_preparation_version") or "") for row in documents}
    if len(corpus) != 1 or "" in corpus or len(prep) != 1 or "" in prep:
        raise SystemExit(
            "documents must be one nonempty corpus and preparation version"
        )
    config = EmbeddingConfig(
        model_id=args.model_id,
        model_version=args.model_version,
        dimension=args.dimension,
        preparation_version=prep.pop(),
        corpus_version=corpus.pop(),
    )
    if args.write_bigquery and not args.run:
        raise SystemExit("--write-bigquery requires --run")
    if not args.run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "documents": len(documents),
                    "config_hash": config.config_hash,
                    "cache": str(args.cache),
                }
            )
        )
        return 0
    if not args.project:
        raise SystemExit("--project is required with --run")
    client = VertexEmbeddingClient(config, project=args.project, location=args.location)
    run = EmbeddingRun(
        config=config,
        embed=client,
        cache=JsonlEmbeddingCache(args.cache, config),
        retries=args.retries,
    )
    rows, counts = run.embed_documents(documents)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    if args.write_bigquery:
        valid = [row for row in rows if row["embedding_status"] == "success"]
        BigQueryArtifactWriter(
            project=args.project,
            dataset=args.dataset,
            table=args.table,
            artifact="retrieval_embeddings",
        ).write(valid, batch_size=args.batch_size, max_rows=args.max_write_rows)
    print(
        json.dumps(
            {
                "counts": counts,
                "output": str(args.output),
                "config_hash": config.config_hash,
            },
            sort_keys=True,
        )
    )
    return 0 if not counts["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
