from __future__ import annotations

from amos_data.cohorts import (
    assess_horizon_label,
    build_installation_candidates,
    build_split_manifest,
    build_workorder_split_index,
    normalized_identifier,
)


def _row(
    workorder: str,
    date: str,
    tac: int,
    *,
    on_pn: str = "PN-1",
    on_serial: str = "S-1",
    off_pn: str = "OTHER",
    off_serial: str = "OTHER-1",
    position: str = "LEFT",
    description: str = "unique narrative",
) -> dict:
    return {
        "workorder_uuid": workorder,
        "envelope": {"envelope_ts": "2026-09-15T15:40:01+00:00"},
        "aircraft": {"msn": "123"},
        "closing": {"date": date, "total_aircraft_cycles": tac},
        "issue": {"description": description},
        "work_steps": [
            {
                "description": description,
                "actions": [
                    {
                        "action_text": description,
                        "component_changes": [
                            {
                                "position": position,
                                "part_on_number": on_pn,
                                "part_on_serial": on_serial,
                                "part_off_number": off_pn,
                                "part_off_serial": off_serial,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_normalized_serial_pair_never_leaks_future_removal_into_origin() -> None:
    rows = [
        _row("install", "2020-01-01", 10, on_pn="PN-1", on_serial="S-1"),
        _row(
            "remove",
            "2020-02-01",
            20,
            on_pn="OTHER",
            on_serial="OTHER-2",
            off_pn="pn 1",
            off_serial="s-1",
        ),
    ]
    candidates, _ = build_installation_candidates(rows, target_pns=("PN-1",))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.pn == normalized_identifier("PN-1")
    assert candidate.prediction_date.isoformat() == "2020-01-01"
    assert candidate.removal_date.isoformat() == "2020-02-01"
    assert candidate.install_workorder_id == "install"
    assert candidate.removal_workorder_id == "remove"


def test_same_day_or_counter_regression_is_not_a_temporal_candidate() -> None:
    rows = [
        _row("install", "2020-01-01", 20),
        _row(
            "remove",
            "2020-01-01",
            30,
            on_pn="OTHER",
            on_serial="OTHER-2",
            off_pn="PN-1",
            off_serial="S-1",
        ),
    ]
    candidates, diagnostics = build_installation_candidates(rows)
    assert candidates == []
    assert diagnostics["candidate_non_increasing_or_ambiguous_date"] == 1


def test_unknown_is_not_negative_without_horizon_and_observed_exposure() -> None:
    assert assess_horizon_label("scheduled_or_serviceable", False, True, horizon_cycles=100, valid_origin=True).status == "unknown"
    assert assess_horizon_label("scheduled_or_serviceable", True, True, horizon_cycles=100, valid_origin=True).status == "negative"
    assert assess_horizon_label("confirmed_failure", True, False, horizon_cycles=100, valid_origin=True).status == "unknown"
    assert assess_horizon_label("confirmed_failure", True, True, horizon_cycles=100, valid_origin=True).status == "unknown"
    assert assess_horizon_label("confirmed_failure", True, True, horizon_cycles=100, valid_origin=True, event_delta_cycles=50).status == "positive"


def test_serial_punctuation_is_not_normalized_away() -> None:
    rows = [
        _row("install", "2020-01-01", 10, on_serial="S-1"),
        _row(
            "remove",
            "2020-02-01",
            20,
            on_pn="OTHER",
            on_serial="OTHER-2",
            off_pn="PN-1",
            off_serial="S1",
        ),
    ]
    candidates, _ = build_installation_candidates(rows)
    assert candidates == []


def test_repeated_endpoint_is_rejected_instead_of_arbitrarily_paired() -> None:
    install = _row("install", "2020-01-01", 10, on_serial="S-1")
    removal = _row(
        "remove", "2020-02-01", 20, on_pn="OTHER", on_serial="X-1", off_pn="PN-1", off_serial="S-1"
    )
    candidates, diagnostics = build_installation_candidates([install, install, removal])
    assert candidates == []
    assert diagnostics["candidate_ambiguous_repeated_endpoint"] == 1


def test_duplicate_narratives_are_purged_into_one_split_group() -> None:
    rows = [
        _row("one-install", "2010-01-01", 10, on_serial="S-1", description="same text"),
        _row(
            "one-remove",
            "2010-02-01",
            20,
            on_pn="OTHER",
            on_serial="X-1",
            off_pn="PN-1",
            off_serial="S-1",
            description="same text",
        ),
        _row("two-install", "2021-01-01", 30, on_serial="S-2", description="same text"),
        _row(
            "two-remove",
            "2021-02-01",
            40,
            on_pn="OTHER",
            on_serial="X-2",
            off_pn="PN-1",
            off_serial="S-2",
            description="same text",
        ),
    ]
    candidates, _ = build_installation_candidates(rows, target_pns=("PN-1",))
    manifest = build_split_manifest(candidates, "a" * 64, "fixture.ndjson")
    assert len(manifest["assignments"]) == 1
    assert manifest["assignments"][0]["candidate_count"] == 2
    assert manifest["assignments"][0]["failure_training_eligible"] is False


def test_closed_snapshot_text_is_not_prediction_time_and_duplicate_cannot_cross_split() -> None:
    rows = [
        _row("early-install", "2010-01-01", 10, on_pn="2085M31G03", on_serial="S-1", description="same text"),
        _row("early-remove", "2010-02-01", 20, on_pn="OTHER", on_serial="X-1", off_pn="2085M31G03", off_serial="S-1", description="same text"),
        _row("late-install", "2027-01-01", 30, on_pn="2085M31G03", on_serial="S-2", description="same text"),
        _row("late-remove", "2027-02-01", 40, on_pn="OTHER", on_serial="X-2", off_pn="2085M31G03", off_serial="S-2", description="same text"),
    ]
    candidates, _ = build_installation_candidates(rows)
    manifest = build_split_manifest(candidates, "b" * 64, "fixture.ndjson", rows)
    index = build_workorder_split_index(candidates, rows, manifest)
    assert {entry["split"] for entry in index.values()} == {"final_test"}
    assert all(not entry["retrieval_index_eligible"] for entry in index.values())
    assert all(not entry["prediction_time_symptom"]["available"] for entry in index.values())
    assert index["early-install"]["historical_case"]["available_from"] == "2026-09-15T15:40:01+00:00"
