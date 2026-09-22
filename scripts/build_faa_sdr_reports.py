"""Build the versioned full FAA Service Difficulty Report import.

The existing ``build_faa_sdr_wo_parts.py`` intentionally produces the 299-row
AMOS-matched enrichment table.  This job keeps the complete fixed 2023--2025
snapshot in a separately named CSV for report-level retrieval.  Original FAA
values, including counter strings, are retained.  Conservative typed parses
and statuses are appended so malformed values remain visible instead of being
rounded, truncated, or silently discarded.

Usage::

    uv run python scripts/build_faa_sdr_reports.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wo_xml import PROCESSED_DIR, REPO_ROOT

SDR_DIR = REPO_ROOT / "data" / "faa-sdr"
SDR_YEARS = ("2023", "2024", "2025")
OUT_CSV = PROCESSED_DIR / "faa_sdr_reports.csv"
SCHEMA_PATH = (
    REPO_ROOT / "deployment" / "terraform" / "shared" / "faa_sdr_reports_schema.json"
)

META_COLUMNS = [
    "source_year",
    "source_file",
    "source_row_number",
    "source_snapshot_hash",
    "difficulty_date_iso",
    "submission_timestamp_iso",
    "difficulty_date_valid",
    "submission_timestamp_valid",
    "aircraft_total_time_value",
    "aircraft_total_cycles_value",
    "aircraft_total_time_parse_status",
    "aircraft_total_cycles_parse_status",
    "part_total_cycles_value",
    "part_total_cycles_parse_status",
    "component_total_cycles_value",
    "component_total_cycles_parse_status",
]

COUNTER_COLUMNS = {
    "AircraftTotalTime": "aircraft_total_time",
    "AircraftTotalCycles": "aircraft_total_cycles",
    "PartTotalCycles": "part_total_cycles",
    "ComponentTotalCycles": "component_total_cycles",
}

csv.field_size_limit(10_000_000)


def normalise_date(value: str) -> tuple[str, str]:
    """Return an ISO date and ``valid``, ``missing`` or ``invalid`` status."""
    raw = (value or "").strip()
    if not raw:
        return "", "missing"
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat(), "valid"
        except ValueError:
            pass
    return "", "invalid"


def normalise_timestamp(value: str) -> tuple[str, str]:
    """Return an RFC3339-ish timestamp and a parse status without coercion."""
    raw = (value or "").strip()
    if not raw:
        return "", "missing"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "", "invalid"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return "", "missing_timezone"
    return parsed.isoformat(), "valid"


def parse_counter(value: str) -> tuple[str, str]:
    """Parse only integer representations; preserve all other raw values."""
    raw = (value or "").strip()
    if not raw:
        return "", "missing"
    if re.fullmatch(r"\d+", raw) and int(raw) <= 2**63 - 1:
        return raw, "valid"
    if re.fullmatch(r"\d+\.0+", raw) and int(raw.split(".", 1)[0]) <= 2**63 - 1:
        return raw.split(".", 1)[0], "valid_integer_decimal"
    return "", "invalid"


def build_row(
    row: dict[str, str],
    year: str,
    source_file: str,
    row_number: int,
    *,
    source_snapshot_hash: str = "",
) -> dict[str, str]:
    """Add conservative canonical fields while retaining source columns."""
    # Keep source fields byte-for-byte at the CSV field level. Parsers below
    # trim only their working copy; raw counters remain auditable.
    output = {key: (value or "") for key, value in row.items() if key is not None}
    difficulty, difficulty_status = normalise_date(output.get("DifficultyDate", ""))
    submission, submission_status = normalise_timestamp(
        output.get("SubmissionDate", "")
    )
    output.update(
        source_year=year,
        source_file=source_file,
        source_row_number=str(row_number),
        source_snapshot_hash=source_snapshot_hash,
        difficulty_date_iso=difficulty,
        submission_timestamp_iso=submission,
        difficulty_date_valid=difficulty_status,
        submission_timestamp_valid=submission_status,
    )
    for source_name, prefix in COUNTER_COLUMNS.items():
        parsed, status = parse_counter(output.get(source_name, ""))
        output[f"{prefix}_value"] = parsed
        output[f"{prefix}_parse_status"] = status
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=SDR_DIR)
    parser.add_argument("--output", type=Path, default=OUT_CSV)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    header: list[str] | None = None
    scanned = 0
    with args.output.open("w", newline="", encoding="utf-8") as output:
        writer = None
        for year in SDR_YEARS:
            path = args.source_dir / f"SDR-{year}.csv"
            snapshot_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            with path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                file_header = list(reader.fieldnames or [])
                if header is None:
                    header = file_header
                    if set(header) & set(META_COLUMNS):
                        raise SystemExit("FAA source header collides with metadata")
                    writer = csv.DictWriter(output, fieldnames=header + META_COLUMNS)
                    writer.writeheader()
                elif file_header != header:
                    raise SystemExit(f"header mismatch in {path}")
                for row_number, row in enumerate(reader, start=1):
                    if None in row:
                        raise SystemExit(f"extra CSV fields in {path}:{row_number}")
                    writer.writerow(
                        build_row(
                            row,
                            year,
                            path.name,
                            row_number,
                            source_snapshot_hash=snapshot_hash,
                        )
                    )
                    scanned += 1

    print(f"sdr rows scanned : {scanned}")
    print(f"csv              : {args.output}")
    print(f"schema           : {SCHEMA_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
