import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from amos_data.retrieval import LocalHistoryProvider
from pm_agent.prediction.contracts import (
    CurrentTac,
    IntervalStats,
    PredictionDecision,
    PredictionReason,
    PredictionResult,
)
from pm_agent.workorders import (
    AnalysisInput,
    WorkOrderAnalysisError,
    WorkOrderAnalysisService,
)
from pm_agent.workorders.artifacts import (
    analyze_current_session_artifact,
    validate_artifact_filename,
)
from pm_agent.workorders.chat import (
    render_chat_upload,
    render_uploaded_workorder_summary,
)
from pm_agent.workorders.service import _resolve_position, _text_hash, build_pma_wo_text

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workorders"
PMA_FIXTURES = Path(__file__).parents[1] / "fixtures" / "pma"


def _request(**overrides):
    values = {
        "mode": "new_work_order",
        "analysis_as_of": "2026-09-01T10:00:00+00:00",
        "current_aircraft_tac": 120,
        "current_tac_source": "operator_feed",
        "current_tac_observed_at": "2026-09-01T09:55:00+00:00",
    }
    values.update(overrides)
    return AnalysisInput(**values)


def test_analysis_preserves_roles_counters_and_never_uses_action_text():
    result = WorkOrderAnalysisService().analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(), _request()
    )
    target = result["target_parts"][0]
    assert target["part_number"] == "2085M31G03"
    assert {"component", "requested"} <= set(target["roles"])
    assert result["parsed_context"]["issue"]["tac"]["value"] == 100
    assert result["parsed_context"]["current_aircraft_tac"]["value"] == 120
    assert result["parsed_context"]["closing"]["tac"]["value"] is None
    assert "REPLACED" not in "\n".join(
        str(x) for x in result["parsed_context"]["symptoms"]
    )
    assert result["prediction"]["value"] is None
    assert result["replacement_recommendation"]["value"] is None


def test_new_workorder_without_current_tac_does_not_promote_issue_counter():
    result = WorkOrderAnalysisService().analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(),
        AnalysisInput(
            mode="new_work_order", analysis_as_of="2026-09-01T10:00:00+00:00"
        ),
    )
    assert result["parsed_context"]["current_aircraft_tac"]["status"] == "not_supplied"
    assert result["parsed_context"]["issue"]["tac"]["value"] == 100


def test_multi_workorder_requires_explicit_selection():
    with pytest.raises(WorkOrderAnalysisError) as error:
        WorkOrderAnalysisService().analyze_xml(
            (FIXTURES / "two_workorders.xml").read_bytes(), _request()
        )
    assert error.value.code == "workorder_selection_required"


def test_closed_export_requires_replay_and_replay_excludes_actions():
    closed = (FIXTURES / "closed_boiler.xml").read_bytes()
    with pytest.raises(WorkOrderAnalysisError, match="historical_replay"):
        WorkOrderAnalysisService().analyze_xml(closed, _request())
    result = WorkOrderAnalysisService().analyze_xml(
        closed,
        AnalysisInput(
            mode="historical_replay", analysis_as_of="2026-02-02T11:00:00+00:00"
        ),
    )
    assert any("unverified snapshot" in item for item in result["limitations"])
    assert "Completed replacement" not in str(result["parsed_context"])


@pytest.mark.parametrize("value", ["user:other.xml", "../other.xml", "/tmp/other.xml"])
def test_artifact_adapter_rejects_user_paths(value):
    with pytest.raises(WorkOrderAnalysisError) as error:
        validate_artifact_filename(value)
    assert error.value.code == "invalid_artifact_reference"


def test_future_tac_and_naive_analysis_time_are_rejected():
    # Validation of the future relation occurs against the analysis request.
    with pytest.raises(WorkOrderAnalysisError) as error:
        WorkOrderAnalysisService().analyze_xml(
            (FIXTURES / "open_nozzle.xml").read_bytes(),
            _request(current_tac_observed_at="2026-09-01T11:00:00+00:00"),
        )
    assert error.value.code == "future_current_tac"
    with pytest.raises(WorkOrderAnalysisError) as error:
        AnalysisInput(mode="new_work_order", analysis_as_of="2026-09-01T10:00:00")
    assert error.value.code == "timezone_required"


