from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pm_agent.fast_api_app import app


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
