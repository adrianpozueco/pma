from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from google.genai import types

from pm_agent.fast_api_app import app
from pm_agent.prediction.contracts import (
    IntervalStats,
    PredictionDecision,
    PredictionReason,
    PredictionResult,
)
from pm_agent.workorders.chat import analyze_chat_upload

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workorders"
PARAMS = {
    "mode": "new_work_order",
    "analysis_as_of": "2026-09-01T10:00:00+00:00",
    "current_aircraft_tac": 120,
    "current_tac_source": "operator_feed",
    "current_tac_observed_at": "2026-09-01T09:55:00+00:00",
}


@pytest.mark.parametrize("part_number", ["2085M31G03", "62197-301-001", "8201-11-0000-01"])
def test_raw_xml_upload_runs_analysis_for_each_configured_target(part_number):
    source = (FIXTURES / "open_nozzle.xml").read_bytes().replace(b"2085M31G03", part_number.encode())
    source = source.replace(b"Fuel nozzle leak observed", {
        "2085M31G03": b"Fuel nozzle leak observed",
        "62197-301-001": b"Water boiler trips circuit breaker",
        "8201-11-0000-01": b"Convection oven trips circuit breaker",
    }[part_number])
    response = TestClient(app).post(
        "/workorders/analyze", params=PARAMS, content=source, headers={"content-type": "application/xml"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(item["part_number"] == part_number for item in body["target_parts"])
    assert body["prediction"]["status"] == "model_unavailable"


def test_multipart_upload_is_supported_and_unsafe_xml_is_rejected():
    client = TestClient(app)
    response = client.post(
        "/workorders/analyze", params=PARAMS,
        files={"file": ("workorder.xml", (FIXTURES / "open_nozzle.xml").read_bytes(), "application/xml")},
    )
    assert response.status_code == 200, response.text
    unsafe = client.post(
        "/workorders/analyze", params=PARAMS, content=b"<amosTransportEnvelope><!DOCTYPE x [<!ENTITY e 'x'>]></amosTransportEnvelope>", headers={"content-type": "application/xml"},
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["detail"]["code"] == "unsafe_xml"


def test_multipart_body_without_content_length_is_capped_before_form_parsing():
    boundary = b"workorder-boundary"
    prefix = b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"large.xml\"\r\nContent-Type: application/xml\r\n\r\n"
    suffix = b"\r\n--" + boundary + b"--\r\n"
    chunks = iter((prefix, b"x" * (25 * 1024 * 1024 + 1), suffix))
    response = TestClient(app).post(
        "/workorders/analyze", params=PARAMS, content=chunks,
        headers={"content-type": "multipart/form-data; boundary=workorder-boundary", "transfer-encoding": "chunked"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "oversized_input"


def test_history_configuration_failure_is_a_structured_503(monkeypatch):
    monkeypatch.setattr("pm_agent.fast_api_app.default_history_provider", lambda: (_ for _ in ()).throw(RuntimeError("credentials")))
    response = TestClient(app).post(
        "/workorders/analyze", params=PARAMS, content=(FIXTURES / "open_nozzle.xml").read_bytes(), headers={"content-type": "application/xml"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "history_provider_unavailable"


class _FakePredictor:
    """Deterministic test double: never touches BigQuery.

    Branches purely on ``ata_chapter`` so both the cargo fixture (T07,
    ``26-16``, no legacy target part) and a non-cargo fixture exercise
    distinct ``pma.decision`` values without any real focus-component match.
    """

    def predict(self, prediction_input):
        common = {
            "aircraft": prediction_input.aircraft_reg,
            "workorder_id": prediction_input.wo_id,
            "analysis_as_of": prediction_input.analysis_as_of,
            "mode": prediction_input.mode,
        }
        if prediction_input.ata_chapter == "26-16":
            return PredictionResult(
                decision=PredictionDecision.HISTORICAL_INTERVAL,
                reason=None,
                component_key="26-16|AFT",
                interval=IntervalStats(
                    basis="closing_tac_to_closing_tac_interval",
                    unit="aircraft_flight_cycles",
                    p50=150.0,
                    p90=420.0,
                    min=20,
                    max=600,
                    n=6,
                    aircraft=4,
                ),
                **common,
            )
        return PredictionResult(
            decision=PredictionDecision.OUT_OF_SCOPE,
            reason=PredictionReason.COMPONENT_NOT_IN_FOCUS_SET,
            **common,
        )


class _FakeConstructionFailedPredictor:
    """Mirrors what ``default_predictor()`` returns when repository
    construction fails: it always answers ``data_source_unavailable``, never
    raises (PMA-ONLINE-AGENT-plan.md §9 T13a "construction failure ->
    fallback, never 500/503")."""

    def predict(self, prediction_input):
        return PredictionResult.disabled(
            PredictionReason.DATA_SOURCE_UNAVAILABLE,
            workorder_id=prediction_input.wo_id,
            aircraft=prediction_input.aircraft_reg,
            analysis_as_of=prediction_input.analysis_as_of,
            mode=prediction_input.mode,
        )


def test_pma_decision_for_cargo_fixture_comes_from_the_injected_predictor(monkeypatch):
    monkeypatch.setattr("pm_agent.fast_api_app.default_predictor", lambda: _FakePredictor())
    response = TestClient(app).post(
        "/workorders/analyze", params=PARAMS,
        content=(FIXTURES / "open_cargo_smoke_detector.xml").read_bytes(),
        headers={"content-type": "application/xml"},
    )
    assert response.status_code == 200, response.text
    pma = response.json()["pma"]
    assert pma["decision"] == "historical_interval"
    assert pma["component_key"] == "26-16|AFT"
    assert pma["interval"]["p50"] == 150.0
    assert pma["interval"]["p90"] == 420.0
    assert pma["interval"]["n"] == 6


def test_pma_decision_for_non_cargo_fixture_is_out_of_scope(monkeypatch):
    monkeypatch.setattr("pm_agent.fast_api_app.default_predictor", lambda: _FakePredictor())
    response = TestClient(app).post(
        "/workorders/analyze", params=PARAMS,
        content=(FIXTURES / "open_nozzle.xml").read_bytes(),
        headers={"content-type": "application/xml"},
    )
    assert response.status_code == 200, response.text
    pma = response.json()["pma"]
    assert pma["decision"] == "out_of_scope"
    assert pma["reason"] == "component_not_in_focus_set"


def test_predictor_construction_failure_never_becomes_a_500_or_503(monkeypatch):
    monkeypatch.setattr(
        "pm_agent.fast_api_app.default_predictor",
        lambda: _FakeConstructionFailedPredictor(),
    )
    response = TestClient(app).post(
        "/workorders/analyze", params=PARAMS,
        content=(FIXTURES / "open_nozzle.xml").read_bytes(),
        headers={"content-type": "application/xml"},
    )
    assert response.status_code == 200, response.text
    pma = response.json()["pma"]
    assert pma["decision"] == "no_reliable_prediction"
    assert pma["reason"] == "data_source_unavailable"


class _FakeChatCtx:
    """Minimal ADK-context double satisfying what ``analyze_chat_upload``
    needs for an inline (non-file-reference) XML upload: ``user_content``,
    ``state``, and async ``save_artifact``/``load_artifact``. ``ctx.session``
    is never touched on this path (PMA-ONLINE-AGENT-plan.md T13a note)."""

    def __init__(self, xml_bytes: bytes, filename: str = "cargo.xml", text: str = ""):
        parts = []
        if text:
            parts.append(types.Part.from_text(text=text))
        parts.append(
            types.Part(
                inline_data=types.Blob(
                    data=xml_bytes, mime_type="application/xml", display_name=filename
                )
            )
        )
        self.user_content = types.Content(role="user", parts=parts)
        self.state: dict = {}
        self._artifacts: dict[str, list] = {}

    async def save_artifact(self, *, filename, artifact):
        versions = self._artifacts.setdefault(filename, [])
        versions.append(artifact)
        return len(versions) - 1

    async def load_artifact(self, *, filename, version=None):
        versions = self._artifacts.get(filename, [])
        if version is None:
            version = len(versions) - 1
        return versions[version]


def _strip_volatile(pma: dict) -> dict:
    """Drop fields that may legitimately differ between two independently
    constructed predictor calls (job ids, request id) so the comparison is
    scoped to the deterministic prediction content itself."""
    pma = dict(pma)
    provenance = pma.get("provenance")
    if isinstance(provenance, dict):
        provenance = dict(provenance)
        provenance.pop("bq_job_ids", None)
        pma["provenance"] = provenance
    pma.pop("request_id", None)
    return pma


@pytest.mark.asyncio
async def test_chat_and_http_uploads_produce_identical_pma_for_the_same_xml_and_cutoff(
    monkeypatch,
):
    xml_bytes = (FIXTURES / "open_cargo_smoke_detector.xml").read_bytes()
    analysis_as_of = "2026-09-24T10:00:00+00:00"

    monkeypatch.setattr(
        "pm_agent.fast_api_app.default_predictor", lambda: _FakePredictor()
    )
    http_response = TestClient(app).post(
        "/workorders/analyze",
        params={"mode": "new_work_order", "analysis_as_of": analysis_as_of},
        content=xml_bytes,
        headers={"content-type": "application/xml"},
    )
    assert http_response.status_code == 200, http_response.text
    http_pma = http_response.json()["pma"]

    monkeypatch.setattr(
        "pm_agent.workorders.chat.default_predictor", lambda: _FakePredictor()
    )
    ctx = _FakeChatCtx(
        xml_bytes,
        filename="cargo.xml",
        text=f"analysis_as_of={analysis_as_of}",
    )
    chat_result = await analyze_chat_upload(ctx)
    assert chat_result["status"] == "analyzed"
    chat_pma = chat_result["analysis"]["pma"]

    assert _strip_volatile(chat_pma) == _strip_volatile(http_pma)
