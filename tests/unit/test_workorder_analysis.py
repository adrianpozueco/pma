from pathlib import Path

import pytest

from pm_agent.workorders import AnalysisInput, WorkOrderAnalysisError, WorkOrderAnalysisService
from pm_agent.workorders.artifacts import analyze_current_session_artifact, validate_artifact_filename
from pm_agent.workorders.service import _text_hash
from amos_data.retrieval import LocalHistoryProvider


FIXTURES = Path(__file__).parents[1] / "fixtures" / "workorders"


def _request(**overrides):
    values = {"mode": "new_work_order", "analysis_as_of": "2026-09-01T10:00:00+00:00", "current_aircraft_tac": 120, "current_tac_source": "operator_feed", "current_tac_observed_at": "2026-09-01T09:55:00+00:00"}
    values.update(overrides)
    return AnalysisInput(**values)


def test_analysis_preserves_roles_counters_and_never_uses_action_text():
    result = WorkOrderAnalysisService().analyze_xml((FIXTURES / "open_nozzle.xml").read_bytes(), _request())
    target = result["target_parts"][0]
    assert target["part_number"] == "2085M31G03"
    assert {"component", "requested"} <= set(target["roles"])
    assert result["parsed_context"]["issue"]["tac"]["value"] == 100
    assert result["parsed_context"]["current_aircraft_tac"]["value"] == 120
    assert result["parsed_context"]["closing"]["tac"]["value"] is None
    assert "REPLACED" not in "\n".join(str(x) for x in result["parsed_context"]["symptoms"])
    assert result["prediction"]["value"] is None
    assert result["replacement_recommendation"]["value"] is None


def test_new_workorder_without_current_tac_does_not_promote_issue_counter():
    result = WorkOrderAnalysisService().analyze_xml((FIXTURES / "open_nozzle.xml").read_bytes(), AnalysisInput(mode="new_work_order", analysis_as_of="2026-09-01T10:00:00+00:00"))
    assert result["parsed_context"]["current_aircraft_tac"]["status"] == "not_supplied"
    assert result["parsed_context"]["issue"]["tac"]["value"] == 100


def test_multi_workorder_requires_explicit_selection():
    with pytest.raises(WorkOrderAnalysisError) as error:
        WorkOrderAnalysisService().analyze_xml((FIXTURES / "two_workorders.xml").read_bytes(), _request())
    assert error.value.code == "workorder_selection_required"


def test_closed_export_requires_replay_and_replay_excludes_actions():
    closed = (FIXTURES / "closed_boiler.xml").read_bytes()
    with pytest.raises(WorkOrderAnalysisError, match="historical_replay"):
        WorkOrderAnalysisService().analyze_xml(closed, _request())
    result = WorkOrderAnalysisService().analyze_xml(closed, AnalysisInput(mode="historical_replay", analysis_as_of="2026-02-02T11:00:00+00:00"))
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
        WorkOrderAnalysisService().analyze_xml((FIXTURES / "open_nozzle.xml").read_bytes(), _request(current_tac_observed_at="2026-09-01T11:00:00+00:00"))
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
            return types.Part(inline_data=types.Blob(data=(FIXTURES / "open_nozzle.xml").read_bytes(), mime_type="application/xml"))

    result = await analyze_current_session_artifact(Context(), "workorder.xml", _request(), WorkOrderAnalysisService(), version=0)
    assert result["wo_id"] == "wo-open"


@pytest.mark.asyncio
async def test_artifact_adapter_rejects_non_xml_mime_before_parsing():
    from google.genai import types

    class Context:
        async def load_artifact(self, *, filename, version=None):
            return types.Part(inline_data=types.Blob(data=b"not xml", mime_type="text/plain"))

    with pytest.raises(WorkOrderAnalysisError) as error:
        await analyze_current_session_artifact(Context(), "workorder.xml", _request(), WorkOrderAnalysisService(), version=0)
    assert error.value.code == "invalid_artifact_mime"


def test_alias_target_is_candidate_and_future_action_change_is_not_used_in_new_mode():
    source = (FIXTURES / "open_nozzle.xml").read_text().replace("2085M31G03", "OTHER")
    source = source.replace("Fuel nozzle leak observed", "water boiler trips breaker")
    source = source.replace("<actionText>REPLACED NOZZLE AFTER INSPECTION</actionText>", "<performedData><performedDateTime>2026-10-01T00:00:00Z</performedDateTime></performedData><componentChanges><componentChange><partOn><partNumber>8201-11-0000-01</partNumber></partOn></componentChange></componentChanges>")
    result = WorkOrderAnalysisService().analyze_xml(source.encode(), _request())
    assert result["target_parts"] == [{"part_number": "62197-301-001", "part_key": "62197301001", "description": "Water boiler", "resolution_status": "candidate_alias", "roles": ["alias_candidate"], "supporting_input": ["alias_candidate"]}]


