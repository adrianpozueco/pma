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
from datetime import date

import pytest

from pm_agent.agent import app
from pm_agent.nodes.evidence_branches import EVIDENCE_CONTEXT_STATE_KEY
from pm_agent.prediction.contracts import (
    ComponentMatch,
    IntervalStats,
    PredictionDecision,
    PredictionReason,
    PredictionResult,
    ProjectedWindow,
)
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


# --- T13b: section (d)'s PMA rendering, wired end-to-end through the graph ---
# (a fake predictor is injected through the ``default_predictor`` seam that
# ``pm_agent/workorders/chat.py`` calls, T13a.)


class _FakeHistoricalIntervalPredictor:
    def predict(self, prediction_input):
        return PredictionResult(
            decision=PredictionDecision.HISTORICAL_INTERVAL,
            reason=None,
            aircraft=prediction_input.aircraft_reg,
            workorder_id=prediction_input.wo_id,
            analysis_as_of=prediction_input.analysis_as_of,
            mode=prediction_input.mode,
            component_key="2085M31G03|DEMO-POSITION",
            part_number="2085M31G03",
            position="DEMO-POSITION",
            match=ComponentMatch(gate_basis="exact_pn_position"),
            interval=IntervalStats(
                basis="closing_tac",
                unit="aircraft_flight_cycles",
                p50=1200,
                p90=2400,
                min=100,
                max=3000,
                n=8,
                aircraft=5,
            ),
        )


@pytest.mark.asyncio
async def test_pma_disabled_by_default_renders_no_reliable_prediction_without_interval_wording(
    runner, monkeypatch
):
    async def bq_branch(request):
        del request
        return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)

    async def ipc_branch(request):
        del request
        return SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)

    session = await session_for(runner)
    _, text, _ = await turn(runner, session, files=[attachment()])
    assert "No reliable prediction: PMA prediction is disabled." in text
    # Interval wording only belongs to a historical_interval decision.
    assert "not a forecast" not in text
    assert "Matched component:" not in text


@pytest.mark.asyncio
async def test_pma_historical_interval_decision_renders_interval_wording_and_match(
    runner, monkeypatch
):
    async def bq_branch(request):
        del request
        return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)

    async def ipc_branch(request):
        del request
        return SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)
    monkeypatch.setattr(
        "pm_agent.workorders.chat.default_predictor",
        _FakeHistoricalIntervalPredictor,
    )

    session = await session_for(runner)
    result, text, _ = await turn(runner, session, files=[attachment()])
    assert result["analysis"]["pma"]["decision"] == "historical_interval"
    assert (
        "Matched component: 2085M31G03\\|DEMO-POSITION (exact\\_pn\\_position)." in text
    )
    assert "Observed historical interval (not a forecast)" in text
    assert (
        "Between closing TACs: p50 1200 cycles, p90 2400 cycles (n=8, 5 aircraft)."
        in text
    )
    assert "No reliable prediction" not in text
    assert "Out of scope for PMA prediction" not in text

    persisted = await runner.session_service.get_session(
        app_name=app.name, user_id=session.user_id, session_id=session.id
    )
    evidence_request = persisted.state[EVIDENCE_CONTEXT_STATE_KEY]["evidence_request"]
    assert evidence_request["aircraft"]["position"] == "DEMO-POSITION"
    # 2085M31G03 is also a configured target, so the PMA role is merged into
    # that one candidate instead of a duplicate that would re-run per-part
    # queries and double their records.
    same_part = [
        c for c in evidence_request["part_candidates"] if c["part_key"] == "2085M31G03"
    ]
    assert len(same_part) == 1
    assert same_part[0]["part_number"] == "2085M31G03"
    assert same_part[0]["resolution_status"] == "resolved"
    assert "pma_component" in same_part[0]["roles"]


class _FakeProjectedWindowPredictor:
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
            match=ComponentMatch(gate_basis="exact_pn_position"),
            supporting_interval=IntervalStats(
                basis="consecutive_replacements_closing_tac",
                unit="aircraft_flight_cycles",
                p50=380.5,
                p90=1890.8,
                min=4,
                max=3158,
                n=35,
                aircraft=28,
            ),
            projected_window=ProjectedWindow(
                last_replacement_tac=17938,
                last_replacement_date=date(2026, 4, 25),
                last_replacement_wo_id="190672272",
                tac_p50=18319,
                tac_p90=19829,
                cycles_since_last_replacement=722,
                position="between_p50_p90",
            ),
            limitations=("projected_window_not_a_forecast",),
        )