@pytest.mark.asyncio
async def test_artifact_adapter_reads_adk_inline_part_and_keeps_version_zero():
    from google.genai import types

    class Context:
        async def load_artifact(self, *, filename, version=None):
            assert (filename, version) == ("workorder.xml", 0)
            return types.Part(
                inline_data=types.Blob(
                    data=(FIXTURES / "open_nozzle.xml").read_bytes(),
                    mime_type="application/xml",
                )
            )

    result = await analyze_current_session_artifact(
        Context(), "workorder.xml", _request(), WorkOrderAnalysisService(), version=0
    )
    assert result["wo_id"] == "wo-open"


@pytest.mark.asyncio
async def test_artifact_adapter_rejects_non_xml_mime_before_parsing():
    from google.genai import types

    class Context:
        async def load_artifact(self, *, filename, version=None):
            return types.Part(
                inline_data=types.Blob(data=b"not xml", mime_type="text/plain")
            )

    with pytest.raises(WorkOrderAnalysisError) as error:
        await analyze_current_session_artifact(
            Context(),
            "workorder.xml",
            _request(),
            WorkOrderAnalysisService(),
            version=0,
        )
    assert error.value.code == "invalid_artifact_mime"


def test_alias_target_is_candidate_and_future_action_change_is_not_used_in_new_mode():
    source = (FIXTURES / "open_nozzle.xml").read_text().replace("2085M31G03", "OTHER")
    source = source.replace("Fuel nozzle leak observed", "water boiler trips breaker")
    source = source.replace(
        "<actionText>REPLACED NOZZLE AFTER INSPECTION</actionText>",
        "<performedData><performedDateTime>2026-10-01T00:00:00Z</performedDateTime></performedData><componentChanges><componentChange><partOn><partNumber>8201-11-0000-01</partNumber></partOn></componentChange></componentChanges>",
    )
    result = WorkOrderAnalysisService().analyze_xml(source.encode(), _request())
    assert result["target_parts"] == [
        {
            "part_number": "62197-301-001",
            "part_key": "62197301001",
            "description": "Water boiler",
            "resolution_status": "candidate_alias",
            "roles": ["alias_candidate"],
            "supporting_input": ["alias_candidate"],
        }
    ]


def test_replay_before_export_masks_component_and_future_action_target_evidence():
    source = (FIXTURES / "closed_boiler.xml").read_text()
    source = source.replace(
        "<amosTransportEnvelope>",
        "<amosTransportEnvelope><header><date>2026-03-01T00:00:00Z</date></header>",
    )
    source = source.replace(
        "<workorderState>C</workorderState>",
        "<workorderState>C</workorderState><component><partNumber>2085M31G03</partNumber></component>",
    )
    source = source.replace(
        "<actionText>Completed replacement</actionText>",
        "<performedData><performedDateTime>2026-03-02T00:00:00Z</performedDateTime></performedData><componentChanges><componentChange><partOn><partNumber>8201-11-0000-01</partNumber></partOn></componentChange></componentChanges>",
    )
    result = WorkOrderAnalysisService().analyze_xml(
        source.encode(),
        AnalysisInput(
            mode="historical_replay", analysis_as_of="2026-02-02T11:00:00+00:00"
        ),
    )
    assert result["target_parts"] == []
    assert result["parsed_context"]["symptoms"] == []
    assert result["prediction"]["status"] == "out_of_scope"


@pytest.mark.parametrize("value", [True, 1.5])
def test_current_tac_rejects_non_integer_values(value):
    with pytest.raises(WorkOrderAnalysisError, match="integer"):
        _request(current_aircraft_tac=value)


def test_injected_history_provider_receives_only_as_of_symptoms_and_excludes_upload():
    provider = LocalHistoryProvider(
        [
            {
                "document_id": "old",
                "record_id": "old",
                "workorder_id": "old",
                "source_namespace": "amos",
                "text_role": "symptom",
                "raw_text": "fuel nozzle leak",
                "normalized_text": "fuel nozzle leak",
                "part_keys": ["2085M31G03"],
                "available_at": "2026-08-01T00:00:00+00:00",
                "availability_status": "available",
                "split": "train",
                "reference_corpus_version": None,
            },
            {
                "document_id": "self",
                "record_id": "self",
                "workorder_id": "wo-open",
                "source_namespace": "amos",
                "text_role": "symptom",
                "raw_text": "fuel nozzle leak",
                "normalized_text": "fuel nozzle leak",
                "part_keys": ["2085M31G03"],
                "available_at": "2026-08-01T00:00:00+00:00",
                "availability_status": "available",
                "split": "train",
                "reference_corpus_version": None,
            },
            {
                "document_id": "future",
                "record_id": "future",
                "workorder_id": "future",
                "source_namespace": "amos",
                "text_role": "symptom",
                "raw_text": "fuel nozzle leak",
                "normalized_text": "fuel nozzle leak",
                "part_keys": ["2085M31G03"],
                "available_at": "2026-10-01T00:00:00+00:00",
                "availability_status": "available",
                "split": "train",
                "reference_corpus_version": None,
            },
        ]
    )
    result = WorkOrderAnalysisService(provider).analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(), _request()
    )
    assert [case["case_id"] for case in result["historical_cases"]] == ["amos:old"]
    assert result["historical_cases"][0]["score_meaning"].endswith(
        "not a probability or outcome link"
    )


