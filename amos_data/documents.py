"""Build immutable, citation-ready retrieval documents from the fixed corpus.

The builder deliberately has no cloud dependency.  It turns a parsed AMOS
snapshot and the raw FAA SDR CSV into canonical text units; embedding and
BigQuery writes are separate jobs.  In particular, closed AMOS workorders are
historical evidence, not proof that their text was visible at issue time.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEXT_PREPARATION_VERSION = "f2-normalize-v1"
DEFAULT_CHUNK_CHARS = 7_500
_KEY = re.compile(r"[^A-Z0-9]")
_IDENTIFIER = re.compile(r"[^A-Za-z0-9_.:-]")


def normalized_key(value: object) -> str | None:
    value = _KEY.sub("", str(value or "").upper())
    return value or None


def normalize_text(value: object) -> str:
    """Normalize encoding and whitespace without changing clinical meaning."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    """Hash logical NDJSON bytes, matching the frozen audit for ``.gz`` input."""
    digest = hashlib.sha256()
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:  # type: ignore[arg-type]
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(*parts: object) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_timestamp(value: object) -> str | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _date_timestamp(value: object) -> str | None:
    if not value:
        return None
    try:
        parsed = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return (
        dt.datetime.combine(parsed, dt.time.min, dt.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _clean_identifier(value: object, fallback: str) -> str:
    result = _IDENTIFIER.sub("_", str(value or "").strip())
    return result or fallback


def split_text(
    text: str, *, max_chars: int = DEFAULT_CHUNK_CHARS
) -> list[tuple[int, int, str]]:
    """Split a long source unit on paragraph/sentence/word boundaries.

    Offsets always refer to the untouched raw source text.  The function does
    not truncate: a long word is retained as its own chunk and later rejected
    by the embedding API with ``auto_truncate=False`` if it exceeds its token
    limit.
    """
    if max_chars < 128:
        raise ValueError("max_chars must be at least 128")
    if not text:
        return []
    chunks: list[tuple[int, int, str]] = []
    start = 0
    length = len(text)
    while start < length:
        while start < length and text[start].isspace():
            start += 1
        if start >= length:
            break
        end = min(start + max_chars, length)
        if end < length:
            window = text[start:end]
            candidates = [
                window.rfind("\n\n"),
                window.rfind("\n"),
                max(window.rfind(". "), window.rfind("! "), window.rfind("? ")),
                window.rfind(" "),
            ]
            cut = next((index for index in candidates if index >= max_chars // 2), -1)
            if cut >= 0:
                end = start + cut + (2 if window[cut : cut + 2] == "\n\n" else 1)
        raw = text[start:end]
        # Leading/trailing whitespace is not part of the semantic text but the
        # citation remains exact via the raw offset.
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right > left:
            chunks.append((start + left, start + right, raw[left:right]))
        start = end
    return chunks


@dataclass(frozen=True)
class SplitAssignment:
    split: str
    retrieval_index_eligible: bool
    duplicate: bool
    available_at: str | None
    availability_status: str
    episode_group_id: str | None = None


@dataclass
class BuildReport:
    source_hashes: dict[str, str] = field(default_factory=dict)
    emitted: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    documents: int = 0
    freeze_id: str | None = None

    def as_dict(self, *, corpus_version: str, freeze_id: str | None) -> dict[str, Any]:
        return {
            "document_count": self.documents,
            "emitted_by_source": dict(sorted(self.emitted.items())),
            "skipped_by_reason": dict(sorted(self.skipped.items())),
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "reference_corpus_version": corpus_version,
            "split_freeze_id": freeze_id or self.freeze_id,
            "text_preparation_version": TEXT_PREPARATION_VERSION,
        }


def load_split_index(
    path: Path, *, expected_source_hash: str | None = None
) -> tuple[dict[str, SplitAssignment], str | None]:
    """Load the immutable per-WO split index and validate its linked manifest."""
    manifest_path = path.with_name(
        "bigquery-feasibility-v2-split-manifest-2026-09-22.json"
    )
    freeze_id: str | None = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if not manifest.get("immutable", {}).get("policy") == "write_once":
            raise ValueError("split manifest is not marked immutable")
        source_hash = str(manifest.get("source", {}).get("sha256") or "")
        if expected_source_hash and source_hash != expected_source_hash:
            raise ValueError("AMOS source hash does not match frozen split manifest")
        freeze_id = str(manifest.get("freeze_id") or "") or None
    result: dict[str, SplitAssignment] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            key = str(row.get("source_workorder_id") or "")
            if not key or key in result:
                raise ValueError(
                    f"invalid or duplicate split-index key at line {line_number}"
                )
            historical = row.get("historical_case") or {}
            result[key] = SplitAssignment(
                split=str(row.get("split") or "purged"),
                retrieval_index_eligible=bool(row.get("retrieval_index_eligible")),
                duplicate=bool(row.get("duplicate_symptom_narrative_grouped")),
                available_at=_utc_timestamp(historical.get("available_from")),
                availability_status=str(
                    historical.get("availability_status") or "unknown"
                ),
                episode_group_id=str(row.get("episode_group_id") or "") or None,
            )
    return result, freeze_id


def _part_metadata(workorder: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    values: list[str] = list(workorder.get("part_keys") or [])
    for step in workorder.get("work_steps") or []:
        if not isinstance(step, Mapping):
            continue
        for action in step.get("actions") or []:
            if not isinstance(action, Mapping):
                continue
            for change in action.get("component_changes") or []:
                if not isinstance(change, Mapping):
                    continue
                values.extend(
                    [
                        change.get("part_off_number"),
                        change.get("part_on_number"),
                        change.get("part_key"),
                    ]
                )
    keys = sorted({key for value in values if (key := normalized_key(value))})
    component = workorder.get("component") or {}
    raw = component.get("part_number") if isinstance(component, Mapping) else None
    return str(raw) if raw else None, keys


def _amos_documents(
    workorders: Iterable[Mapping[str, Any]],
    assignments: Mapping[str, SplitAssignment],
    *,
    source_snapshot: str,
    corpus_version: str,
    chunk_chars: int,
    protected_text_hashes: set[str],
    report: BuildReport,
) -> Iterator[dict[str, Any]]:
    for workorder in workorders:
        record_id = str(workorder.get("workorder_uuid") or "")
        assignment = assignments.get(record_id)
        if assignment is None:
            report.skipped["missing_split_assignment"] += 1
            continue
        if assignment.split in {"final_test", "purged"}:
            report.skipped[f"split_{assignment.split}"] += 1
            continue
        if assignment.duplicate or not assignment.retrieval_index_eligible:
            report.skipped["duplicate_or_index_ineligible"] += 1
            continue
        if (
            assignment.availability_status != "snapshot_only"
            or not assignment.available_at
        ):
            report.skipped["unavailable_amos_snapshot"] += 1
            continue
        aircraft = workorder.get("aircraft") or {}
        raw_part, part_keys = _part_metadata(workorder)
        any_text = False
        for index, step in enumerate(workorder.get("work_steps") or []):
            if not isinstance(step, Mapping):
                continue
            # The description is the source of truth.  A headline is used only
            # when there is no description; actions/diagnoses are never joined.
            field = "description" if step.get("description") else "headline"
            raw_unit = str(step.get(field) or "")
            if not normalize_text(raw_unit):
                continue
            any_text = True
            step_id = _clean_identifier(
                step.get("uuid") or step.get("sequence_number"), f"step-{index}"
            )
            for chunk_index, (start, end, raw_chunk) in enumerate(
                split_text(raw_unit, max_chars=chunk_chars)
            ):
                normalized = normalize_text(raw_chunk)
                if not normalized:
                    continue
                if text_hash(normalized) in protected_text_hashes:
                    report.skipped["heldout_or_duplicate_text_hash"] += 1
                    continue
                chunk_id = f"{step_id}:{field}:{chunk_index:04d}"
                yield {
                    "document_id": stable_id(
                        "amos",
                        source_snapshot,
                        record_id,
                        chunk_id,
                        TEXT_PREPARATION_VERSION,
                    ),
                    "source_namespace": "amos",
                    "source_snapshot": source_snapshot,
                    "record_id": record_id,
                    "workorder_id": record_id,
                    "workorder_number": workorder.get("workorder_number"),
                    "step_or_segment_id": step_id,
                    "chunk_id": chunk_id,
                    "text_role": "symptom",
                    "source_span": f"work_steps[{step_id}].{field}:char:{start}-{end}",
                    "raw_text": raw_chunk,
                    "normalized_text": normalized,
                    "text_hash": text_hash(normalized),
                    "text_preparation_version": TEXT_PREPARATION_VERSION,
                    "available_at": assignment.available_at,
                    "availability_status": "snapshot_only",
                    "split": assignment.split,
                    "reference_corpus_version": corpus_version,
                    "part_number_raw": raw_part,
                    "part_number_key": normalized_key(raw_part),
                    "component_part_number_raw": None,
                    "component_part_number_key": None,
                    "part_keys": part_keys,
                    "component_class": None,
                    # Parser variants are the shared query-family vocabulary
                    # (for example B737-8 -> 737-800), unlike raw AMOS types.
                    "aircraft_family": aircraft.get("variant")
                    if isinstance(aircraft, Mapping)
                    else None,
                    "aircraft_id": aircraft.get("full_registration")
                    if isinstance(aircraft, Mapping)
                    else None,
                    "ata_chapter": workorder.get("ata_chapter"),
                    "ata_code": workorder.get("ata_code"),
                    "position": (workorder.get("position_info") or {}).get("position")
                    if isinstance(workorder.get("position_info"), Mapping)
                    else None,
                    "quality_flags": [
                        "closed_snapshot",
                        "historical_evidence_only",
                        f"episode:{assignment.episode_group_id}",
                    ]
                    if assignment.episode_group_id
                    else ["closed_snapshot", "historical_evidence_only"],
                    "created_at": None,
                }
        if not any_text:
            report.skipped["empty_amos_symptom"] += 1


def _faa_documents(
    paths: Iterable[Path],
    *,
    source_snapshot: str,
    corpus_version: str,
    snapshot_as_of: str | None,
    chunk_chars: int,
    protected_text_hashes: set[str],
    report: BuildReport,
) -> Iterator[dict[str, Any]]:
    available_at = _utc_timestamp(snapshot_as_of)
    availability_status = "available" if available_at else "context_only"
    for path in paths:
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=1):
                raw_unit = str(row.get("Discrepancy") or "")
                if not normalize_text(raw_unit):
                    report.skipped["empty_faa_discrepancy"] += 1
                    continue
                record_id = _clean_identifier(
                    row.get("OperatorControlNumber"), f"{path.stem}-{row_number}"
                )
                part_raw = str(row.get("PartNumber") or "") or None
                component_raw = str(row.get("ComponentPartNumber") or "") or None
                keys = sorted(
                    {
                        key
                        for value in (part_raw, component_raw)
                        if (key := normalized_key(value))
                    }
                )
                for chunk_index, (start, end, raw_chunk) in enumerate(
                    split_text(raw_unit, max_chars=chunk_chars)
                ):
                    normalized = normalize_text(raw_chunk)
                    if not normalized:
                        continue
                    if text_hash(normalized) in protected_text_hashes:
                        report.skipped["heldout_or_duplicate_text_hash"] += 1
                        continue
                    chunk_id = f"discrepancy:{chunk_index:04d}"
                    yield {
                        "document_id": stable_id(
                            "faa_sdr",
                            source_snapshot,
                            path.name,
                            record_id,
                            row_number,
                            chunk_id,
                            TEXT_PREPARATION_VERSION,
                        ),
                        "source_namespace": "faa_sdr",
                        "source_snapshot": source_snapshot,
                        "record_id": record_id,
                        "workorder_id": None,
                        "workorder_number": None,
                        "step_or_segment_id": "discrepancy",
                        "chunk_id": chunk_id,
                        "text_role": "historical_case",
                        "source_span": f"{path.name}:row:{row_number}:Discrepancy:char:{start}-{end}",
                        "raw_text": raw_chunk,
                        "normalized_text": normalized,
                        "text_hash": text_hash(normalized),
                        "text_preparation_version": TEXT_PREPARATION_VERSION,
                        "available_at": available_at,
                        "availability_status": availability_status,
                        "split": "context_only" if not available_at else "train",
                        "reference_corpus_version": corpus_version,
                        "part_number_raw": part_raw,
                        "part_number_key": normalized_key(part_raw),
                        "component_part_number_raw": component_raw,
                        "component_part_number_key": normalized_key(component_raw),
                        "part_keys": keys,
                        "component_class": None,
                        # Raw FAA model names must not be mapped to AMOS families
                        # without a reviewed mapping artifact.
                        "aircraft_family": None,
                        "aircraft_id": None,
                        "ata_chapter": None,
                        "ata_code": row.get("JASCCode") or None,
                        "position": row.get("PartLocation")
                        or row.get("ComponentLocation")
                        or None,
                        "quality_flags": [
                            "faa_full_narrative",
                            "publication_time_unverified",
                        ]
                        if not available_at
                        else ["faa_full_narrative", "snapshot_as_of_supplied"],
                        "created_at": None,
                    }


def _read_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[arg-type]
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _protected_amos_text_hashes(
    workorders: Iterable[Mapping[str, Any]],
    assignments: Mapping[str, SplitAssignment],
    *,
    chunk_chars: int,
) -> set[str]:
    """Text from final/purged/duplicate source cases cannot re-enter as a clone."""
    hashes: set[str] = set()
    for workorder in workorders:
        assignment = assignments.get(str(workorder.get("workorder_uuid") or ""))
        if assignment is None or (
            assignment.split not in {"final_test", "purged"}
            and not assignment.duplicate
        ):
            continue
        for step in workorder.get("work_steps") or []:
            if not isinstance(step, Mapping):
                continue
            raw = str(step.get("description") or step.get("headline") or "")
            for _, _, chunk in split_text(raw, max_chars=chunk_chars):
                normalized = normalize_text(chunk)
                if normalized:
                    hashes.add(text_hash(normalized))
    return hashes


def build_documents(
    *,
    amos_path: Path,
    split_index_path: Path,
    faa_paths: Iterable[Path] = (),
    faa_snapshot_as_of: str | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
) -> tuple[list[dict[str, Any]], BuildReport]:
    """Build deterministic documents and an explicit coverage report in memory."""
    amos_hash = file_hash(amos_path)
    assignments, freeze_id = load_split_index(
        split_index_path, expected_source_hash=amos_hash
    )
    faa_paths = tuple(faa_paths)
    report = BuildReport(source_hashes={str(amos_path): amos_hash}, freeze_id=freeze_id)
    for path in faa_paths:
        report.source_hashes[str(path)] = file_hash(path)
    availability_policy = "faa_snapshot_as_of:" + (
        _utc_timestamp(faa_snapshot_as_of) or "context_only"
    )
    corpus_version = (
        "f2-"
        + stable_id(
            TEXT_PREPARATION_VERSION,
            freeze_id,
            f"chunk_chars:{chunk_chars}",
            availability_policy,
            *(report.source_hashes[key] for key in sorted(report.source_hashes)),
        )[:20]
    )
    workorders = list(_read_ndjson(amos_path))
    protected_text_hashes = _protected_amos_text_hashes(
        workorders, assignments, chunk_chars=chunk_chars
    )
    documents = list(
        _amos_documents(
            workorders,
            assignments,
            source_snapshot=f"sha256:{amos_hash}",
            corpus_version=corpus_version,
            chunk_chars=chunk_chars,
            protected_text_hashes=protected_text_hashes,
            report=report,
        )
    )
    documents.extend(
        _faa_documents(
            faa_paths,
            source_snapshot="sha256:"
            + stable_id(*(report.source_hashes[str(path)] for path in faa_paths)),
            corpus_version=corpus_version,
            snapshot_as_of=faa_snapshot_as_of,
            chunk_chars=chunk_chars,
            protected_text_hashes=protected_text_hashes,
            report=report,
        )
    )
    # A repeated source text is one vector candidate, even if unrelated source
    # records refer to many component changes. Keep the first deterministic ID.
    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for document in documents:
        key = (
            document["source_namespace"],
            document["text_hash"],
            document["text_role"],
            document["reference_corpus_version"],
        )
        if key in deduped:
            report.skipped["duplicate_text_unit"] += 1
            continue
        deduped[key] = document
        report.emitted[document["source_namespace"]] += 1
    output = sorted(deduped.values(), key=lambda item: item["document_id"])
    report.documents = len(output)
    return output, report


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "TEXT_PREPARATION_VERSION",
    "BuildReport",
    "SplitAssignment",
    "build_documents",
    "file_hash",
    "load_split_index",
    "normalize_text",
    "normalized_key",
    "split_text",
    "stable_id",
    "text_hash",
]
