"""Filter FAA Service Difficulty Reports down to parts seen in the AMOS workorders.

The full SDR corpus is ~196k rows across three years; only a small slice refers
to part numbers that actually appear in our fleet's workorders. That slice is
useful mainly as enrichment: SDR carries PartName and PartCondition, which the
workorder XML never records.

Matching is on normalised part numbers (see wo_xml.part_key) because the two
sources punctuate differently - WO writes 15800-029-3, SDR writes 158000293.

Outputs:
    data/processed/faa_sdr_matching_wo_parts.csv
    deployment/terraform/shared/faa_sdr_wo_parts_schema.json

Usage:
    uv run python scripts/build_faa_sdr_wo_parts.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wo_xml import (  # noqa: E402
    PROCESSED_DIR,
    REPO_ROOT,
    iter_workorders,
    part_key,
    workorder_part_keys,
)

SDR_DIR = REPO_ROOT / "data" / "faa-sdr"
SDR_YEARS = ("2023", "2024", "2025")
OUT_CSV = PROCESSED_DIR / "faa_sdr_matching_wo_parts.csv"
SCHEMA_PATH = REPO_ROOT / "deployment" / "terraform" / "shared" / "faa_sdr_wo_parts_schema.json"

META_COLUMNS = ["match_key", "match_field", "wo_part_number", "sdr_year"]

# Everything else stays STRING: the structural and component columns are under
# 5% filled and carry mixed free text that defeats any narrower type.
DATE_COLUMNS = {"DifficultyDate"}
TIMESTAMP_COLUMNS = {"SubmissionDate"}
INT_COLUMNS = {"AircraftTotalTime", "AircraftTotalCycles"}

csv.field_size_limit(10_000_000)


def normalise_date(value: str) -> str:
    """SDR writes MM/DD/YYYY; BigQuery wants ISO. Unparseable values become empty."""
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def normalise_int(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return str(int(float(value)))
    except ValueError:
        return ""


def collect_wo_part_keys() -> tuple[set[str], dict[str, str]]:
    """Normalised part keys from every workorder, plus one raw spelling each."""
    keys: set[str] = set()
    raw: dict[str, str] = {}
    for _, root in iter_workorders():
        keys |= workorder_part_keys(root)
        for node, path in (
            (".//componentChange", "partOff/partNumber"),
            (".//componentChange", "partOn/partNumber"),
            (".//partRequest", "partNumber"),
        ):
            for el in root.findall(node):
                target = el.find(path)
                if target is not None and target.text:
                    value = target.text.strip()
                    key = part_key(value)
                    if key:
                        raw.setdefault(key, value)
    return keys, raw


def build_schema(columns: list[str]) -> list[dict]:
    schema = []
    for name in columns:
        if name in DATE_COLUMNS:
            type_ = "DATE"
        elif name in TIMESTAMP_COLUMNS:
            type_ = "TIMESTAMP"
        elif name in INT_COLUMNS:
            type_ = "INT64"
        else:
            type_ = "STRING"
        schema.append({"name": name, "type": type_, "mode": "NULLABLE"})
    return schema


def main() -> int:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)

    target_keys, raw_spelling = collect_wo_part_keys()

    matches: list[dict] = []
    header: list[str] | None = None
    scanned = 0

    for year in SDR_YEARS:
        path = SDR_DIR / f"SDR-{year}.csv"
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            header = header or list(reader.fieldnames or [])
            for row in reader:
                scanned += 1
                pk = part_key(row.get("PartNumber"))
                ck = part_key(row.get("ComponentPartNumber"))
                if pk and pk in target_keys:
                    key, field = pk, "PartNumber"
                elif ck and ck in target_keys:
                    key, field = ck, "ComponentPartNumber"
                else:
                    continue
                row = {k: (v or "").strip() for k, v in row.items() if k is not None}
                for column in DATE_COLUMNS:
                    row[column] = normalise_date(row.get(column, ""))
                for column in INT_COLUMNS:
                    row[column] = normalise_int(row.get(column, ""))
                row.update(
                    match_key=key,
                    match_field=field,
                    wo_part_number=raw_spelling.get(key, ""),
                    sdr_year=year,
                )
                matches.append(row)

    if header is None:
        raise SystemExit("no SDR input files found")

    columns = header + META_COLUMNS
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(matches)

    SCHEMA_PATH.write_text(json.dumps(build_schema(columns), indent=2) + "\n")

    distinct_parts = len({m["match_key"] for m in matches})
    print(f"wo part keys     : {len(target_keys)}")
    print(f"sdr rows scanned : {scanned}")
    print(f"rows matched     : {len(matches)}")
    print(f"distinct wo parts: {distinct_parts}")
    print(f"csv              : {OUT_CSV.relative_to(REPO_ROOT)}")
    print(f"schema           : {SCHEMA_PATH.relative_to(REPO_ROOT)} ({len(columns)} columns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