def test_history_excludes_same_family_self_by_uuid_or_number_and_duplicate_text():
    def document(document_id, **values):
        return {
            "document_id": document_id,
            "record_id": document_id,
            "workorder_id": document_id,
            "source_namespace": "amos",
            "text_role": "symptom",
            "raw_text": "Fuel nozzle leak observed during inspection",
            "normalized_text": "Fuel nozzle leak observed during inspection",
            "part_keys": ["2085M31G03"],
            "available_at": "2026-08-01T00:00:00+00:00",
            "availability_status": "available",
            "split": "train",
            "reference_corpus_version": None,
            "aircraft_family": "737-8200",
            "text_hash": _text_hash("Fuel nozzle leak observed during inspection"),
            **values,
        }

    provider = LocalHistoryProvider(
        [
            document("allowed"),
            document("uuid-self", workorder_id="wo-open"),
            document("number-self", workorder_id="OPEN-1"),
            document(
                "duplicate",
                raw_text="Fuel nozzle leak observed",
                normalized_text="Fuel nozzle leak observed",
                text_hash=_text_hash("Fuel nozzle leak observed"),
            ),
            document("wrong-family", aircraft_family="A320"),
        ]
    )
    result = WorkOrderAnalysisService(provider).analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(), _request()
    )
    assert [case["case_id"] for case in result["historical_cases"]] == ["amos:allowed"]


def _entries(text_value):
    """Whitespace-normalised entry multiset: split on the blank-line entry
    separator, strip each entry, drop empties."""
    return sorted(e.strip() for e in text_value.split("\n\n") if e.strip())


def test_build_pma_wo_text_matches_stored_wo_embeddings_content_rows():
    samples = json.loads(
        (PMA_FIXTURES / "wo_embeddings_content_sample.json").read_text()
    )
    assert len(samples) == 3

    row_1 = {
        "remarks": None,
        "work_steps": [
            {
                "sequence_number": "1",
                "description": "WINDOW LIGHT 8DEF INOP",
                "actions": [
                    {
                        "action_text": "WINDOW LIGHT 8DEF REPLACED IAW AMM 33-21-00-960-801 REV 24"
                    }
                ],
            }
        ],
    }
    row_2 = {
        "remarks": None,
        "work_steps": [
            {
                "sequence_number": "1",
                "description": 'REF TO HIL 55186764 "FWD BOILER INOP"',
                "actions": [
                    {
                        "action_text": "FWD BOILER REPLACMENT C/OUT IAW AMM 25-31-CAMO/0001 REV.88\nOP TEST C/OUT SATIS"
                    }
                ],
            },
            {
                "sequence_number": "2",
                "description": "",
                "actions": [{"action_text": "HIL CLOSED"}],
            },
        ],
    }
    row_3 = {
        "remarks": None,
        "work_steps": [
            {
                "sequence_number": str(i),
                "description": "",
                "actions": [{"action_text": text}],
            }
            for i, text in enumerate(
                [
                    "X-REF FROM WO - 104`892`227 - #2 ENG HIGH STAGE REGULATOR LEAKING - TO BE REPLACED\n"
                    "#2 HIGH STAGE REGULATOR REPLACED IAW AMM 36-11-07-000-801 & 36-11-07-400-801 REV 88",
                    "TQ WRENCH - TECH2FR100 - S/N 1223109791",
                    "INDEPENDENT INSPECTION CARRIED OUT IAW MOE ISS 2 REV 13 APPENDIX IV - CRITICAL TASKS",
                    "GEN VER CHECK C/O",
                    "UK.145.00897",
                    "ASSISTED BY SUJ",
                ],
                start=1,
            )
        ],
    }

    for row, sample in zip((row_1, row_2, row_3), samples, strict=True):
        actual = build_pma_wo_text(row, include_actions=True)
        assert _entries(actual) == _entries(sample["wo_embeddings_content"])


