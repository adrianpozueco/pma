#!/usr/bin/env python3
"""Credential-free evaluation of reviewed historical-retrieval relevance labels."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from amos_data.embeddings import EmbeddingConfig, validate_vector
from amos_data.retrieval import HistoryQuery, LocalHistoryProvider

TARGET_PNS = ("2085M31G03", "62197-301-001", "8201-11-0000-01")


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _as_of(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("analysis_as_of must be timezone-aware")
    return parsed


def _validate_labels(labels: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    valid = []
    query_ids = set()
    for label in labels:
        required = (
            "query_id",
            "target_part_number",
            "symptoms",
            "analysis_as_of",
            "corpus_version",
            "expected_relevant_case_ids",
        )
        if any(
            key not in label
            or (key != "expected_relevant_case_ids" and not label.get(key))
            for key in required
        ):
            raise ValueError("reviewed relevance row missing required field")
        if label.get("review_status") != "reviewed":
            raise ValueError(
                f"query {label.get('query_id')} is not independently reviewed"
            )
        if not isinstance(label["expected_relevant_case_ids"], list):
            raise ValueError("expected_relevant_case_ids must be a list")
        if any(
            not isinstance(case, str) or not re.fullmatch(r"(?:amos|faa_sdr):.+", case)
            for case in label["expected_relevant_case_ids"]
        ):
            raise ValueError(
                "relevant case IDs must include an allowed source namespace"
            )
        if label["query_id"] in query_ids:
            raise ValueError("query_id must be unique in the evaluation set")
        query_ids.add(label["query_id"])
        _as_of(str(label["analysis_as_of"]))
        valid.append(label)
    if not valid:
        raise ValueError("no reviewed relevance labels supplied")
    return valid


def _metrics(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    totals = defaultdict(float)
    for row in rows:
        expected, returned = set(row["expected"]), row["returned"][:k]
        hits = len(expected.intersection(returned))
        totals["queries"] += 1
        totals["precision"] += hits / k
        if expected:
            totals["recall"] += hits / len(expected)
            totals["mrr"] += next(
                (1 / rank for rank, case in enumerate(returned, 1) if case in expected),
                0.0,
            )
            totals["relevant_queries"] += 1
        totals["missing_support"] += float(not expected)
        totals["no_match_correct"] += float(not expected and not returned)
    count = totals["queries"] or 1
    relevant = totals["relevant_queries"]
    return {
        "queries": int(totals["queries"]),
        f"precision_at_{k}": totals["precision"] / count if rows else None,
        f"recall_at_{k}": totals["recall"] / relevant if relevant else None,
        "mrr": totals["mrr"] / relevant if relevant else None,
        "reviewed_no_match_queries": int(totals["missing_support"]),
        "missing_target_support": not rows,
        "no_match_accuracy": totals["no_match_correct"] / totals["missing_support"]
        if totals["missing_support"]
        else None,
    }


def evaluate(
    documents: list[dict[str, Any]],
    labels: list[Mapping[str, Any]],
    *,
    method: str = "keyword",
    k: int = 5,
    amos_only: bool = False,
    embedding_config: EmbeddingConfig | None = None,
) -> dict[str, Any]:
    if method not in {"keyword", "vector", "hybrid"}:
        raise ValueError("method must be keyword, vector, or hybrid")
    if method != "keyword" and embedding_config is None:
        raise ValueError(
            "semantic evaluation needs a compatible explicit embedding configuration"
        )
    if not 1 <= k <= 50:
        raise ValueError("k must be between 1 and 50")
    selected = [
        doc
        for doc in documents
        if not amos_only or doc.get("source_namespace") == "amos"
    ]
    result_rows = []
    for label in _validate_labels(labels):
        if not any(
            doc.get("reference_corpus_version") == label["corpus_version"]
            for doc in documents
        ):
            raise ValueError("reference corpus required by a query is missing")
        if str(label["target_part_number"]) not in TARGET_PNS:
            raise ValueError(
                "final evaluation labels must use one of the three fixed target PNs"
            )
        if method != "keyword":
            if embedding_config.corpus_version != label["corpus_version"]:
                raise ValueError("query corpus differs from embedding configuration")
            metadata = label.get("query_embedding_metadata") or {}
            if metadata != embedding_config.as_metadata(embedding_config.query_task):
                raise ValueError("incompatible query embedding metadata")
            vector = validate_vector(
                label.get("query_embedding"), dimension=embedding_config.dimension
            )
            provider = LocalHistoryProvider(
                selected,
                embed_query=lambda _text, value=vector: value,
                embedding_config=embedding_config,
            )
        else:
            provider = LocalHistoryProvider(selected)
        query = HistoryQuery(
            symptoms=str(label["symptoms"]),
            analysis_as_of=_as_of(str(label["analysis_as_of"])),
            target_part_numbers=(str(label["target_part_number"]),),
            aircraft_family=label.get("aircraft_family"),
            ata_code=label.get("ata_code"),
            position=label.get("position"),
            exclude_workorder_ids=frozenset(label.get("exclude_workorder_ids", [])),
            exclude_text_hashes=frozenset(label.get("exclude_text_hashes", [])),
            corpus_version=str(label["corpus_version"]),
            limit=k,
            method=method,
        )
        response = provider.search(query)
        if response["status"] != "ok":
            raise ValueError(
                f"retrieval failed for query {label['query_id']}: {response['errors']}"
            )
        result_rows.append(
            {
                "pn": str(label["target_part_number"]),
                "expected": label["expected_relevant_case_ids"],
                "returned": [case["case_id"] for case in response["cases"]],
            }
        )
    per_pn = {
        pn: _metrics([row for row in result_rows if row["pn"] == pn], k)
        for pn in TARGET_PNS
    }
    excluded = sum(
        1
        for row in labels
        for case in row["expected_relevant_case_ids"]
        if amos_only and not case.startswith("amos:")
    )
    return {
        "status": "evaluated",
        "method": method,
        "k": k,
        "source_scope": "amos_only" if amos_only else "amos_plus_faa",
        "overall": _metrics(result_rows, k),
        "per_pn": per_pn,
        "excluded_source_support": excluded,
        "predictive_metrics": "unavailable: relevance judgments do not establish failure prediction",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--method", default="keyword")
    parser.add_argument("--k", default=5, type=int)
    parser.add_argument("--amos-only", action="store_true")
    parser.add_argument("--embedding-config", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = (
        EmbeddingConfig(**json.loads(args.embedding_config.read_text()))
        if args.embedding_config
        else None
    )
    report = evaluate(
        _lines(args.documents),
        _lines(args.labels),
        method=args.method,
        k=args.k,
        amos_only=args.amos_only,
        embedding_config=config,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
