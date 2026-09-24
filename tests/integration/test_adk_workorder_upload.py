"""Actual ADK graph/session/artifact contracts; no LLM or cloud query needed."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.artifacts import InMemoryArtifactService
from google.adk.models import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from pm_agent.agent import app
from pm_agent.nodes.evidence_branches import EVIDENCE_CONTEXT_STATE_KEY
from pm_agent.workorders.chat import STATE_KEY, analyze_chat_upload
from pm_agent.workorders.evidence import SourceResult, SourceStatus

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workorders"


async def _fake_no_match_bq_branch(request):
    del request
    return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)


async def _fake_no_match_ipc_branch(request):
    del request
    return SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)


@pytest.fixture
def runner(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail(
            "An XML upload must reach the parser without a model/database lookup"
        )
        yield  # Keep the model's async-generator interface.

    monkeypatch.setattr(Gemini, "generate_content_async", forbidden)
    # An "analyzed" upload now fans out to real BigQuery/IPC evidence branches
    # (pm_agent/nodes/evidence_branches.py). Every pre-existing test in this
    # file predates that fan-out and asserts nothing about its content, so by
    # default both production async seams are faked here to a deterministic
    # NO_MATCH - keeping every unrelated assertion in this file exercising
    # only upload parsing/provenance/selection/replay behavior, with no real
    # BigQuery or Vertex AI call ever attempted. Tests that specifically
    # exercise evidence-branch behavior override these two names directly via
    # their own monkeypatch.
    monkeypatch.setattr(
        "pm_agent.nodes.evidence_branches._run_bq_branch", _fake_no_match_bq_branch
    )
    monkeypatch.setattr(
        "pm_agent.nodes.evidence_branches._run_ipc_branch", _fake_no_match_ipc_branch
    )
    return Runner(
        app=app,
        session_service=InMemorySessionService(),
        artifact_service=InMemoryArtifactService(),
    )


def attachment(data=None, filename="demo.xml", mime="application/xml"):
    if data is None:
        data = (FIXTURES / "demo_nozzle_upload.xml").read_bytes()
    # This is the exact camelCase payload produced by ADK's file input.
    import base64

    return types.Part.model_validate(
        {
            "inlineData": {
                "displayName": filename,
                "mimeType": mime,
                "data": base64.b64encode(data).decode(),
            }
        }
    )


async def turn(runner, session, text="", files=()):
    parts = ([types.Part.from_text(text=text)] if text else []) + list(files)
    events = [
        event
        async for event in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=parts),
        )
    ]
    # Function nodes inherit the Workflow author; node_info identifies the node.
    results = [
        event.output
        for event in events
        if event.node_info
        and "/prepare_workorder_upload@" in event.node_info.path
        and event.output
    ]
    content = "\n".join(
        part.text
        for event in events
        if event.content
        for part in event.content.parts or []
        if part.text
    )
    assert results, [event.model_dump(exclude_none=True) for event in events]
    # A prompt/selection/replay/error turn renders via display_workorder_upload
    # (unchanged short-circuit); a fully "analyzed" turn instead fans out to
    # the evidence branches and renders via compose_evidence_answer. Either is
    # a valid visible-content node for this helper.
    assert any(
        event.node_info
        and (
            "/display_workorder_upload@" in event.node_info.path
            or "/compose_evidence_answer@" in event.node_info.path
        )
        and event.content
        and not event.output
        for event in events
    )
    return results[-1], content, events


async def session_for(runner, user="upload-user"):
    return await runner.session_service.create_session(app_name=app.name, user_id=user)


@pytest.mark.asyncio
async def test_ui_format_attachment_reaches_shared_parser_and_emits_visible_result(
    runner,
):
    session = await session_for(runner)
    result, text, _ = await turn(
        runner, session, "Analyse the attached work order.", [attachment()]
    )
    assert result["status"] == "analyzed"
    assert result["uploaded_workorder"]["workorder_number"] == "DEMO-XML-ONLY-2085"
    assert result["source"]["version"] == 0
    assert (
        result["source"]["upload_hash"]
        == hashlib.sha256(
            (FIXTURES / "demo_nozzle_upload.xml").read_bytes()
        ).hexdigest()
    )
    assert result["analysis"]["input_mode"] == "historical_replay"
    assert result["analysis"]["prediction"]["probability"] is None
    assert result["analysis"]["parsed_context"]["current_aircraft_tac"]["value"] is None
    assert "COPPER-FINCH-41" in text and "DEMO-OFF-41" in text and "DEMO-ON-42" in text
    saved = await runner.artifact_service.load_artifact(
        app_name=app.name,
        user_id=session.user_id,
        session_id=session.id,
        filename="demo.xml",
        version=0,
    )
    assert saved.inline_data.data == (FIXTURES / "demo_nozzle_upload.xml").read_bytes()
    persisted = await runner.session_service.get_session(
        app_name=app.name, user_id=session.user_id, session_id=session.id
    )
    assert persisted.state[STATE_KEY]["references"][0]["version"] == 0


@pytest.mark.asyncio
async def test_multiple_workorders_are_selected_in_next_turn_without_reupload(runner):
    session = await session_for(runner)
    result, _, _ = await turn(
        runner,
        session,
        files=[attachment((FIXTURES / "two_workorders.xml").read_bytes())],
    )
    assert result["status"] == "selection_required" and result["choices"] == [
        "ONE",
        "TWO",
    ]
    result, _, _ = await turn(runner, session, "selected_wo_id=TWO")
    assert result["analysis"]["wo_id"] == "wo-two"
    assert result["source"]["version"] == 0


@pytest.mark.asyncio
async def test_replay_followup_masks_later_snapshot_and_reuses_pinned_artifact(runner):
    session = await session_for(runner)
    await turn(runner, session, files=[attachment()])
    # Overwrite the same filename externally: follow-up must still use version 0.
    await runner.artifact_service.save_artifact(
        app_name=app.name,
        user_id=session.user_id,
        session_id=session.id,
        filename="demo.xml",
        artifact=attachment(b"<unrelated/>"),
    )
    result, text, _ = await turn(runner, session, "analysis_as_of=2026-09-01T09:15:00Z")
    assert result["source"]["version"] == 0
    assert result["analysis"]["parsed_context"]["symptoms"] == []
    assert result["uploaded_workorder"]["recorded_actions"] == []
    assert "DEMO-ON-42" not in text and "COPPER-FINCH-41" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,filename,mime,code",
    [
        (
            b"<!DOCTYPE x [<!ENTITY e 'xx'>]><x/>",
            "unsafe.xml",
            "application/xml",
            "unsafe_xml",
        ),
        (b"<bad", "bad.xml", "application/xml", "malformed_xml"),
        (b"<x/>", "../other.xml", "application/xml", "invalid_artifact_reference"),
        (b"<x/>", "wrong.xml", "application/pdf", "invalid_artifact_mime"),
    ],
)
async def test_invalid_attachments_do_not_fall_through_to_chat(
    runner, data, filename, mime, code
):
    session = await session_for(runner)
    result, _, _ = await turn(runner, session, files=[attachment(data, filename, mime)])
    assert result["status"] == "error" and result["code"] == code


@pytest.mark.asyncio
async def test_artifact_reference_cannot_cross_sessions(runner):
    session = await session_for(runner)
    await turn(runner, session, files=[attachment()])
    other = await session_for(runner)
    ref = types.Part(
        file_data=types.FileData(
            mime_type="application/xml",
            display_name="demo.xml",
            file_uri=f"artifact://apps/{app.name}/users/{session.user_id}/sessions/{session.id}/artifacts/demo.xml/versions/0",
        )
    )
    result, _, _ = await turn(runner, other, files=[ref])
    assert result["code"] == "invalid_artifact_reference"


def artifact_reference(session, filename="demo.xml", version=0):
    return types.Part(
        file_data=types.FileData(
            mime_type="application/xml",
            display_name=filename,
            file_uri=f"artifact://apps/{app.name}/users/{session.user_id}/sessions/{session.id}/artifacts/{filename}/versions/{version}",
        )
    )


@pytest.mark.asyncio
async def test_current_session_artifact_reference_version_zero(runner):
    session = await session_for(runner)
    await runner.artifact_service.save_artifact(
        app_name=app.name,
        user_id=session.user_id,
        session_id=session.id,
        filename="demo.xml",
        artifact=attachment(),
    )
    result, _, _ = await turn(runner, session, files=[artifact_reference(session)])
    assert result["status"] == "analyzed" and result["source"]["version"] == 0


@pytest.mark.asyncio
async def test_file_selection_keeps_cutoff_and_allows_switching_workorders(runner):
    session = await session_for(runner)
    result, _, _ = await turn(
        runner,
        session,
        "analysis_as_of=2026-09-22T12:00:00Z",
        [
            attachment((FIXTURES / "two_workorders.xml").read_bytes(), "multi.xml"),
            attachment(filename="demo.xml"),
        ],
    )
    assert result["choices"] == ["multi.xml", "demo.xml"]
    result, _, _ = await turn(runner, session, 'filename="multi.xml"')
    assert result["choices"] == ["ONE", "TWO"]
    result, _, _ = await turn(runner, session, "TWO")
    assert result["analysis"]["wo_id"] == "wo-two"
    assert result["analysis"]["analysis_as_of"] == "2026-09-22T12:00:00Z"
    result, _, _ = await turn(runner, session, 'filename="demo.xml"')
    assert result["status"] == "analyzed"
    assert result["analysis"]["input_mode"] == "historical_replay"
    assert result["uploaded_workorder"]["workorder_number"] == "DEMO-XML-ONLY-2085"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option",
    ["selected_wo_id=UNKNOWN", "mode=new_work_order", "analysis_as_of=garbage"],
)
async def test_invalid_followup_does_not_poison_saved_analysis(runner, option):
    session = await session_for(runner)
    await turn(runner, session, files=[attachment()])
    result, _, _ = await turn(runner, session, option)
    assert result["status"] == "error"
    result, _, _ = await turn(runner, session, "Analyse this file")
    assert result["status"] == "analyzed"
    assert result["uploaded_workorder"]["workorder_number"] == "DEMO-XML-ONLY-2085"


@pytest.mark.asyncio
async def test_failed_new_upload_clears_old_context_and_ordinary_chat_passes_through(
    runner,
):
    session = await session_for(runner)
    await turn(runner, session, files=[attachment()])
    saved = await runner.session_service.get_session(
        app_name=app.name, user_id=session.user_id, session_id=session.id
    )
    ordinary = types.Content(
        role="user",
        parts=[types.Part.from_text(text="How many work orders are in BigQuery?")],
    )
    assert (
        await analyze_chat_upload(
            SimpleNamespace(user_content=ordinary, state=saved.state)
        )
        is None
    )
    await turn(runner, session, files=[attachment(b"<bad")])
    saved = await runner.session_service.get_session(
        app_name=app.name, user_id=session.user_id, session_id=session.id
    )
    assert not saved.state[STATE_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [False, True])
async def test_combined_inline_and_artifact_size_is_bounded(
    runner, monkeypatch, reference
):
    from pm_agent.workorders import chat

    payload = (FIXTURES / "demo_nozzle_upload.xml").read_bytes()
    monkeypatch.setattr(chat, "MAX_XML_BYTES", len(payload) + 1)
    session = await session_for(runner)
    parts = [attachment(filename="one.xml"), attachment(filename="two.xml")]
    if reference:
        for filename in ("one.xml", "two.xml"):
            await runner.artifact_service.save_artifact(
                app_name=app.name,
                user_id=session.user_id,
                session_id=session.id,
                filename=filename,
                artifact=attachment(),
            )
        parts = [
            artifact_reference(session, filename) for filename in ("one.xml", "two.xml")
        ]
    result, _, _ = await turn(runner, session, files=parts)
    assert result["code"] == "oversized_input"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "part_number,description",
    [
        ("2085M31G03", "fuel nozzle"),
        ("62197301001", "water boiler"),
        ("820111000001", "convection oven"),
    ],
)
async def test_all_configured_parts_are_resolved_from_upload(
    runner, part_number, description
):
    payload = (
        (FIXTURES / "demo_nozzle_upload.xml")
        .read_bytes()
        .replace(b"2085M31G03", part_number.encode())
        .replace(b"fuel nozzle", description.encode())
    )
    session = await session_for(runner)
    result, _, _ = await turn(runner, session, files=[attachment(payload)])
    assert [target["part_key"] for target in result["analysis"]["target_parts"]] == [
        part_number
    ]


@pytest.mark.asyncio
async def test_ordinary_chat_still_reaches_existing_specialist(runner, monkeypatch):
    responses = iter(['{"route":"bq"}', "Stub specialist response"])
    requests = []

    async def stub_model(self, llm_request, stream=False):
        requests.append(llm_request)
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=next(responses))]
            )
        )

    monkeypatch.setattr(Gemini, "generate_content_async", stub_model)
    session = await session_for(runner)
    question = "How many work orders are in BigQuery?"
    events = [
        event
        async for event in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text=question)]
            ),
        )
    ]
    assert len(requests) == 2
    assert any(
        event.node_info and "/bq_analytics@" in event.node_info.path for event in events
    )
    assert any(
        part.text == question
        for content in requests[-1].contents
        for part in content.parts or []
    )
    assert not any(
        event.node_info and "/display_workorder_upload@" in event.node_info.path
        for event in events
    )


@pytest.mark.asyncio
async def test_ambiguous_replay_date_does_not_silently_reuse_previous_cutoff(runner):
    session = await session_for(runner)
    await turn(runner, session, files=[attachment()])
    result, _, _ = await turn(runner, session, "Analyse this file as of September 1")
    assert result["status"] == "missing_input"


@pytest.mark.asyncio
async def test_evidence_request_aircraft_position_is_populated_from_parsed_context(
    runner,
):
    """T13b: ``AircraftContext.position`` used to be hardcoded ``None``; it
    must now come from the resolved position (``pma.position`` when the PMA
    core resolved one, else ``parsed_context.position`` - here the PMA core
    is disabled by default, so it falls back to ``parsed_context.position``,
    which this fixture's component-change carries as "DEMO-POSITION")."""
    session = await session_for(runner)
    result, _, _ = await turn(runner, session, files=[attachment()])
    assert result["status"] == "analyzed"
    position = result["analysis"]["parsed_context"]["position"]
    assert position  # Not None: the fixture's componentChange carries one.
    assert result["analysis"]["pma"]["reason"] == "prediction_disabled"
    persisted = await runner.session_service.get_session(
        app_name=app.name, user_id=session.user_id, session_id=session.id
    )
    evidence_request = persisted.state[EVIDENCE_CONTEXT_STATE_KEY]["evidence_request"]
    assert evidence_request["aircraft"]["position"] == position


@pytest.mark.asyncio
async def test_missing_cutoff_can_be_supplied_without_reupload(runner):
    session = await session_for(runner)
    result, _, _ = await turn(
        runner, session, "Analyse this file as of September 1", [attachment()]
    )
    assert result["status"] == "missing_input"
    result, _, _ = await turn(runner, session, "analysis_as_of=2026-09-01T09:15:00Z")
    assert result["status"] == "analyzed"
    assert result["uploaded_workorder"]["recorded_actions"] == []