def test_build_pma_wo_text_replay_excludes_all_action_text():
    row = {
        "remarks": "PILOT REPORT",
        "work_steps": [
            {
                "sequence_number": "1",
                "description": "AFT CARGO FIRE LOOP A - INOP",
                "actions": [{"action_text": "REPLACED FIRE LOOP DETECTOR"}],
            }
        ],
    }
    replay_text = build_pma_wo_text(row, include_actions=False)
    assert "REPLACED FIRE LOOP DETECTOR" not in replay_text
    assert replay_text == "PILOT REPORT\nAFT CARGO FIRE LOOP A - INOP"
    live_text = build_pma_wo_text(row, include_actions=True)
    assert "REPLACED FIRE LOOP DETECTOR" in live_text


def test_parsed_context_exposes_ata_chapter_and_normalized_position():
    request = AnalysisInput(
        mode="new_work_order", analysis_as_of="2026-09-24T10:00:00+00:00"
    )
    cargo = WorkOrderAnalysisService().analyze_xml(
        (FIXTURES / "open_cargo_smoke_detector.xml").read_bytes(), request
    )
    assert cargo["parsed_context"]["ata_chapter"] == "26-16"
    assert cargo["parsed_context"]["position"] == "AFT"

    landing_light = WorkOrderAnalysisService().analyze_xml(
        (FIXTURES / "open_landing_light_rh.xml").read_bytes(), request
    )
    assert landing_light["parsed_context"]["ata_chapter"] == "33-41"
    assert landing_light["parsed_context"]["position"] == "RH"


def test_predictor_none_is_disabled_and_keeps_legacy_blocks_identical():
    result = WorkOrderAnalysisService().analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(), _request()
    )
    assert result["pma"]["reason"] == "prediction_disabled"
    assert result["pma"]["decision"] == "no_reliable_prediction"
    # Golden copy of today's (pre-prediction) legacy output: constructing the
    # service without a predictor must never change these three blocks.
    assert result["prediction"] == {
        "status": "model_unavailable",
        "reason": "No validated failure-risk model is configured for this fixed corpus.",
        "value": None,
        "model_version": None,
        "feature_version": None,
        "target_event": "confirmed_component_failure",
        "origin_timestamp": "2026-09-01T09:55:00+00:00",
        "origin_tac": 120,
        "horizon_cycles": None,
        "probability": None,
        "training_cutoff": None,
        "applicability": None,
    }
    assert result["timing"] == {
        "status": "unavailable",
        "reason": "No validated time-to-failure model is configured.",
        "value": None,
        "model_version": None,
        "feature_version": None,
        "unit": "aircraft_flight_cycles",
        "estimable_quantiles": None,
        "forecast": None,
    }
    assert result["replacement_recommendation"] == {
        "status": "unavailable",
        "reason": "No reviewed replacement policy or documented cycle limit is configured.",
        "value": None,
        "cycles_remaining": None,
        "due_counter": None,
        "basis": None,
        "applicability": None,
    }


class _FakeHistoricalIntervalPredictor:
    def predict(self, prediction_input):
        return PredictionResult(
            decision=PredictionDecision.HISTORICAL_INTERVAL,
            reason=None,
            aircraft=prediction_input.aircraft_reg,
            workorder_id=prediction_input.wo_id,
            analysis_as_of=prediction_input.analysis_as_of,
            mode=prediction_input.mode,
            interval=IntervalStats(
                basis="closing_tac_to_closing_tac_interval",
                unit="aircraft_flight_cycles",
                p50=120.0,
                p90=340.0,
                min=10,
                max=500,
                n=8,
            ),
        )


def test_fake_historical_interval_predictor_fills_timing_quantiles_and_status():
    service = WorkOrderAnalysisService(predictor=_FakeHistoricalIntervalPredictor())
    result = service.analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(), _request()
    )
    assert result["pma"]["decision"] == "historical_interval"
    assert result["prediction"]["status"] == "historical_interval_only"
    assert result["timing"]["status"] == "historical_interval_only"
    assert result["timing"]["estimable_quantiles"] == {
        "p50": 120.0,
        "p90": 340.0,
        "unit": "aircraft_flight_cycles",
        "basis": "closing_tac_to_closing_tac_interval",
    }
    assert result["timing"]["forecast"] is None


