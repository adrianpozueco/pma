"""Unit tests for `PrecursorPredictor`/`default_predictor`
(PMA-ONLINE-AGENT-plan.md §7/§8.1, task T09).

Uses a fake `Repository` throughout (no BigQuery, no credentials) that
returns plain `date`/`datetime`-bearing rows, mirroring what
`BigQueryPredictionRepository` actually hands back. Covers every reason in
§6.3 plus `historical_interval`; `aircraft_type_not_in_scope` and
`prediction_disabled` have no producing code path in `policy.py`/`service.py`
(the former is "reserved" per plan §11 OQ13, the latter is only ever
returned directly by `default_predictor()` returning `None` rather than by
`predict()`), so those two are exercised via `PredictionResult.disabled()`
directly rather than through `predict()`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from pm_agent.prediction import policy
from pm_agent.prediction.contracts import (
    BuildInfo,
    CurrentTac,
    DataSourceUnavailable,
    EmbeddingFailed,
    FocusComponent,
    JobInfo,
    LastReplacement,
    LeadStats,
    Neighbour,
    NeighbourLeadSample,
    PrecursorEvidence,
    PredictionDecision,
    PredictionInput,
    PredictionReason,
    PredictionResult,
)
from pm_agent.prediction.service import PrecursorPredictor, default_predictor
from pm_agent.prediction.settings import PredictionSettings

AS_OF = datetime(2026, 6, 1, tzinfo=UTC)
BUILD_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _valid_build_info() -> BuildInfo:
    return BuildInfo(
        table_creation_times=dict.fromkeys(policy.REQUIRED_BUILD_TABLES, BUILD_CREATED)
    )


def make_input(**overrides) -> PredictionInput:
    base = {
        "wo_id": "WO-1",
        "mode": "live",
        "analysis_as_of": AS_OF,
        "aircraft_reg": None,
        "ata_chapter": None,
        "position": None,
        "part_numbers": (),
        "header_part_number": None,
        "pma_wo_text": "pump leaking",
        "exclude_wo_uuids": (),
    }
    base.update(overrides)
    return PredictionInput(**base)


class FakeRepository:
    """A `Repository` (structurally) with fully injectable, benign-by-default
    responses, so each test configures only what it needs."""

    def __init__(
        self,
        *,
        build_info: BuildInfo | None = None,
        focus: tuple[FocusComponent, ...] = (),
        embed_query=None,
        anchor_neighbours: tuple[Neighbour, ...] = (),
        wo_neighbours: tuple[Neighbour, ...] = (),
        lead_time_stats: dict[str, LeadStats] | None = None,
        precursor_evidence: tuple[PrecursorEvidence, ...] = (),
        latest_closing_tac: CurrentTac | None = None,
        latest_closing_max_tac: int | None = None,
        job_log: tuple[JobInfo, ...] = (),
        last_replacement: LastReplacement | Exception | None = None,
        neighbour_lead_samples: tuple[NeighbourLeadSample, ...] | Exception = (),
    ) -> None:
        self._build_info = build_info if build_info is not None else _valid_build_info()
        self._focus = list(focus)
        self._embed_query = embed_query
        self._anchor_neighbours = list(anchor_neighbours)
        self._wo_neighbours = list(wo_neighbours)
        self._lead_time_stats = lead_time_stats or {}
        self._precursor_evidence = list(precursor_evidence)
        self._latest_closing_tac = latest_closing_tac
        self._latest_closing_max_tac = latest_closing_max_tac
        self._job_log = list(job_log)
        self._last_replacement = last_replacement
        self.last_replacement_args: tuple | None = None
        self._neighbour_lead_samples = neighbour_lead_samples
        self.neighbour_lead_samples_args: tuple | None = None
        self.calls: list[str] = []

    def curated_build_info(self) -> BuildInfo:
        self.calls.append("curated_build_info")
        return self._build_info

    def focus_components(self) -> list[FocusComponent]:
        self.calls.append("focus_components")
        return list(self._focus)

    def embed_query(self, wo_text: str) -> list[float]:
        self.calls.append("embed_query")
        if isinstance(self._embed_query, Exception):
            raise self._embed_query
        if self._embed_query is None:
            return [0.1, 0.2, 0.3]
        return list(self._embed_query)

    def anchor_neighbours(self, emb, as_of, exclude, k, min_sim):
        self.calls.append("anchor_neighbours")
        return list(self._anchor_neighbours)

    def wo_neighbours(self, emb, as_of, exclude, k, min_sim):
        self.calls.append("wo_neighbours")
        return list(self._wo_neighbours)

    def lead_time_stats(self, component_key, as_of, exclude) -> LeadStats:
        self.calls.append(f"lead_time_stats:{component_key}")
        return self._lead_time_stats[component_key]

    def precursor_evidence(self, component_key, as_of, exclude, neighbour_uuids, limit):
        self.calls.append("precursor_evidence")
        return list(self._precursor_evidence)

    def latest_closing_tac(self, reg, as_of, exclude_uuid) -> CurrentTac | None:
        self.calls.append("latest_closing_tac")
        return self._latest_closing_tac

    def latest_closing_max_tac_before_cutoff(
        self, reg, as_of, exclude_uuid
    ) -> int | None:
        self.calls.append("latest_closing_max_tac_before_cutoff")
        return self._latest_closing_max_tac

    def last_replacement(
        self, component_key, reg, as_of, exclude_uuid
    ) -> LastReplacement | None:
        self.calls.append("last_replacement")
        self.last_replacement_args = (component_key, reg, as_of, exclude_uuid)
        if isinstance(self._last_replacement, Exception):
            raise self._last_replacement
        return self._last_replacement

    def neighbour_lead_samples(
        self, uuids, as_of, exclude_uuid
    ) -> list[NeighbourLeadSample]:
        self.calls.append("neighbour_lead_samples")
        self.neighbour_lead_samples_args = (list(uuids), as_of, exclude_uuid)
        if isinstance(self._neighbour_lead_samples, Exception):
            raise self._neighbour_lead_samples
        return list(self._neighbour_lead_samples)

    @property
    def job_log(self) -> list[JobInfo]:
        return list(self._job_log)


def _fc(key: str, part: str, position: str | None, *, aliases=None) -> FocusComponent:
    return FocusComponent(
        component_key=key,
        part_number=part,
        position=position,
        part_key=part,
        part_aliases=aliases or (part,),
        freq_rank=1,
        replacement_count=5,
        aircraft_with_replacement=3,
    )


def _predict(repo: FakeRepository, pi: PredictionInput, *, settings=None, logs=None):
    settings = settings or PredictionSettings()
    log_fn = logs.append if logs is not None else None
    predictor = PrecursorPredictor(repo, settings, log_fn=log_fn)
    return predictor.predict(pi)


# --- reasons reachable through predict() -------------------------------------


def test_empty_text_never_touches_the_repository():
    repo = FakeRepository()
    result = _predict(repo, make_input(pma_wo_text="", part_numbers=()))

    assert result.reason == PredictionReason.EMPTY_TEXT
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert repo.calls == []
    json.dumps(result.to_dict())


def test_build_inconsistency_maps_to_data_source_unavailable():
    repo = FakeRepository(build_info=BuildInfo(table_creation_times={}))
    result = _predict(repo, make_input())

    assert result.reason == PredictionReason.DATA_SOURCE_UNAVAILABLE
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert result.reason_detail["detail"].startswith(
        "curated_build_inconsistent:missing_tables:"
    )
    json.dumps(result.to_dict())


def test_component_not_in_focus_set_when_header_part_has_no_focus_match():
    repo = FakeRepository(focus=[])
    pi = make_input(part_numbers=("PN-999",), header_part_number="PN-999")
    result = _predict(repo, pi)

    assert result.reason == PredictionReason.COMPONENT_NOT_IN_FOCUS_SET
    assert result.decision == PredictionDecision.OUT_OF_SCOPE
    json.dumps(result.to_dict())


def test_position_not_in_focus_set_when_matched_part_lacks_that_position():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    repo = FakeRepository(focus=[fc])
    pi = make_input(part_numbers=("PN1",), position="POS-B")
    result = _predict(repo, pi)

    assert result.reason == PredictionReason.POSITION_NOT_IN_FOCUS_SET
    assert result.decision == PredictionDecision.OUT_OF_SCOPE
    json.dumps(result.to_dict())


def test_ambiguous_position_fetches_stats_for_every_sibling_component():
    fc_a = _fc("PN1|POS-A", "PN1", "POS-A")
    fc_b = _fc("PN1|POS-B", "PN1", "POS-B")
    repo = FakeRepository(
        focus=[fc_a, fc_b],
        lead_time_stats={
            "PN1|POS-A": LeadStats(component_key="PN1|POS-A", n=3),
            "PN1|POS-B": LeadStats(component_key="PN1|POS-B", n=2),
        },
    )
    pi = make_input(part_numbers=("PN1",), position=None)
    # `show_recommendation` off: the condensed recommendation (approved
    # 2026-09-24) runs its own independent `embed_query`/`wo_neighbours`
    # regardless of gate outcome, which would otherwise mask this test's
    # "no evidence-gathering I/O on a failed gate" assertion below.
    settings = PredictionSettings(show_recommendation=False)
    result = _predict(repo, pi, settings=settings)

    assert result.reason == PredictionReason.AMBIGUOUS_POSITION
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert "lead_time_stats:PN1|POS-A" in repo.calls
    assert "lead_time_stats:PN1|POS-B" in repo.calls
    # Gate never passed, so no evidence-gathering I/O should have run.
    assert "embed_query" not in repo.calls
    assert "precursor_evidence" not in repo.calls
    json.dumps(result.to_dict())


def test_no_confident_component_match_when_vote_fails_and_no_part_numbers():
    repo = FakeRepository(focus=[], embed_query=[0.1, 0.2], anchor_neighbours=[])
    pi = make_input(
        part_numbers=(), header_part_number=None, pma_wo_text="pump leaking"
    )
    result = _predict(repo, pi)

    assert result.reason == PredictionReason.NO_CONFIDENT_COMPONENT_MATCH
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    json.dumps(result.to_dict())


def test_embedding_failed_status_maps_to_embedding_failed_reason():
    repo = FakeRepository(
        focus=[], embed_query=EmbeddingFailed("bad", detail="status:error")
    )
    pi = make_input(part_numbers=(), pma_wo_text="pump leaking")
    result = _predict(repo, pi)

    assert result.reason == PredictionReason.EMBEDDING_FAILED
    assert result.reason_detail == {"detail": "status:error"}
    json.dumps(result.to_dict())


def test_embedding_wrong_dimension_maps_to_embedding_incompatible_reason():
    repo = FakeRepository(
        focus=[], embed_query=EmbeddingFailed("bad dim", detail="dim:5")
    )
    pi = make_input(part_numbers=(), pma_wo_text="pump leaking")
    result = _predict(repo, pi)

    assert result.reason == PredictionReason.EMBEDDING_INCOMPATIBLE
    assert result.reason_detail == {"detail": "dim:5"}
    json.dumps(result.to_dict())


def test_no_lead_time_samples_when_matched_component_has_zero_samples():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    repo = FakeRepository(
        focus=[fc],
        lead_time_stats={"PN1|POS-A": LeadStats(component_key="PN1|POS-A", n=0)},
        embed_query=[0.1, 0.2],
    )
    pi = make_input(part_numbers=("PN1",), position="POS-A")
    result = _predict(repo, pi)

    assert result.reason == PredictionReason.NO_LEAD_TIME_SAMPLES
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert result.component_key == "PN1|POS-A"
    json.dumps(result.to_dict())


def test_insufficient_samples_when_n_below_min_sample():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    stats = LeadStats(
        component_key="PN1|POS-A",
        n=2,
        lead_p50=100.0,
        lead_p90=200.0,
        replacement_interval_share=0.1,
    )
    repo = FakeRepository(
        focus=[fc], lead_time_stats={"PN1|POS-A": stats}, embed_query=[0.1, 0.2]
    )
    pi = make_input(part_numbers=("PN1",), position="POS-A")
    settings = PredictionSettings(min_sample=5)
    result = _predict(repo, pi, settings=settings)

    assert result.reason == PredictionReason.INSUFFICIENT_SAMPLES
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    json.dumps(result.to_dict())


def test_samples_not_symptom_to_replacement_when_share_too_high():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    stats = LeadStats(
        component_key="PN1|POS-A",
        n=10,
        lead_p50=100.0,
        lead_p90=200.0,
        replacement_interval_share=0.9,
    )
    repo = FakeRepository(
        focus=[fc], lead_time_stats={"PN1|POS-A": stats}, embed_query=[0.1, 0.2]
    )
    pi = make_input(part_numbers=("PN1",), position="POS-A")
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.reason == PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    json.dumps(result.to_dict())


def test_historical_interval_full_happy_path_with_evidence_and_current_tac():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    stats = LeadStats(
        component_key="PN1|POS-A",
        n=10,
        aircraft_n=8,
        replacement_n=9,
        lead_min=5,
        lead_max=400,
        lead_p50=100.0,
        lead_p90=300.0,
        lead_mean=150.0,
        lead_sd=40.0,
        cv=0.27,
        replacement_interval_share=0.1,
        chained_sample_n=2,
        replacement_wo_uuids=("wo-a", "wo-b"),
    )
    precursor = PrecursorEvidence(
        component_key="PN1|POS-A",
        aircraft_reg="SP-REG00374",
        precursor_wo_id="wo-100",
        precursor_wo_uuid="prec-uuid-1",
        precursor_tac=4100,
        replacement_wo_uuid="wo-uuid-1",
        replacement_tac=4200,
        replacement_date=date(2026, 3, 1),
        lead_cycles=100,
        sim=0.9,
        reason="same symptom",
        precursor_snippet="pump warning",
        is_neighbour_hit=True,
    )
    wo_neighbour = Neighbour(
        wo_uuid="wo-uuid-2",
        wo_id="190306536",
        aircraft_reg="EI-REG00263",
        sim=0.82,
        snippet="pump replaced",
        ata_chapter="29-11",
        tac=6072,
        closing_date=date(2026, 2, 1),
    )
    current_tac = CurrentTac(
        value=4180,
        source="latest_closing_tac",
        observed_at=datetime(2026, 5, 30, tzinfo=UTC),
    )
    repo = FakeRepository(
        focus=[fc],
        lead_time_stats={"PN1|POS-A": stats},
        embed_query=[0.1, 0.2, 0.3],
        precursor_evidence=[precursor],
        wo_neighbours=[wo_neighbour],
        latest_closing_tac=current_tac,
        latest_closing_max_tac=4000,
        job_log=[JobInfo(job_id="job-1", total_bytes_billed=12345)],
    )
    pi = make_input(
        part_numbers=("PN1",),
        position="POS-A",
        aircraft_reg="SP-REG00374",
        exclude_wo_uuids=("self-uuid",),
    )
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.reason is None
    assert result.decision == PredictionDecision.HISTORICAL_INTERVAL
    assert result.component_key == "PN1|POS-A"
    assert result.interval is not None
    assert result.interval.p50 == 100.0
    assert result.current_tac == current_tac
    assert len(result.evidence) >= 1
    assert result.provenance is not None
    assert result.provenance.bq_job_ids == ("job-1",)
    assert "embed_query" in repo.calls
    assert "precursor_evidence" in repo.calls
    assert "wo_neighbours" in repo.calls
    assert "latest_closing_tac" in repo.calls
    assert "latest_closing_max_tac_before_cutoff" in repo.calls
    # date/datetime-bearing fake rows must survive a full json round trip.
    dumped = json.dumps(result.to_dict())
    reloaded = json.loads(dumped)
    assert reloaded["decision"] == "historical_interval"


# --- reasons NOT reachable through predict() (verified directly) ------------


def test_aircraft_type_not_in_scope_is_reserved_and_only_built_via_disabled():
    result = PredictionResult.disabled(
        PredictionReason.AIRCRAFT_TYPE_NOT_IN_SCOPE, workorder_id="WO-1"
    )
    assert result.decision == PredictionDecision.OUT_OF_SCOPE
    json.dumps(result.to_dict())


def test_prediction_disabled_is_only_built_via_disabled():
    result = PredictionResult.disabled(
        PredictionReason.PREDICTION_DISABLED, workorder_id="WO-1"
    )
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert result.missing == ("feature_flag",)
    json.dumps(result.to_dict())


# --- prediction log -----------------------------------------------------------


def test_prediction_log_record_has_required_keys_and_labels():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    repo = FakeRepository(
        focus=[fc],
        lead_time_stats={"PN1|POS-A": LeadStats(component_key="PN1|POS-A", n=0)},
        embed_query=[0.1],
    )
    logs: list[dict] = []
    pi = make_input(part_numbers=("PN1",), position="POS-A")
    _predict(repo, pi, logs=logs)

    assert len(logs) == 1
    record = logs[0]
    for key in (
        "request_id",
        "wo_id",
        "wo_text_sha256",
        "pma",
        "table_creation_times",
        "embedding_endpoint",
        "settings_hash",
        "bq_job_ids",
        "total_bytes_billed",
        "latency_ms",
        "labels",
    ):
        assert key in record, f"missing log key {key!r}"
    assert record["labels"] == {
        "type": "agent_telemetry",
        "service_name": "pma-agent",
        "event": "pma_prediction",
    }
    assert record["wo_id"] == "WO-1"
    assert isinstance(record["pma"], dict)
    assert record["pma"]["reason"] == "no_lead_time_samples"
    # Raw WO text never leaves the log; only its hash does.
    assert "pump" not in json.dumps(record)
    json.dumps(record)


def test_prediction_log_is_emitted_even_when_no_log_fn_is_configured():
    # Default `_default_log_emitter` (stdlib logging fallback) must not raise.
    repo = FakeRepository()
    result = _predict(repo, make_input(pma_wo_text="", part_numbers=()))
    assert result.reason == PredictionReason.EMPTY_TEXT


# --- default_predictor() -------------------------------------------------------


def test_default_predictor_returns_none_when_flag_is_off(monkeypatch):
    monkeypatch.delenv("PMA_PREDICTION_ENABLED", raising=False)
    assert default_predictor() is None


def test_default_predictor_returns_none_when_flag_explicitly_false(monkeypatch):
    monkeypatch.setenv("PMA_PREDICTION_ENABLED", "false")
    assert default_predictor() is None


def test_default_predictor_does_not_import_repository_module_when_disabled(monkeypatch):
    monkeypatch.delenv("PMA_PREDICTION_ENABLED", raising=False)
    sys.modules.pop("pm_agent.prediction.repository", None)

    assert default_predictor() is None
    assert "pm_agent.prediction.repository" not in sys.modules


def test_default_predictor_turns_construction_failure_into_data_source_unavailable(
    monkeypatch,
):
    monkeypatch.setenv("PMA_PREDICTION_ENABLED", "true")
    import pm_agent.prediction.repository as repo_module

    def _boom(settings=None):
        raise RuntimeError("credentials missing")

    monkeypatch.setattr(repo_module, "build_default_repository", _boom)

    predictor = default_predictor()
    assert predictor is not None

    result = predictor.predict(make_input())
    assert result.reason == PredictionReason.DATA_SOURCE_UNAVAILABLE
    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    # Only the exception type name travels, never `str(exc)` (credential-safety
    # convention mirrored from `bq_analytics.queries._failure`).
    assert result.reason_detail == {"detail": "construction_failed:RuntimeError"}
    assert "credentials missing" not in json.dumps(result.to_dict())
    json.dumps(result.to_dict())


def test_default_predictor_builds_a_working_predictor_on_successful_construction(
    monkeypatch,
):
    monkeypatch.setenv("PMA_PREDICTION_ENABLED", "true")
    import pm_agent.prediction.repository as repo_module

    fake_repo = FakeRepository()

    def _fake_build(settings=None):
        return fake_repo

    monkeypatch.setattr(repo_module, "build_default_repository", _fake_build)

    predictor = default_predictor()
    assert isinstance(predictor, PrecursorPredictor)

    result = predictor.predict(make_input(pma_wo_text="", part_numbers=()))
    assert result.reason == PredictionReason.EMPTY_TEXT


def test_settings_default_anchor_threshold_is_backtest_choice(monkeypatch):
    # Backtest re-run 2026-09-24 on the 20-part rebuild (1720 pre-cutoff
    # anchors): 0.84 is the lowest threshold with false-gate rate <= 5% (3.8%).
    monkeypatch.delenv("PMA_ONLINE_ANCHOR_SIM_THRESHOLD", raising=False)
    monkeypatch.delenv("PMA_ANCHOR_SIM_THRESHOLD", raising=False)
    assert PredictionSettings.from_env().anchor_sim_threshold == 0.84


def test_settings_reads_plan_documented_anchor_threshold_env(monkeypatch):
    monkeypatch.setenv("PMA_ANCHOR_SIM_THRESHOLD", "0.9")
    assert PredictionSettings.from_env().anchor_sim_threshold == 0.9
    monkeypatch.setenv("PMA_ONLINE_ANCHOR_SIM_THRESHOLD", "0.85")
    assert PredictionSettings.from_env().anchor_sim_threshold == 0.85


def test_settings_default_show_supporting_interval_and_projected_window_are_true():
    # Option B (approved 2026-09-24): both now default on, overriding the
    # plan's original `PMA_SHOW_SUPPORTING_INTERVAL | false` (§5.8/OQ6).
    settings = PredictionSettings()
    assert settings.show_supporting_interval is True
    assert settings.show_projected_window is True


def test_settings_show_projected_window_env_override(monkeypatch):
    monkeypatch.setenv("PMA_SHOW_PROJECTED_WINDOW", "false")
    assert PredictionSettings.from_env().show_projected_window is False
    monkeypatch.setenv("PMA_SHOW_PROJECTED_WINDOW", "true")
    assert PredictionSettings.from_env().show_projected_window is True


# --- Option B projected window (approved 2026-09-24) ---------------------------


def _share_too_high_repo_and_input(**repo_overrides):
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    stats = LeadStats(
        component_key="PN1|POS-A",
        n=10,
        lead_p50=100.0,
        lead_p90=200.0,
        replacement_interval_share=0.9,
    )
    repo = FakeRepository(
        focus=[fc],
        lead_time_stats={"PN1|POS-A": stats},
        embed_query=[0.1, 0.2],
        **repo_overrides,
    )
    pi = make_input(
        part_numbers=("PN1",),
        position="POS-A",
        aircraft_reg="SP-REG00374",
        exclude_wo_uuids=("self-uuid",),
    )
    return repo, pi


def test_projected_window_populated_when_last_replacement_found():
    last = LastReplacement(
        tac=17938,
        replacement_date=date(2026, 4, 25),
        wo_id="WO-OLD-1",
        wo_uuid="wo-old-uuid-1",
    )
    current_tac = CurrentTac(
        value=18660, source="latest_closing_tac", observed_at=AS_OF
    )
    repo, pi = _share_too_high_repo_and_input(
        last_replacement=last,
        latest_closing_tac=current_tac,
        latest_closing_max_tac=18660,
    )
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.reason == PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT
    assert result.supporting_interval is not None
    assert result.projected_window is not None
    assert result.projected_window.tac_p50 == 17938 + 100
    assert result.projected_window.tac_p90 == 17938 + 200
    assert result.projected_window.cycles_since_last_replacement == 18660 - 17938
    assert result.projected_window.position == "past_p90"
    assert "projected_window_not_a_forecast" in result.limitations
    assert "last_replacement" in repo.calls
    # Leakage guards: the analysis cut-off (never the wall clock) and the
    # uploaded WO's own uuid are what reach the repository.
    assert repo.last_replacement_args == (
        "PN1|POS-A",
        "SP-REG00374",
        pi.analysis_as_of,
        "self-uuid",
    )
    d = result.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["projected_window"]["last_replacement_date"] == "2026-04-25"


def test_projected_window_binds_empty_exclude_uuid_when_upload_has_none():
    last = LastReplacement(
        tac=17938, replacement_date=date(2026, 4, 25), wo_id="WO-OLD-1", wo_uuid="u1"
    )
    repo, pi = _share_too_high_repo_and_input(last_replacement=last)
    pi = replace(pi, exclude_wo_uuids=())
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    _predict(repo, pi, settings=settings)

    assert repo.last_replacement_args is not None
    assert repo.last_replacement_args[3] == ""


def test_projected_window_ignores_current_tac_observed_after_cutoff():
    last = LastReplacement(
        tac=17938, replacement_date=date(2026, 4, 25), wo_id="WO-OLD-1", wo_uuid="u1"
    )
    later = CurrentTac(
        value=20000,
        source="user_supplied",
        observed_at=AS_OF + timedelta(days=30),
    )
    repo, pi = _share_too_high_repo_and_input(
        last_replacement=last, latest_closing_tac=later
    )
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.projected_window is not None
    assert result.projected_window.tac_p50 == 17938 + 100
    assert result.projected_window.cycles_since_last_replacement is None
    assert result.projected_window.position is None


def test_projected_window_none_and_limitation_when_no_prior_replacement():
    repo, pi = _share_too_high_repo_and_input(last_replacement=None)
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.projected_window is None
    assert "no_prior_replacement_on_aircraft" in result.limitations
    assert "projected_window_not_a_forecast" not in result.limitations


def test_projected_window_lookup_failure_does_not_fail_the_prediction():
    repo, pi = _share_too_high_repo_and_input(
        last_replacement=DataSourceUnavailable("boom", detail="timeout")
    )
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.reason == PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT
    assert result.projected_window is None
    assert "projected_window_unavailable" in result.limitations
    assert "projected_window_not_a_forecast" not in result.limitations
    d = result.to_dict()
    assert json.loads(json.dumps(d)) == d


def test_projected_window_unexpected_error_does_not_fail_the_prediction():
    repo, pi = _share_too_high_repo_and_input(last_replacement=RuntimeError("x"))
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.decision == PredictionDecision.NO_RELIABLE_PREDICTION
    assert result.reason == PredictionReason.SAMPLES_NOT_SYMPTOM_TO_REPLACEMENT
    assert result.projected_window is None
    assert "projected_window_unavailable" in result.limitations
    assert "projected_window_not_a_forecast" not in result.limitations


def test_projected_window_skipped_when_aircraft_reg_missing():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    stats = LeadStats(
        component_key="PN1|POS-A",
        n=10,
        lead_p50=100.0,
        lead_p90=200.0,
        replacement_interval_share=0.9,
    )
    repo = FakeRepository(
        focus=[fc],
        lead_time_stats={"PN1|POS-A": stats},
        embed_query=[0.1, 0.2],
        last_replacement=RuntimeError("must not be called"),
    )
    pi = make_input(part_numbers=("PN1",), position="POS-A")
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.projected_window is None
    assert "last_replacement" not in repo.calls


def test_projected_window_skipped_when_setting_disabled():
    last = LastReplacement(
        tac=17938, replacement_date=date(2026, 4, 25), wo_id="WO-OLD-1", wo_uuid="u1"
    )
    repo, pi = _share_too_high_repo_and_input(last_replacement=last)
    # `show_recommendation` off too: the condensed recommendation (approved
    # 2026-09-24) fetches its own `last_replacement` (to anchor the
    # `fleet_replacement_interval`/`component_history` TAC projection)
    # independently of `show_projected_window`, which would otherwise mask
    # this test's "projected_window's own last_replacement call is skipped"
    # assertion below.
    settings = PredictionSettings(
        min_sample=5,
        max_replacement_interval_share=0.5,
        show_projected_window=False,
        show_recommendation=False,
    )
    result = _predict(repo, pi, settings=settings)

    assert result.projected_window is None
    assert "last_replacement" not in repo.calls


# --- condensed recommendation orchestration (approved 2026-09-24) ------------


def _rec_neighbours(n: int = 8, sim: float = 0.85) -> list[Neighbour]:
    return [
        Neighbour(
            wo_uuid=f"nb-{i}", wo_id=f"WO-NB-{i}", aircraft_reg=f"REG{i}", sim=sim, snippet=""
        )
        for i in range(n)
    ]


def _rec_samples(key: str, n: int = 8, lead: int = 300) -> list[NeighbourLeadSample]:
    return [
        NeighbourLeadSample(
            component_key=key,
            precursor_wo_uuid=f"nb-{i}",
            aircraft_reg=f"REG{i}",
            lead_cycles=lead + 10 * i,
        )
        for i in range(n)
    ]


def test_recommendation_similar_workorders_happy_path_and_limitation():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    stats = LeadStats(component_key="PN1|POS-A", n=10, lead_p50=100.0, lead_p90=300.0)
    current = CurrentTac(value=5000, source="latest_closing_tac", observed_at=AS_OF)
    repo = FakeRepository(
        focus=[fc],
        lead_time_stats={"PN1|POS-A": stats},
        wo_neighbours=_rec_neighbours(),
        neighbour_lead_samples=_rec_samples("PN1|POS-A"),
        latest_closing_tac=current,
    )
    pi = make_input(
        part_numbers=("PN1",),
        position="POS-A",
        aircraft_reg="SP-REG00374",
        exclude_wo_uuids=("self-uuid",),
    )
    result = _predict(repo, pi)

    rec = result.recommendation
    assert rec is not None
    assert rec.basis == "similar_workorders"
    assert rec.component_key == "PN1|POS-A"
    assert rec.reference_tac == 5000
    p = rec.predicted_replacement
    assert p.lead_tac_p50 == 335  # median of 300..370 step 10
    assert p.tac_p50 == 5000 + p.lead_tac_p50
    assert p.tac_p90 == 5000 + p.lead_tac_p90
    assert "recommendation_heuristic_not_calibrated" in result.limitations
    assert "recommendation_unavailable" not in result.limitations
    assert repo.neighbour_lead_samples_args is not None
    assert repo.neighbour_lead_samples_args[2] == "self-uuid"
    assert repo.neighbour_lead_samples_args[1] == pi.analysis_as_of
    d = result.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["recommendation"]["basis"] == "similar_workorders"


def test_recommendation_computed_for_ungated_wo_with_text():
    """No part numbers, vote fails -> gate does not pass, yet the WO text still
    gets embedded once and can produce a vote-based recommendation."""
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    current = CurrentTac(value=5000, source="latest_closing_tac", observed_at=AS_OF)
    repo = FakeRepository(
        focus=[fc],
        wo_neighbours=_rec_neighbours(),
        neighbour_lead_samples=_rec_samples("PN1|POS-A"),
        latest_closing_tac=current,
    )
    pi = make_input(aircraft_reg="SP-REG00374")
    result = _predict(repo, pi)

    assert result.decision != PredictionDecision.HISTORICAL_INTERVAL
    assert result.recommendation is not None
    assert result.recommendation.basis == "similar_workorders"
    assert repo.calls.count("embed_query") == 1


def test_recommendation_none_when_no_current_tac_and_no_fallback():
    repo = FakeRepository(
        focus=[_fc("PN1|POS-A", "PN1", "POS-A")],
        wo_neighbours=_rec_neighbours(),
        neighbour_lead_samples=_rec_samples("PN1|POS-A"),
    )
    result = _predict(repo, make_input())

    assert result.recommendation is None
    assert "recommendation_heuristic_not_calibrated" not in result.limitations
    assert "recommendation_unavailable" not in result.limitations


def test_recommendation_ignores_current_tac_observed_after_cutoff():
    later = CurrentTac(
        value=9000, source="user_supplied", observed_at=AS_OF + timedelta(days=30)
    )
    repo = FakeRepository(
        focus=[_fc("PN1|POS-A", "PN1", "POS-A")],
        wo_neighbours=_rec_neighbours(),
        neighbour_lead_samples=_rec_samples("PN1|POS-A"),
        latest_closing_tac=later,
    )
    result = _predict(repo, make_input(aircraft_reg="SP-REG00374"))

    assert result.recommendation is None


def test_recommendation_failure_never_fails_predict():
    fc = _fc("PN1|POS-A", "PN1", "POS-A")
    stats = LeadStats(component_key="PN1|POS-A", n=10, lead_p50=100.0, lead_p90=300.0)
    repo = FakeRepository(
        focus=[fc],
        lead_time_stats={"PN1|POS-A": stats},
        wo_neighbours=_rec_neighbours(),
        neighbour_lead_samples=DataSourceUnavailable("boom", detail="timeout"),
        latest_closing_tac=CurrentTac(
            value=5000, source="latest_closing_tac", observed_at=AS_OF
        ),
    )
    pi = make_input(part_numbers=("PN1",), position="POS-A", aircraft_reg="R")
    settings = PredictionSettings(min_sample=5, max_replacement_interval_share=0.5)
    result = _predict(repo, pi, settings=settings)

    assert result.decision == PredictionDecision.HISTORICAL_INTERVAL
    assert result.recommendation is None
    assert "recommendation_unavailable" in result.limitations


def test_recommendation_skipped_when_setting_disabled():
    repo = FakeRepository(
        focus=[_fc("PN1|POS-A", "PN1", "POS-A")],
        wo_neighbours=_rec_neighbours(),
        neighbour_lead_samples=_rec_samples("PN1|POS-A"),
        latest_closing_tac=CurrentTac(
            value=5000, source="latest_closing_tac", observed_at=AS_OF
        ),
    )
    settings = PredictionSettings(show_recommendation=False)
    result = _predict(repo, make_input(aircraft_reg="R"), settings=settings)

    assert result.recommendation is None
    assert "neighbour_lead_samples" not in repo.calls
    assert result.to_dict()["recommendation"] is None
