from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from amos_data.documents import build_documents, file_hash, split_text


def _write_manifest(index: Path, source_hash: str, record_id: str, *, split: str = "train", duplicate: bool = False) -> None:
    manifest = {
        "freeze_id": "frozen", "immutable": {"policy": "write_once"},
        "source": {"sha256": source_hash},
    }
    index.with_name("bigquery-feasibility-v2-split-manifest-2026-09-22.json").write_text(json.dumps(manifest))
    index.write_text(json.dumps({
        "source_workorder_id": record_id, "split": split,
        "retrieval_index_eligible": not duplicate, "duplicate_symptom_narrative_grouped": duplicate,
        "episode_group_id": "episode", "historical_case": {
            "availability_status": "snapshot_only", "available_from": "2026-09-15T00:00:00Z",
        },
    }) + "\n")


def _workorder(record_id: str, description: str) -> dict:
    return {
        "workorder_uuid": record_id, "workorder_number": "100", "ata_code": "2532",
        "aircraft": {"type_raw": "M73-82", "variant": "737-8200", "full_registration": "EI-TEST", "registration": "TEST"},
        "part_keys": ["62197-301-001"],
        "work_steps": [{"uuid": "step-1", "description": description, "headline": "ignored headline", "actions": [{"action_text": "diagnosis must not appear", "component_changes": [{"part_on_number": "62197-301-001"}]}]}],
    }


def test_builds_amos_symptom_without_actions_and_preserves_chunk_spans(tmp_path: Path) -> None:
    source = tmp_path / "workorders.ndjson.gz"
    row = _workorder("wo-1", "fault symptom\n" + "x" * 300)
    with gzip.open(source, "wt") as handle:
        handle.write(json.dumps(row) + "\n")
    index = tmp_path / "split.ndjson"
    _write_manifest(index, file_hash(source), "wo-1")

    docs, report = build_documents(amos_path=source, split_index_path=index, chunk_chars=128)

    assert len(docs) == 3
    assert all(doc["text_role"] == "symptom" for doc in docs)
    assert all("diagnosis" not in doc["normalized_text"] for doc in docs)
    assert all(doc["availability_status"] == "snapshot_only" for doc in docs)
    assert all(doc["aircraft_family"] == "737-8200" for doc in docs)
    assert all(doc["workorder_id"] == "wo-1" for doc in docs)
    assert all(doc["workorder_number"] == "100" for doc in docs)
    assert docs[0]["source_span"].startswith("work_steps[step-1].description:char:")
    assert report.documents == 3


@pytest.mark.parametrize("split,duplicate", [("final_test", False), ("purged", False), ("train", True)])
def test_excludes_held_out_and_duplicate_workorders(tmp_path: Path, split: str, duplicate: bool) -> None:
    source = tmp_path / "workorders.ndjson"
    source.write_text(json.dumps(_workorder("wo-1", "symptom")) + "\n")
    index = tmp_path / "split.ndjson"
    _write_manifest(index, hashlib.sha256(source.read_bytes()).hexdigest(), "wo-1", split=split, duplicate=duplicate)

    docs, report = build_documents(amos_path=source, split_index_path=index)

    assert docs == []
    assert sum(report.skipped.values()) == 1


def test_faa_is_context_only_without_explicit_snapshot_and_raw_model_is_unknown(tmp_path: Path) -> None:
    source = tmp_path / "workorders.ndjson"
    source.write_text("")
    index = tmp_path / "split.ndjson"
    _write_manifest(index, hashlib.sha256(source.read_bytes()).hexdigest(), "unused")
    faa = tmp_path / "sdr.csv"
    faa.write_text("OperatorControlNumber,Discrepancy,PartNumber,ComponentPartNumber,AircraftModel,JASCCode\nR-1,CB opens while heating,8201-11-0000-01,,737823,2532\n")

    docs, _ = build_documents(amos_path=source, split_index_path=index, faa_paths=[faa])

    assert docs[0]["availability_status"] == "context_only"
    assert docs[0]["available_at"] is None
    assert docs[0]["aircraft_family"] is None
    assert docs[0]["part_number_key"] == "820111000001"


def test_split_text_never_truncates_long_unbroken_text() -> None:
    text = "z" * 400
    chunks = split_text(text, max_chars=128)
    assert "".join(chunk[2] for chunk in chunks) == text
    assert chunks[-1][1] == len(text)


def test_heldout_text_hash_excludes_cross_workorder_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "workorders.ndjson"
    heldout = _workorder("heldout", "same final symptom")
    train = _workorder("train", "same final symptom")
    source.write_text("\n".join(map(json.dumps, (heldout, train))) + "\n")
    index = tmp_path / "split.ndjson"
    _write_manifest(index, hashlib.sha256(source.read_bytes()).hexdigest(), "heldout", split="final_test")
    with index.open("a") as handle:
        handle.write(json.dumps({
            "source_workorder_id": "train", "split": "train", "retrieval_index_eligible": True,
            "duplicate_symptom_narrative_grouped": False, "historical_case": {
                "availability_status": "snapshot_only", "available_from": "2026-09-15T00:00:00Z",
            },
        }) + "\n")
    docs, report = build_documents(amos_path=source, split_index_path=index)
    assert docs == []
    assert report.skipped["heldout_or_duplicate_text_hash"] == 1


def test_document_row_matches_committed_bigquery_schema(tmp_path: Path) -> None:
    source = tmp_path / "workorders.ndjson"
    source.write_text(json.dumps(_workorder("wo-1", "symptom")) + "\n")
    index = tmp_path / "split.ndjson"
    _write_manifest(index, hashlib.sha256(source.read_bytes()).hexdigest(), "wo-1")
    docs, _ = build_documents(amos_path=source, split_index_path=index)
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "deployment/terraform/shared/retrieval_documents_schema.json").read_text())
    assert set(docs[0]) == {field["name"] for field in schema}


def test_corpus_version_changes_with_chunk_or_faa_availability_policy(tmp_path: Path) -> None:
    source = tmp_path / "workorders.ndjson"
    source.write_text(json.dumps(_workorder("wo-1", "symptom")) + "\n")
    index = tmp_path / "split.ndjson"
    _write_manifest(index, hashlib.sha256(source.read_bytes()).hexdigest(), "wo-1")
    standard, _ = build_documents(amos_path=source, split_index_path=index)
    changed_chunk, _ = build_documents(amos_path=source, split_index_path=index, chunk_chars=128)
    changed_policy, _ = build_documents(
        amos_path=source, split_index_path=index, faa_snapshot_as_of="2026-01-01T00:00:00Z"
    )
    assert standard[0]["reference_corpus_version"] != changed_chunk[0]["reference_corpus_version"]
    assert standard[0]["reference_corpus_version"] != changed_policy[0]["reference_corpus_version"]