@pytest.mark.parametrize(
    "service",
    [
        WorkOrderAnalysisService(),
        WorkOrderAnalysisService(predictor=_FakeHistoricalIntervalPredictor()),
    ],
)
def test_due_counter_is_always_null(service):
    result = service.analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(), _request()
    )
    assert result["replacement_recommendation"]["due_counter"] is None


def test_positional_construction_of_service_still_works():
    provider = LocalHistoryProvider([])
    service = WorkOrderAnalysisService(provider)
    assert service.history_provider is provider
    assert service.predictor is None
    result = service.analyze_xml(
        (FIXTURES / "open_nozzle.xml").read_bytes(), _request()
    )
    assert result["wo_id"] == "wo-open"


def test_position_falls_back_to_unique_component_change_position_in_replay_only():
    from datetime import UTC, datetime

    as_of = datetime(2026, 1, 2, tzinfo=UTC)
    row = {
        "position_info": {"position": None},
        "workorder_state": "open",
        "work_steps": [
            {
                "actions": [
                    {
                        "performed_ts": "2026-01-01T00:00:00Z",
                        "component_changes": [{"position": " aft cargo "}],
                    }
                ]
            }
        ],
    }
    assert _resolve_position(row, as_of, "historical_replay") == "AFT"
    assert _resolve_position(row, as_of, "new_work_order") is None
    row["work_steps"][0]["actions"][0]["component_changes"].append({"position": "FWD"})
    assert _resolve_position(row, as_of, "historical_replay") is None
    row["position_info"]["position"] = "eng 1"
    assert _resolve_position(row, as_of, "historical_replay") == "#1"


class _FakeMatchedNoIntervalPredictor:
    """PMA matched a component the legacy IPC target list does not know and
    read the last closing TAC from BigQuery (the manual AFT test case)."""

    def predict(self, prediction_input):
        return PredictionResult(
            decision=PredictionDecision.NO_RELIABLE_PREDICTION,
            reason=PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT,
            aircraft=prediction_input.aircraft_reg,
            workorder_id=prediction_input.wo_id,
            analysis_as_of=prediction_input.analysis_as_of,
            mode=prediction_input.mode,
            component_key="473597-5|AFT",
            part_number="473597-5",
            position="AFT",
            current_tac=CurrentTac(
                value=41250,
                source="latest_closing_tac",
                observed_at=datetime(2026, 8, 20, 22, 30, tzinfo=UTC),
                stale=True,
            ),
        )


def _no_tac_request():
    return _request(
        analysis_as_of="2026-09-24T10:00:00+00:00",
        current_aircraft_tac=None,
        current_tac_source=None,
        current_tac_observed_at=None,
    )


def test_pma_match_rewords_target_and_current_tac_limitations():
    result = WorkOrderAnalysisService(
        predictor=_FakeMatchedNoIntervalPredictor()
    ).analyze_xml(
        (FIXTURES / "open_cargo_smoke_detector.xml").read_bytes(), _no_tac_request()
    )
    limitations = result["limitations"]
    assert result["target_parts"] == []
    assert not any("No configured target part" in item for item in limitations)
    assert not any(
        "issue and closing TAC were not assumed" in item for item in limitations
    )
    assert (
        "Part 473597-5 is not in the configured IPC target-part list; it was "
        "resolved by the PMA component match (473597-5|AFT)."
    ) in limitations
    assert (
        "Current aircraft TAC was not supplied; the last closing TAC in BigQuery "
        "(41250 at 2026-08-20T22:30:00+00:00) is shown as context only and is "
        "not assumed current."
    ) in limitations


def test_without_pma_facts_the_legacy_limitations_stay_verbatim():
    result = WorkOrderAnalysisService().analyze_xml(
        (FIXTURES / "open_cargo_smoke_detector.xml").read_bytes(), _no_tac_request()
    )
    assert (
        "No configured target part was resolved from the uploaded work order."
        in result["limitations"]
    )
    assert (
        "Current aircraft TAC was not supplied; issue and closing TAC were not "
        "assumed current."
    ) in result["limitations"]


def _rendered(predictor):
    xml = (FIXTURES / "open_cargo_smoke_detector.xml").read_bytes()
    analysis = WorkOrderAnalysisService(predictor=predictor).analyze_xml(
        xml, _no_tac_request()
    )
    return render_chat_upload(
        {
            "status": "analyzed",
            "analysis": analysis,
            "source": {"filename": "cargo.xml", "version": 0},
            "uploaded_workorder": {
                "workorder_number": "CARGO-AFT-001",
                "is_closed": False,
                "recorded_actions": [],
            },
        }
    )


