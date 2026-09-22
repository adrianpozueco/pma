from __future__ import annotations

import pytest

from amos_data import ParseError, parse_workorders


def _xml(*workorders: str, namespace: str = "") -> bytes:
    ns = f' xmlns="{namespace}"' if namespace else ""
    body = "".join(workorders)
    return f'<amosTransportEnvelope{ns} version="1.3"><header><date timezone="UTC">2026-01-02T03:04:05.123456Z</date></header><payload type="transferWorkorder" version="3.1"><transferWorkorder>{body}</transferWorkorder></payload></amosTransportEnvelope>'.encode()


def _wo(uuid: str, number: str, description: str = "SYMPTOM") -> str:
    return f'''<workorder uuid="{uuid}"><workorderNumber>{number}</workorderNumber><workorderHeader><workorderType>M</workorderType><workorderState>O</workorderState><issueData><issueDate>2024-02-29Z</issueDate><issueDateTime>2024-02-29T12:34:56.123456789Z</issueDateTime><issueTac>10</issueTac></issueData></workorderHeader><workSteps><workStep uuid="step-{uuid}"><workStepDateTime>2024-02-29T12:34:56.123456789Z</workStepDateTime><description>{description}</description><actions><action uuid="action-{uuid}"><actionType>Action</actionType><actionText>CHECK</actionText></action></actions></workStep></workSteps></workorder>'''


def test_parses_all_workorders_and_preserves_precision_and_identity():
    result = parse_workorders(_xml(_wo("wo-1", "1"), _wo("wo-2", "2")), source_name="batch.xml", artifact_version="v1")
    assert [row["workorder_number"] for row in result.workorders] == ["1", "2"]
    assert result.workorders[0]["work_steps"][0]["ts"] == "2024-02-29T12:34:56.123456789+00:00"
    assert result.workorders[0]["provenance"]["source_name"] == "batch.xml"
    assert result.workorders[0]["provenance"]["raw_timestamps"]


def test_namespaces_and_no_swaps_do_not_change_semantics():
    result = parse_workorders(_xml(_wo("wo-1", "1", "NO PART CHANGE"), namespace="urn:amos:test"))
    assert len(result.workorders) == 1
    assert result.workorders[0]["part_swap_count"] == 0
    assert result.workorders[0]["work_steps"][0]["description"] == "NO PART CHANGE"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"<amosTransportEnvelope><payload type='other'/></amosTransportEnvelope>", "unsupported_payload"),
        (b"<amosTransportEnvelope><!DOCTYPE x [<!ENTITY e 'x'>]></amosTransportEnvelope>", "unsafe_xml"),
        (b"<amosTransportEnvelope><payload>&unknown;</payload></amosTransportEnvelope>", "unsafe_xml"),
        (b"<amosTransportEnvelope>", "malformed_xml"),
    ],
)
def test_rejects_unsafe_or_invalid_input(payload: bytes, code: str):
    with pytest.raises(ParseError) as error:
        parse_workorders(payload)
    assert error.value.code == code


def test_duplicate_identity_is_diagnostic_and_rows_are_retained():
    xml = _xml(_wo("same", "1"), _wo("same", "2"))
    result = parse_workorders(xml)
    assert len(result.workorders) == 2
    assert any(item["code"] == "conflicting_identity" for item in result.diagnostics)


def test_invalid_optional_date_is_diagnostic_but_retrieval_survives():
    result = parse_workorders(_xml(_wo("wo-1", "1")).replace(b"2024-02-29Z", b"2024-02-30Z"))
    assert len(result.workorders) == 1
    assert result.workorders[0]["issue"]["date"] is None
    assert any(item["code"] == "invalid_date" for item in result.diagnostics)


def test_dtd_is_rejected_when_utf16_has_no_bom():
    unsafe = '<amosTransportEnvelope><!DOCTYPE x [<!ENTITY e "x">]></amosTransportEnvelope>'.encode("utf-16le")
    with pytest.raises(ParseError) as error:
        parse_workorders(unsafe)
    assert error.value.code == "unsafe_xml"


def test_deep_nesting_is_limited_without_recursion_error():
    payload = "<amosTransportEnvelope>" + ("<x>" * 110) + ("</x>" * 110) + "</amosTransportEnvelope>"
    with pytest.raises(ParseError) as error:
        parse_workorders(payload.encode())
    assert error.value.code == "oversized_input"
