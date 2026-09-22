#!/usr/bin/env python3
"""Audit the fixed AMOS corpus before any predictive-model training.

The command is intentionally local/read-only with respect to source data.  Its
outputs are write-once audit artifacts: re-running against the same snapshot
verifies the frozen files, while a changed snapshot requires new output paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from amos_data.cohorts import (
    build_installation_candidates,
    build_workorder_split_index,
    load_ndjson,
    summarize_feasibility,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data/processed/wo_workorders.ndjson.gz"
DEFAULT_AUDIT = ROOT / "docs/audits/bigquery-feasibility-v2-2026-09-22.json"
DEFAULT_MANIFEST = (
    ROOT / "docs/audits/bigquery-feasibility-v2-split-manifest-2026-09-22.json"
)
DEFAULT_WORKORDER_INDEX = (
    ROOT / "docs/audits/bigquery-feasibility-v2-workorder-split-2026-09-22.ndjson"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _write_once(path: Path, value: dict[str, object]) -> str:
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        existing = json.loads(path.read_text())
        if _canonical(existing) != _canonical(value):
            raise RuntimeError(
                f"Refusing to overwrite frozen artifact {path}. Use a new versioned path "
                "when source or assignments change."
            )
        return "verified_existing"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded)
    return "created"


def _write_ndjson_once(path: Path, index: dict[str, dict[str, object]]) -> str:
    encoded = "".join(
        json.dumps({"source_workorder_id": key, **value}, sort_keys=True) + "\n"
        for key, value in sorted(index.items())
    )
    if path.exists():
        if path.read_text() != encoded:
            raise RuntimeError(
                f"Refusing to overwrite frozen artifact {path}. Use a new versioned path "
                "when source or assignments change."
            )
        return "verified_existing"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded)
    return "created"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--audit-output", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--split-output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--workorder-index-output", type=Path, default=DEFAULT_WORKORDER_INDEX
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows, source_sha256 = load_ndjson(args.source)
    source_name = str(args.source.resolve().relative_to(ROOT))
    audit, manifest = summarize_feasibility(rows, source_sha256, source_name)
    manifest["workorder_split_index"]["path"] = str(
        args.workorder_index_output.resolve().relative_to(ROOT)
    )
    frozen = {key: value for key, value in manifest.items() if key != "freeze_id"}
    manifest["freeze_id"] = hashlib.sha256(_canonical(frozen)).hexdigest()
    candidates, _ = build_installation_candidates(rows)
    workorder_index = build_workorder_split_index(candidates, rows, manifest)
    audit_status = _write_once(args.audit_output, audit)
    manifest_status = _write_once(args.split_output, manifest)
    workorder_index_status = _write_ndjson_once(
        args.workorder_index_output, workorder_index
    )
    summary = {
        "audit_artifact": str(args.audit_output.relative_to(ROOT)),
        "audit_status": audit_status,
        "split_manifest": str(args.split_output.relative_to(ROOT)),
        "split_manifest_status": manifest_status,
        "workorder_split_index": str(args.workorder_index_output.relative_to(ROOT)),
        "workorder_split_index_status": workorder_index_status,
        "risk_training_gate_passed": audit["risk_training_gate"]["passed"],
        "gate_reasons": audit["risk_training_gate"]["reasons"],
        "all_pn_support": audit["all_pn_support"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"cohort audit failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