def test_chat_render_shows_pma_part_and_bigquery_tac_without_contradiction():
    text = _rendered(_FakeMatchedNoIntervalPredictor())
    assert (
        "- 473597-5: resolved by the PMA component match (473597-5\\|AFT); "
        "not in the IPC target list."
    ) in text
    assert (
        "Current aircraft TAC: not supplied; last closing TAC in BigQuery 41250 at "
        "2026-08-20T22:30:00+00:00 (context only)."
    ) in text
    assert "To select a target, reply target_part_number=PN." not in text
    assert "not recorded (not\\_supplied)" not in text
    assert "Current aircraft TAC is stale (last seen" in text


def test_chat_render_without_prediction_keeps_legacy_target_and_tac_lines():
    text = _rendered(None)
    assert "Current aircraft TAC: not recorded (not\\_supplied)." in text
    assert "To select a target, reply target_part_number=PN." in text
    assert "resolved by the PMA component match" not in text


# --- Option B projected replacement window (approved 2026-09-24, requirement 7) ---
# Hand-written ``pma`` dicts, per the shared "renderers consume the serialised
# pma dict" contract: no dependency on the prediction-core dataclasses/wiring
# a teammate is building in parallel (`pm_agent/prediction/service.py`).
# Live numbers from PMA-ONLINE-AGENT-plan.md's background section:
# 473597-5|AFT on SP-REG00374, last replaced at TAC 17938 (2026-04-25), fleet
# p50 356 / p90 ~1968.2, latest known TAC 18660 (722 cycles later).


def _hand_built_result(pma: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "analyzed",
        "analysis": {
            "parsed_context": {
                "aircraft": {"full_registration": "SP-REG00374", "variant": "A320"},
                "symptoms": [],
                "current_aircraft_tac": {"status": "not_supplied", "value": None},
                "closing": {"tac": {"value": None}},
            },
            "input_mode": "new_work_order",
            "analysis_as_of": "2026-09-24T10:00:00+00:00",
            "target_parts": [],
            "pma": pma,
            "limitations": [],
        },
        "source": {"filename": "aft.xml", "version": 0},
        "uploaded_workorder": {
            "workorder_number": "AFT-WINDOW-001",
            "is_closed": False,
            "recorded_actions": [],
        },
    }


def _aft_projected_window_pma(*, stale: bool = True) -> dict[str, Any]:
    return {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "component_key": "473597-5|AFT",
        "match": {"gate_basis": "anchor_vote"},
        # p90 carries BigQuery PERCENTILE_CONT float noise on purpose, to
        # exercise the integer-rounding fix (§7 requirement 7).
        "supporting_interval": {
            "p50": 356,
            "p90": 1968.2000000000003,
            "n": 27,
            "aircraft": 24,
        },
        "projected_window": {
            "last_replacement_tac": 17938,
            "last_replacement_date": "2026-04-25",
            "tac_p50": 18294,
            "tac_p90": 19906,
            "cycles_since_last_replacement": 722,
            "position": "between_p50_p90",
        },
        "current_tac": {
            "value": 18660,
            "source": "latest_closing_tac",
            "observed_at": "2026-08-20T22:30:00+00:00",
            "stale": stale,
        },
        "limitations": ["projected_window_not_a_forecast"],
    }


@pytest.mark.parametrize(
    "render",
    [render_chat_upload, lambda r: render_uploaded_workorder_summary(r)],
)
def test_render_projected_window_lines_both_renderers(render):
    text = render(_hand_built_result(_aft_projected_window_pma()))
    assert "Matched component: 473597-5\\|AFT (anchor\\_vote)." in text
    assert (
        "Fleet pattern (not a forecast): this component was replaced again after "
        "p50 356 / p90 1968 cycles (n=27, 24 aircraft)."
    ) in text
    assert (
        "Projected window for this aircraft (fleet pattern, not a forecast): last "
        "replaced at TAC 17938 (2026-04-25); if the pattern repeats, next "
        "replacement around TAC 18294\u201319906."
    ) in text
    assert (
        "Latest known TAC is 722 cycles after that replacement, past the fleet "
        "median but inside p90."
    ) in text
    assert (
        "Current aircraft TAC is stale (last seen 2026-08-20T22:30:00+00:00); "
        "the window is not adjusted for cycles flown since."
    ) in text
    # No BigQuery float noise leaked into the rendered text.
    assert "2000000000003" not in text


