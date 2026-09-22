#!/usr/bin/env python3
"""Print (or explicitly execute) the BigQuery ML command only after the gate passes."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = ROOT / "docs/audits/bigquery-feasibility-v2-2026-09-22.json"
SQL_FILE = ROOT / "scripts/sql/train_failure_risk_logistic.sql"
DATASET_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
PROJECT_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def _validate_identifier(value: str, field: str) -> str:
    pattern = PROJECT_IDENTIFIER if field == "project" else DATASET_IDENTIFIER
    if not pattern.fullmatch(value):
        raise ValueError(f"{field} is not a safe BigQuery identifier")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--project", help="BigQuery project, required only with --execute"
    )
    parser.add_argument("--dataset", default="pma_agent_analytics")
    parser.add_argument(
        "--execute", action="store_true", help="Run bq query after all local gates pass"
    )
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    gate = audit.get("risk_training_gate", {})
    if gate.get("passed") is not True:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "fixed_corpus_failure_risk_gate_not_passed",
                    "gate_reasons": gate.get("reasons", []),
                },
                indent=2,
            )
        )
        return 3
    if not args.execute:
        print("Gate passed. Review and execute the SQL explicitly with --execute.")
        return 0
    if not args.project:
        parser.error("--project is required with --execute")
    project = _validate_identifier(args.project, "project")
    dataset = _validate_identifier(args.dataset, "dataset")
    sql = (
        SQL_FILE.read_text()
        .replace("${PROJECT}", project)
        .replace("${DATASET}", dataset)
    )
    subprocess.run(
        [
            "bq",
            "--location=us-central1",
            "query",
            "--maximum_bytes_billed=5000000000",
            "--use_legacy_sql=false",
            sql,
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
