"""Integration tests for the wired parallel-evidence graph (N5):

    prepare_workorder_upload -"workorder_evidence"-> bq_evidence + ipc_evidence
        -> join_evidence -> compose_evidence_answer

Covers concurrency (deterministic, via an asyncio.Event barrier - never
elapsed-time measurement), join-payload keying by predecessor node name, and
that a failed branch is always named as failed (never rendered as "no matches
found"), including when both branches fail. Pure/sync unit coverage of
``_merge_branch``/``_unavailable_result``/``_run_bq_tools`` lives in
``tests/unit/test_evidence_branches.py``.

Reuses the ``runner`` fixture and ``attachment``/``session_for``/``turn``
helpers from ``test_adk_workorder_upload.py`` (same Gemini-forbidden Runner,
same turn/event-collection contract); every test here overrides the
fixture's default NO_MATCH fakes for ``_run_bq_branch``/``_run_ipc_branch``
with its own monkeypatch, per that fixture's documented override contract.
"""

import asyncio

import pytest

from pm_agent.workorders.evidence import SourceResult, SourceStatus
from tests.integration.test_adk_workorder_upload import attachment as attachment
from tests.integration.test_adk_workorder_upload import runner as runner
from tests.integration.test_adk_workorder_upload import session_for as session_for
from tests.integration.test_adk_workorder_upload import turn as turn


@pytest.mark.asyncio
async def test_bq_and_ipc_branches_run_concurrently_not_sequentially(
    runner, monkeypatch
):
    bq_started = asyncio.Event()
    ipc_started = asyncio.Event()

    async def bq_branch(request):
        del request
        bq_started.set()
        # A sequential (non-concurrent) scheduler would run this branch to
        # completion before ever starting the other, so ipc_started would
        # never be set and this wait_for would time out and raise - turning
        # this branch's own result into ERROR. Concurrency is proven by the
        # absence of that outcome, never by measuring elapsed time.
        await asyncio.wait_for(ipc_started.wait(), timeout=5)
        return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)

    async def ipc_branch(request):
        del request
        ipc_started.set()
        await asyncio.wait_for(bq_started.wait(), timeout=5)
        return SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)

    session = await session_for(runner)
    result, text, _ = await turn(runner, session, files=[attachment()])
    assert result["status"] == "analyzed"
    assert bq_started.is_set() and ipc_started.is_set()
    # If either branch had deadlocked/timed out waiting on the other, its
    # result would be ERROR (see module docstring on evidence_branches.py's
    # try/except boundary), never the NO_MATCH each fake actually returns.
    assert "unexpected_bq_evidence_failure" not in text
    assert "unexpected_ipc_evidence_failure" not in text
    assert "BigQuery was queried and found no matching records." in text
    assert "The IPC knowledge base was queried and found no matching pages." in text


@pytest.mark.asyncio
async def test_join_evidence_payload_is_keyed_by_predecessor_node_name(
    runner, monkeypatch
):
    async def bq_branch(request):
        del request
        return SourceResult(
            source="bq_evidence",
            status=SourceStatus.SUCCESS,
            records=({"r": 1}, {"r": 2}, {"r": 3}),
            counts={"tools_executed": 3, "tools_succeeded": 3, "tools_failed": 0},
        )

    async def ipc_branch(request):
        del request
        return SourceResult(
            source="ipc_manual_retrieval",
            status=SourceStatus.SUCCESS,
            citations=({"title": "IPC Page A"}, {"title": "IPC Page B"}),
            counts={"chunks_returned": 5},
        )

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)

    session = await session_for(runner)
    _, text, _ = await turn(runner, session, files=[attachment()])
    # If join_evidence's payload keys were swapped, compose_evidence_answer
    # would read the IPC result where it expects the BQ one (and vice versa):
    # the BQ section would then report 0 records/0 queries (IPC's SourceResult
    # carries no "tools"/records data), not the 3-of-3 the real BQ fake sent.
    assert "3 record(s) across 3 of 3 queries." in text
    assert "2 document(s) matched, 5 excerpt(s) returned." in text


@pytest.mark.asyncio
async def test_one_branch_timeout_one_branch_success_names_failure_and_keeps_partial_answer(
    runner, monkeypatch
):
    async def bq_branch(request):
        del request
        return SourceResult(
            source="bq_evidence",
            status=SourceStatus.TIMEOUT,
            error_detail="timeout:DeadlineExceeded",
        )

    async def ipc_branch(request):
        del request
        return SourceResult(
            source="ipc_manual_retrieval",
            status=SourceStatus.SUCCESS,
            citations=({"title": "IPC Page A"},),
            counts={"chunks_returned": 1},
        )

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)

    session = await session_for(runner)
    _, text, _ = await turn(runner, session, files=[attachment()])
    # The failed branch must be named as failed, never rendered as an
    # innocuous "no matches found".
    assert "BigQuery evidence TIMED OUT" in text
    assert "(timeout:DeadlineExceeded)" in text
    assert "found no matching records" not in text
    assert "This is not the same as 'no matches found'" in text
    # The successful branch's evidence is still present in the same answer.
    assert "1 document(s) matched, 1 excerpt(s) returned." in text
    assert "BigQuery evidence is incomplete: branch TIMED OUT." in text


@pytest.mark.asyncio
async def test_both_branches_fail_states_both_failures_not_fabricated_no_match(
    runner, monkeypatch
):
    async def bq_branch(request):
        del request
        return SourceResult(
            source="bq_evidence", status=SourceStatus.ERROR, error_detail="error:Boom"
        )

    async def ipc_branch(request):
        del request
        return SourceResult(
            source="ipc_manual_retrieval",
            status=SourceStatus.UNAVAILABLE,
            error_detail="unavailable:reference_corpus_unconfigured",
        )

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)

    session = await session_for(runner)
    _, text, _ = await turn(runner, session, files=[attachment()])
    assert "BigQuery evidence FAILED with an ERROR" in text
    assert "IPC knowledge-base evidence is UNAVAILABLE (not deployed/loaded)" in text
    # Neither failure is ever mistaken for a clean empty result.
    assert "found no matching records" not in text
    assert "found no matching pages" not in text
    assert "BigQuery evidence is incomplete: branch FAILED with an ERROR." in text
    assert (
        "IPC manual evidence is incomplete: branch is UNAVAILABLE (not deployed/loaded)."
        in text
    )
    # A useful partial answer is still produced: part (a), the uploaded
    # work order's own facts, is unaffected by either branch failing.
    assert "**(a) What the uploaded work order records**" in text
    assert "DEMO-XML-ONLY-2085" in text


@pytest.mark.asyncio
async def test_no_match_and_unavailable_render_distinctly(runner, monkeypatch):
    async def bq_branch(request):
        del request
        return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)

    async def ipc_branch(request):
        del request
        return SourceResult(
            source="ipc_manual_retrieval",
            status=SourceStatus.UNAVAILABLE,
            error_detail="unavailable:reference_corpus_unconfigured",
        )

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)

    session = await session_for(runner)
    _, text, _ = await turn(runner, session, files=[attachment()])
    # A genuine empty result ...
    assert "BigQuery was queried and found no matching records." in text
    # ... reads nothing like an unconfigured/undeployed source.
    assert "IPC knowledge-base evidence is UNAVAILABLE (not deployed/loaded)" in text
    assert "(unavailable:reference_corpus_unconfigured)" in text
    assert "This is not the same as 'no matches found'" in text