def test_render_stale_tac_keeps_old_wording_without_projected_window():
    pma = _aft_projected_window_pma()
    pma["projected_window"] = None
    pma["limitations"] = ["no_prior_replacement_on_aircraft"]
    text = render_chat_upload(_hand_built_result(pma))
    assert (
        "No earlier replacement of this component on this aircraft is recorded, "
        "so no aircraft-specific window is given."
    ) in text
    assert (
        "Current aircraft TAC is stale (last seen 2026-08-20T22:30:00+00:00); "
        "no due TAC is given."
    ) in text
    assert "the window is not adjusted for cycles flown since." not in text


# --- Condensed recommendation block (approved 2026-09-24, USER REQUEST: lead
# with p50/p90/p95, PMA-POC-plan.md §11.4-style shape). Hand-written
# ``pma["recommendation"]`` dicts, per the same "renderers consume the
# serialised dict" contract as the Option B tests above: no dependency on
# `pm_agent/prediction/contracts.py`'s `Recommendation` dataclass, which a
# teammate is building in parallel.


def _recommendation(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "component_key": "473597-5|AFT",
        "basis": "similar_workorders",
        "predicted_replacement": {
            "lead_tac_p50": 356,
            "lead_tac_p90": 1968,
            "lead_tac_p95": 2200,
            "tac_p50": 18294,
            "tac_p90": 19906,
            "tac_p95": 20138,
        },
        "confidence": {
            "level": "high",
            "similarity": 0.82,
            "sample_size": 12,
            "cv": 0.31,
        },
        "evidence": [
            {"wo_id": "190672272", "sim": 0.82},
            {"wo_id": "190672111", "sim": 0.79},
        ],
        "action": "recommend_inspection_or_part_planning",
        "label": "Heuristic estimate, not a calibrated forecast",
        "reference_tac": 18660,
        "reference_tac_source": "latest_closing_tac",
    }
    base.update(overrides)
    return base


def _pma_with_recommendation(**overrides: Any) -> dict[str, Any]:
    return {
        "decision": "no_reliable_prediction",
        "reason": "samples_not_symptom_to_replacement",
        "component_key": None,
        "recommendation": _recommendation(**overrides),
        "limitations": [],
    }


@pytest.mark.parametrize(
    "render",
    [render_chat_upload, lambda r: render_uploaded_workorder_summary(r)],
)
def test_recommendation_block_renders_first_with_all_fields(render):
    # reference_tac 18660 is >= p50 (18294) and < p90 (19906) -> "past p50".
    text = render(_hand_built_result(_pma_with_recommendation()))
    assert text.startswith(
        "**Recommendation: Plan inspection / part replacement — 473597-5\\|AFT**"
    )
    assert (
        "Replace around TAC 18294 (p50) · 19906 (p90) · 20138 (p95); "
        "latest known TAC 18660 → past p50."
    ) in text
    assert (
        "Confidence: high (n=12, CV 0.31, similarity 0.82) · similar work "
        "orders + lead-time history · heuristic, not a calibrated forecast."
    ) in text
    assert "Evidence: WO 190672272 (0.82), WO 190672111 (0.79)" in text
    assert "```json" in text
    # The block (header through the fenced JSON) comes before every other
    # section, separated from the rest of the answer by one blank line.
    recommendation_end = text.index("```\n\n")
    rest = text[recommendation_end + len("```\n\n") :]
    assert rest.startswith("Analysed uploaded XML")
    # Component key is raw (unescaped) inside the JSON block, unlike the
    # markdown text lines above it.
    json_start = text.index("```json\n") + len("```json\n")
    json_text = text[json_start : text.index("\n```", json_start)]
    payload = json.loads(json_text)
    assert payload == {
        "decision": "recommendation",
        "recommendations": [
            {
                "component_key": "473597-5|AFT",
                "basis": "similar_workorders",
                "predicted_replacement": {
                    "lead_tac_p50": 356,
                    "lead_tac_p90": 1968,
                    "lead_tac_p95": 2200,
                    "tac_p50": 18294,
                    "tac_p90": 19906,
                    "tac_p95": 20138,
                },
                "confidence": {
                    "level": "high",
                    "similarity": 0.82,
                    "sample_size": 12,
                    "cv": 0.31,
                },
                "evidence": [
                    {"wo_id": "190672272", "sim": 0.82},
                    {"wo_id": "190672111", "sim": 0.79},
                ],
                "action": "recommend_inspection_or_part_planning",
            }
        ],
    }