@pytest.mark.asyncio
async def test_pma_projected_window_section_d_does_not_contradict_the_window(
    runner, monkeypatch
):
    async def bq_branch(request):
        del request
        return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)

    async def ipc_branch(request):
        del request
        return SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)
    monkeypatch.setattr(
        "pm_agent.workorders.chat.default_predictor",
        _FakeProjectedWindowPredictor,
    )

    session = await session_for(runner)
    _, text, _ = await turn(runner, session, files=[attachment()])
    assert (
        "Fleet pattern (not a forecast): this component was replaced again after "
        "p50 381 / p90 1891 cycles (n=35, 28 aircraft)." in text
    )
    assert "next replacement around TAC 18319\u201319829." in text
    assert (
        "the projected window above is a fleet pattern, not a forecast or a deadline"
        in text
    )
    # The generic "no replacement policy is configured" gap would contradict
    # the window rendered just above it.
    assert "replacement deadline are unavailable" not in text


# --- Condensed recommendation block (approved 2026-09-24), wired end-to-end
# through the graph. `PredictionResult` (contracts.py) has no `recommendation`
# field yet - a teammate is building `Recommendation`/`PredictedReplacement`/
# `RecommendationConfidence` in parallel - so `_RecommendationResultProxy`
# wraps a real, frozen `PredictionResult` and only overrides `to_dict()` to
# inject the plain-dict `"recommendation"` key `chat.py`/`evidence_branches.py`
# read (both treat it as an untyped dict, never the dataclass). Every other
# attribute access `service.py` makes (`.decision`, `.interval`,
# `.component_key`, `.current_tac`, `.part_number`, ...) is delegated
# unchanged to the wrapped instance via `__getattr__`.


class _RecommendationResultProxy:
    def __init__(self, inner: PredictionResult, recommendation: dict) -> None:
        self._inner = inner
        self._recommendation = recommendation

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def to_dict(self) -> dict:
        data = self._inner.to_dict()
        data["recommendation"] = self._recommendation
        return data


_RECOMMENDATION = {
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
    "reference_tac": 18660,
}


class _FakeRecommendationPredictor:
    def predict(self, prediction_input):
        inner = PredictionResult(
            decision=PredictionDecision.HISTORICAL_INTERVAL,
            reason=None,
            aircraft=prediction_input.aircraft_reg,
            workorder_id=prediction_input.wo_id,
            analysis_as_of=prediction_input.analysis_as_of,
            mode=prediction_input.mode,
            component_key="473597-5|AFT",
            part_number="473597-5",
            position="AFT",
            match=ComponentMatch(gate_basis="exact_pn_position"),
            interval=IntervalStats(
                basis="closing_tac",
                unit="aircraft_flight_cycles",
                p50=1200,
                p90=2400,
                min=100,
                max=3000,
                n=8,
                aircraft=5,
            ),
        )
        return _RecommendationResultProxy(inner, _RECOMMENDATION)


@pytest.mark.asyncio
async def test_recommendation_block_renders_first_before_section_a(runner, monkeypatch):
    async def bq_branch(request):
        del request
        return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)

    async def ipc_branch(request):
        del request
        return SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)
    monkeypatch.setattr(
        "pm_agent.workorders.chat.default_predictor",
        _FakeRecommendationPredictor,
    )

    session = await session_for(runner)
    result, text, _ = await turn(runner, session, files=[attachment()])
    assert result["analysis"]["pma"]["recommendation"]["component_key"] == "473597-5|AFT"
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
    assert '"decision": "recommendation"' in text or '"decision":"recommendation"' in text
    # Renders once, before section (a) - never duplicated inside it.
    rec_index = text.index("**Recommendation:")
    section_a_index = text.index("**(a) What the uploaded work order records**")
    assert rec_index < section_a_index
    assert text.count("**Recommendation:") == 1


@pytest.mark.asyncio
async def test_no_recommendation_means_no_block_renders(runner, monkeypatch):
    async def bq_branch(request):
        del request
        return SourceResult(source="bq_evidence", status=SourceStatus.NO_MATCH)

    async def ipc_branch(request):
        del request
        return SourceResult(source="ipc_manual_retrieval", status=SourceStatus.NO_MATCH)

    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_bq_branch", bq_branch)
    monkeypatch.setattr("pm_agent.nodes.evidence_branches._run_ipc_branch", ipc_branch)
    monkeypatch.setattr(
        "pm_agent.workorders.chat.default_predictor",
        _FakeHistoricalIntervalPredictor,
    )

    session = await session_for(runner)
    _, text, _ = await turn(runner, session, files=[attachment()])
    assert "**Recommendation:" not in text
    assert text.startswith("**(a) What the uploaded work order records**")
