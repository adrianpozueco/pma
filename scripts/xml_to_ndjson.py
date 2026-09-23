"""Backward-compatible command-line client for the shared AMOS record builder."""

from __future__ import annotations

from amos_data.records import (
    NDJSON_PATH,
    SCHEMA,
    SCHEMA_PATH,
    build_record,
    build_records,
    check_against_schema,
    main,
)

__all__ = [
    "NDJSON_PATH",
    "SCHEMA",
    "SCHEMA_PATH",
    "build_record",
    "build_records",
    "check_against_schema",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