def test_replay_before_export_masks_component_and_future_action_target_evidence():
    source = (FIXTURES / "closed_boiler.xml").read_text()
    source = source.replace("<amosTransportEnvelope>", "<amosTransportEnvelope><header><date>2026-03-01T00:00:00Z</date></header>")
    source = source.replace("<workorderState>C</workorderState>", "<workorderState>C</workorderState><component><partNumber>2085M31G03</partNumber></component>")
    source = source.replace("<actionText>Completed replacement</actionText>", "<performedData><performedDateTime>2026-03-02T00:00:00Z</performedDateTime></performedData><componentChanges><componentChange><partOn><partNumber>8201-11-0000-01</partNumber></partOn></componentChange></componentChanges>")
    result = WorkOrderAnalysisService().analyze_xml(source.encode(), AnalysisInput(mode="historical_replay", analysis_as_of="2026-02-02T11:00:00+00:00"))
    assert result["target_parts"] == []
    assert result["parsed_context"]["symptoms"] == []
    assert result["prediction"]["status"] == "out_of_scope"


@pytest.mark.parametrize("value", [True, 1.5])
def test_current_tac_rejects_non_integer_values(value):
    with pytest.raises(WorkOrderAnalysisError, match="integer"):
        _request(current_aircraft_tac=value)


def test_injected_history_provider_receives_only_as_of_symptoms_and_excludes_upload():
    provider = LocalHistoryProvider([
        {"document_id": "old", "record_id": "old", "workorder_id": "old", "source_namespace": "amos", "text_role": "symptom", "raw_text": "fuel nozzle leak", "normalized_text": "fuel nozzle leak", "part_keys": ["2085M31G03"], "available_at": "2026-08-01T00:00:00+00:00", "availability_status": "available", "split": "train", "reference_corpus_version": None},
        {"document_id": "self", "record_id": "self", "workorder_id": "wo-open", "source_namespace": "amos", "text_role": "symptom", "raw_text": "fuel nozzle leak", "normalized_text": "fuel nozzle leak", "part_keys": ["2085M31G03"], "available_at": "2026-08-01T00:00:00+00:00", "availability_status": "available", "split": "train", "reference_corpus_version": None},
        {"document_id": "future", "record_id": "future", "workorder_id": "future", "source_namespace": "amos", "text_role": "symptom", "raw_text": "fuel nozzle leak", "normalized_text": "fuel nozzle leak", "part_keys": ["2085M31G03"], "available_at": "2026-10-01T00:00:00+00:00", "availability_status": "available", "split": "train", "reference_corpus_version": None},
    ])
    result = WorkOrderAnalysisService(provider).analyze_xml((FIXTURES / "open_nozzle.xml").read_bytes(), _request())
    assert [case["case_id"] for case in result["historical_cases"]] == ["amos:old"]
    assert result["historical_cases"][0]["score_meaning"].endswith("not a probability or outcome link")


def test_history_excludes_same_family_self_by_uuid_or_number_and_duplicate_text():
    def document(document_id, **values):
        return {
            "document_id": document_id, "record_id": document_id, "workorder_id": document_id,
            "source_namespace": "amos", "text_role": "symptom", "raw_text": "Fuel nozzle leak observed during inspection",
            "normalized_text": "Fuel nozzle leak observed during inspection", "part_keys": ["2085M31G03"],
            "available_at": "2026-08-01T00:00:00+00:00", "availability_status": "available", "split": "train",
            "reference_corpus_version": None, "aircraft_family": "737-8200", "text_hash": _text_hash("Fuel nozzle leak observed during inspection"),
            **values,
        }
    provider = LocalHistoryProvider([
        document("allowed"),
        document("uuid-self", workorder_id="wo-open"),
        document("number-self", workorder_id="OPEN-1"),
        document("duplicate", raw_text="Fuel nozzle leak observed", normalized_text="Fuel nozzle leak observed", text_hash=_text_hash("Fuel nozzle leak observed")),
        document("wrong-family", aircraft_family="A320"),
    ])
    result = WorkOrderAnalysisService(provider).analyze_xml((FIXTURES / "open_nozzle.xml").read_bytes(), _request())
    assert [case["case_id"] for case in result["historical_cases"]] == ["amos:allowed"]