def test_recommendation_status_already_past_p95():
    pma = _pma_with_recommendation(reference_tac=20500)
    text = render_chat_upload(_hand_built_result(pma))
    assert "latest known TAC 20500 → already past p95." in text


def test_recommendation_status_past_p90():
    pma = _pma_with_recommendation(reference_tac=19906)
    text = render_chat_upload(_hand_built_result(pma))
    assert "latest known TAC 19906 → past p90." in text


def test_recommendation_status_before_p50_shows_lead_cycles():
    pma = _pma_with_recommendation(reference_tac=17000)
    text = render_chat_upload(_hand_built_result(pma))
    assert "latest known TAC 17000 → 356 cycles before p50." in text


def test_recommendation_omits_p95_segment_when_null():
    rec = _recommendation()
    rec["predicted_replacement"]["tac_p95"] = None
    pma = _pma_with_recommendation()
    pma["recommendation"] = rec
    text = render_chat_upload(_hand_built_result(pma))
    assert (
        "Replace around TAC 18294 (p50) · 19906 (p90); latest known TAC "
        "18660 → past p50." in text
    )
    assert "(p95)" not in text


def test_recommendation_omits_arrow_clause_when_reference_tac_unknown():
    pma = _pma_with_recommendation(reference_tac=None)
    text = render_chat_upload(_hand_built_result(pma))
    assert "Replace around TAC 18294 (p50) · 19906 (p90) · 20138 (p95)." in text
    assert "latest known TAC" not in text
    assert '"tac_p95": 20138' in text  # JSON block is unaffected.


def test_recommendation_confidence_line_omits_cv_and_similarity_when_null():
    pma = _pma_with_recommendation(
        confidence={"level": "low", "similarity": None, "sample_size": 4, "cv": None}
    )
    text = render_chat_upload(_hand_built_result(pma))
    assert (
        "Confidence: low (n=4) · similar work orders + lead-time history "
        "· heuristic, not a calibrated forecast." in text
    )


@pytest.mark.parametrize(
    ("basis", "expected"),
    [
        ("component_history", "component lead-time history"),
        ("fleet_replacement_interval", "fleet replacement pattern"),
        ("some_future_basis", "some\\_future\\_basis"),
    ],
)
def test_recommendation_basis_text_variants(basis, expected):
    pma = _pma_with_recommendation(basis=basis)
    text = render_chat_upload(_hand_built_result(pma))
    assert f"· {expected} · heuristic, not a calibrated forecast." in text


def test_recommendation_evidence_line_omitted_when_empty():
    pma = _pma_with_recommendation(evidence=[])
    text = render_chat_upload(_hand_built_result(pma))
    assert "Evidence:" not in text
    assert '"evidence": []' in text  # JSON block still carries the empty list.


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("monitor", "Monitor"),
        ("recommend_inspection_or_part_planning", "Plan inspection / part replacement"),
        ("some_future_action", "some\\_future\\_action"),
    ],
)
def test_recommendation_action_text_variants(action, expected):
    pma = _pma_with_recommendation(action=action)
    text = render_chat_upload(_hand_built_result(pma))
    assert f"**Recommendation: {expected} — 473597-5\\|AFT**" in text


def test_no_recommendation_means_output_is_unchanged():
    # Regression check: a pma dict without a "recommendation" key (the
    # Option B projected-window fixture) never gets the condensed block.
    text = render_chat_upload(_hand_built_result(_aft_projected_window_pma()))
    assert "**Recommendation:" not in text
    assert "```json" not in text
    pma_no_rec = _pma_with_recommendation()
    pma_no_rec["recommendation"] = None
    text = render_uploaded_workorder_summary(_hand_built_result(pma_no_rec))
    assert "**Recommendation:" not in text


def test_render_uploaded_workorder_summary_include_recommendation_false_suppresses_block():
    # `compose_evidence_answer` renders the recommendation block once, itself,
    # before section (a); its call into this function must not duplicate it.
    result = _hand_built_result(_pma_with_recommendation())
    text = render_uploaded_workorder_summary(result, include_recommendation=False)
    assert "**Recommendation:" not in text
    assert "```json" not in text
    assert text.startswith("Analysed uploaded XML")
    assert "Projected window for this aircraft" not in text
