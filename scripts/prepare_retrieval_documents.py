"""Prepare an immutable F2 canonical retrieval-document artifact.

It never changes source files. BigQuery writes require an explicit flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amos_data.documents import DEFAULT_CHUNK_CHARS, build_documents  # noqa: E402
from amos_data.embeddings import BigQueryArtifactWriter  # noqa: E402

DEFAULT_AMOS = ROOT / "data/processed/wo_workorders.ndjson.gz"
DEFAULT_INDEX = (
    ROOT / "docs/audits/bigquery-feasibility-v2-workorder-split-2026-09-22.ndjson"
)
DEFAULT_FAA = tuple(
    ROOT / "data/faa-sdr" / f"SDR-{year}.csv" for year in ("2023", "2024", "2025")
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amos", type=Path, default=DEFAULT_AMOS)
    parser.add_argument("--split-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--faa", type=Path, action="append", default=[])
    parser.add_argument(
        "--faa-snapshot-as-of",
        help="RFC3339 export/snapshot time; do not use SubmissionDate",
    )
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/retrieval_documents.jsonl")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("/tmp/retrieval_documents_coverage.json")
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Small local sample by default; 0 emits all",
    )
    parser.add_argument(
        "--all", action="store_true", help="Emit all eligible canonical documents"
    )
    parser.add_argument(
        "--write-bigquery",
        action="store_true",
        help="MERGE the artifact after preparation",
    )
    parser.add_argument(
        "--project", help="Explicit project required when writing BigQuery"
    )
    parser.add_argument("--dataset", default="pma_agent_analytics")
    parser.add_argument("--table", default="retrieval_documents")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-write-rows", type=int, default=10_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    faa = tuple(args.faa) if args.faa else DEFAULT_FAA
    documents, report = build_documents(
        amos_path=args.amos,
        split_index_path=args.split_index,
        faa_paths=faa,
        faa_snapshot_as_of=args.faa_snapshot_as_of,
        chunk_chars=args.chunk_chars,
    )
    eligible = [
        document
        for document in documents
        if document["split"] in {"train", "development"}
        and document["availability_status"] in {"snapshot_only", "available"}
    ]
    selected = documents if args.all or args.limit == 0 else eligible[: args.limit]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    output_bytes = "".join(
        json.dumps(document, sort_keys=True) + "\n" for document in selected
    ).encode()
    report_data = report.as_dict(
        corpus_version=documents[0]["reference_corpus_version"]
        if documents
        else "none",
        freeze_id=report.freeze_id,
    )
    report_data.update(
        {
            "emitted_to_output": len(selected),
            "eligible_documents": len(eligible),
            "limit": args.limit,
            "full_artifact_documents": len(documents),
        }
    )
    report_bytes = (json.dumps(report_data, indent=2, sort_keys=True) + "\n").encode()
    for path, contents in ((args.output, output_bytes), (args.report, report_bytes)):
        if path.exists() and path.read_bytes() != contents:
            raise SystemExit(
                f"refusing to overwrite changed immutable artifact: {path}"
            )
        if not path.exists():
            path.write_bytes(contents)
    if args.write_bigquery:
        if not args.project:
            raise SystemExit("--project is required with --write-bigquery")
        if not (args.all or args.limit == 0):
            raise SystemExit(
                "--write-bigquery requires --all or --limit 0 for a complete canonical artifact"
            )
        BigQueryArtifactWriter(
            project=args.project,
            dataset=args.dataset,
            table=args.table,
            artifact="retrieval_documents",
        ).write(documents, batch_size=args.batch_size, max_rows=args.max_write_rows)
    print(json.dumps(report_data, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
