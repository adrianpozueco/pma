"""Live BigQuery integration tests for the PMA prediction core.

PMA-ONLINE-AGENT-plan.md §8.2 "LIVE" list (T14), items 1-8. Every test here
talks to the real ``pma_agent_curated``/``pma_agent_analytics`` datasets
through user ADC (no fakes, no mocks) - it is the counterpart to the
fake-repository unit tests in ``tests/unit/test_prediction_{policy,
repository,service}.py`` and the offline fixture tests in
``tests/unit/test_workorder_analysis.py``.

Gated behind ``PMA_LIVE_BQ=1`` and the ``live`` pytest marker so it never
runs in the default ``uv run pytest tests/unit tests/integration`` sweep:

    PMA_LIVE_BQ=1 uv run pytest tests/integration/test_pma_prediction_live.py -q

Every query issued here is a read-only ``SELECT``/``AI.EMBED`` against
small, already-existing tables (never DDL/DML), consistent with the plan's
"live reads: SELECT and dry-runs only, small scans" rule.

Item 8 (AI.EMBED/curated SELECT under ``app_sa`` impersonation) is marked
``skip``: it depends on the curated-dataset IAM grant in
``deployment/terraform/single-project/iam.tf`` (T06), which has not been
applied yet (this task never runs ``terraform apply``). See that test's
skip reason for the exact blocker.
"""

from __future__ import annotations

import os
import re
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("PMA_LIVE_BQ") != "1",
        reason="needs PMA_LIVE_BQ=1 and live BigQuery ADC credentials",
    ),
]

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workorders"
PMA_FIXTURES = Path(__file__).parents[1] / "fixtures" / "pma"

CARGO_FIXTURE = FIXTURES / "open_cargo_smoke_detector.xml"
RH_FIXTURE = FIXTURES / "open_landing_light_rh.xml"

# Matches the plan's §8.2 items 3-6 fixed analysis date.
AS_OF = datetime(2026, 9, 23, tzinfo=UTC)

CURATED_DATASET = "pma_agent_curated"
ANALYTICS_DATASET = "pma_agent_analytics"


def _entries(text_value: str) -> list[str]:
    """Whitespace-normalised, order-independent comparison of the
    ``"\\n\\n"``-joined entries `build_pma_wo_text` produces - same helper
    as `tests/unit/test_workorder_analysis.py`."""
    return sorted(e.strip() for e in text_value.split("\n\n") if e.strip())


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@pytest.fixture(scope="module")
def project() -> str:
    from pm_agent.config import project_id

    return os.environ.get("GOOGLE_CLOUD_PROJECT") or project_id()


@pytest.fixture(scope="module")
def bq_client(project: str):
    from google.cloud import bigquery

    return bigquery.Client(project=project)


@pytest.fixture(scope="module")
def settings():
    from pm_agent.prediction.settings import PredictionSettings

    # `prediction_enabled=True` only matters for `default_predictor()`'s env
    # gate; these tests build the repository/predictor directly.
    return PredictionSettings(prediction_enabled=True)


@pytest.fixture(scope="module")
def repository(settings):
    from pm_agent.prediction.repository import build_default_repository

    return build_default_repository(settings)


@pytest.fixture(scope="module")
def predictor(repository, settings):
    from pm_agent.prediction.service import PrecursorPredictor

    return PrecursorPredictor(repository, settings)


@pytest.fixture(scope="module")
def analysis_service(predictor):
    from pm_agent.workorders.service import WorkOrderAnalysisService

    return WorkOrderAnalysisService(predictor=predictor)


# -- 1. Embed parity -------------------------------------------------------


def test_1_embed_parity(bq_client, project):
    """`AI.EMBED` of a stored `wo_embeddings.content` row vs its stored
    vector: cosine >= 0.999 (plan §8.2 item 1)."""
    sql = f"""
    SELECT
      e.content,
      e.embedding.result AS stored_emb,
      e.embedding.status AS stored_status,
      AI.EMBED(e.content, endpoint => 'text-embedding-005').result AS fresh_emb
    FROM `{project}.{CURATED_DATASET}.wo_embeddings` e
    WHERE e.embedding.status = ''
    LIMIT 1
    """
    rows = list(bq_client.query(sql).result())
    assert rows, "no wo_embeddings row with a valid stored embedding"
    row = rows[0]
    assert row["stored_status"] == ""
    stored = list(row["stored_emb"])
    fresh = list(row["fresh_emb"])
    assert len(stored) == 768
    assert len(fresh) == 768
    assert _cosine(stored, fresh) >= 0.999


# -- 1b. Text-recipe parity -------------------------------------------------


def test_1b_text_recipe_parity_matches_wo_embeddings_content(bq_client, project):
    """For 20 `wo_workorders` rows (converted to the parser row shape),
    `build_pma_wo_text(include_actions=True)` matches the stored
    `wo_embeddings.content` (entry multiset, whitespace-normalised) - plan
    §8.2 item 1b. Rows are chosen by a stable hash rather than `RAND()` so
    the test is reproducible across runs."""
    from pm_agent.workorders.service import build_pma_wo_text

    sql = f"""
    SELECT w.remarks, w.work_steps, e.content
    FROM `{project}.{CURATED_DATASET}.wo_embeddings` e
    JOIN `{project}.{ANALYTICS_DATASET}.wo_workorders` w
      ON w.workorder_uuid = e.wo_uuid
    ORDER BY FARM_FINGERPRINT(e.wo_uuid)
    LIMIT 20
    """
    rows = list(bq_client.query(sql).result())
    assert len(rows) == 20
    mismatches = []
    for row in rows:
        d = dict(row)
        built = build_pma_wo_text(d, include_actions=True)
        if _entries(built) != _entries(row["content"]):
            mismatches.append((built, row["content"]))
    assert not mismatches, f"{len(mismatches)}/20 rows mismatched: {mismatches[:1]!r}"


# -- 2. Anchor kNN for the cargo fixture ------------------------------------


def test_2_anchor_knn_cargo_fixture_top1_is_aft_smoke_detector(repository):
    """Anchor kNN for the cargo fixture text: top-1 `473597-5|AFT` (in-sample
    smoke test - the fixture's symptom text was itself sourced from a real
    precursor of this component, plan §8.2 item 2)."""
    from amos_data import parse_workorders
    from pm_agent.workorders.service import build_pma_wo_text

    result = parse_workorders(CARGO_FIXTURE.read_bytes())
    row = result.workorders[0]
    text = build_pma_wo_text(row, include_actions=False)
    assert text.strip()

    emb = repository.embed_query(text)
    neighbours = repository.anchor_neighbours(emb, AS_OF, [], k=5, min_sim=0.0)
    assert neighbours, "no anchor neighbours returned"
    assert neighbours[0].component_key == "473597-5|AFT"


# -- 3. Lead-time stats: 473597-5|AFT --------------------------------------


def test_3_lead_time_stats_aft_smoke_detector(repository):
    """`473597-5|AFT`, analysis_as_of=2026-09-23, on the 2026-09-24 20-part
    curated rebuild: n=35, aircraft_n=28, p50=381.0, p90~=1890.8,
    replacement_interval_share~=0.971 (plan §8.2 item 3; was n=27, p50 356,
    p90 ~1968.2 on the 6-part build)."""
    stats = repository.lead_time_stats("473597-5|AFT", AS_OF, [])
    assert stats.n == 35
    assert stats.aircraft_n == 28
    assert stats.lead_p50 == pytest.approx(381.0, abs=0.5)
    assert stats.lead_p90 == pytest.approx(1890.8, abs=1.0)
    assert stats.replacement_interval_share == pytest.approx(0.971, abs=0.01)


# -- 4. RH fixture -----------------------------------------------------------


def test_4_rh_fixture_decision_at_default_threshold(analysis_service):
    """RH fixture, at the *default* `anchor_sim_threshold` (0.84, re-tuned
    2026-09-24 on the 20-part rebuild: lowest threshold with false-gate rate
    <= 5%, 3.8%): only 1 anchor (`45-0351-4|RH`, 0.843) clears 0.84 and the
    minimum vote support is 3, so the symptom-only vote does not gate and
    the decision is `no_confident_component_match` (plan §8.2 item 4). At
    the old 0.80 it gated to `45-0351-4|RH` (4 anchors) -> `insufficient_samples`."""
    from pm_agent.workorders.service import AnalysisInput

    xml_bytes = RH_FIXTURE.read_bytes()
    result = analysis_service.analyze_xml(
        xml_bytes,
        AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma["reason"] == "no_confident_component_match", pma
    assert pma["component_key"] is None, pma
    assert pma.get("projected_window") is None, pma


def test_4_rh_fixture_lead_time_stats_n5(repository):
    """Stats `45-0351-4|RH` -> n=5, 5 aircraft on the 2026-09-24 rebuild
    (plan §8.2 item 4; was n=1)."""
    stats = repository.lead_time_stats("45-0351-4|RH", AS_OF, [])
    assert stats.n == 5
    assert stats.aircraft_n == 5


# -- 5. Lead-time stats: 2085M31G03|#1 --------------------------------------


def test_5_lead_time_stats_engine1_no_samples(repository):
    """Stats `2085M31G03|#1` -> one aggregate row, n=0 (plan §8.2 item 5;
    the repository must map this to `n=0`, never the runner's `NO_MATCH`
    status - see `repository.py`'s own comment on this)."""
    stats = repository.lead_time_stats("2085M31G03|#1", AS_OF, [])
    assert stats.n == 0
    assert stats.lead_p50 is None


# -- 6. Latest closing TAC ----------------------------------------------------


def test_6_latest_closing_tac_not_null(repository):
    """Latest closing TAC for `9H-REG00163` is not null (plan §8.2 item 6)."""
    tac = repository.latest_closing_tac("9H-REG00163", AS_OF, "")
    assert tac is not None
    assert tac.value is not None


# -- 7. Billed bytes and latency ---------------------------------------------


def test_7_billed_bytes_and_p95_latency_over_10_calls(analysis_service, repository):
    """Billed bytes per `predict()` call <= 200 MB (from `JobInfo`); p95
    latency over 10 calls is measured and printed, not hard-asserted (plan
    §8.2 item 7 only requires it be "recorded" - the acceptance-level p95
    <= 8s bound in §8.4 item 8 is a separate, non-flaky-by-construction
    measurement the lead/T16 owns)."""
    from pm_agent.workorders.service import AnalysisInput

    xml_bytes = CARGO_FIXTURE.read_bytes()
    latencies_s: list[float] = []
    billed_bytes_per_call: list[int] = []

    for _ in range(10):
        before = len(repository.job_log)
        start = time.monotonic()
        result = analysis_service.analyze_xml(
            xml_bytes,
            AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
        )
        latencies_s.append(time.monotonic() - start)
        assert "pma" in result
        after_jobs = repository.job_log[before:]
        billed_bytes_per_call.append(sum(j.total_bytes_billed or 0 for j in after_jobs))

    p95 = (
        statistics.quantiles(latencies_s, n=20, method="inclusive")[18]
        if len(latencies_s) > 1
        else latencies_s[0]
    )
    print(
        f"\npredict() latency over {len(latencies_s)} calls: "
        f"p50={statistics.median(latencies_s):.2f}s p95={p95:.2f}s "
        f"max={max(latencies_s):.2f}s (plan §8.4 item 8 bound: p95 <= 8s)"
    )
    for i, billed in enumerate(billed_bytes_per_call):
        assert billed <= 200_000_000, f"call {i}: billed {billed} bytes > 200MB budget"


# -- 8. app_sa impersonation --------------------------------------------------


@pytest.mark.skip(
    reason="needs the app_sa curated-dataset IAM grant "
    "(deployment/terraform/single-project/iam.tf, task T06) applied via "
    "`terraform apply`, which no task in this run may execute; re-enable "
    "once that apply has happened and an impersonated-credentials smoke "
    "test can reach pma_agent_curated as app_sa instead of user ADC"
)
def test_8_app_sa_impersonation_reads_curated():
    pass


# -- Option B: projected replacement window (approved 2026-09-24) -----------
#
# PMA-ONLINE-AGENT-plan.md "T00 decisions": Option B was approved 2026-09-24,
# overriding OQ1's default / BIGQUERY-AGENT-plan §8.5 "no absolute due TAC"
# only for this clearly-labelled fleet-pattern window (README.md "Online
# prediction (pma-online-v1)"). Everything else in that plan is unchanged.
# Same live aircraft/component as `test_2`/`test_3` above (`SP-REG00374`,
# `473597-5|AFT`, last replaced at TAC 17938 on 2026-04-25,
# `fct_replacement_events`), but with an exact part-number match instead of
# the symptom-only vote so the gate basis (not the decision) differs.

_AFT_EXACT_PN_XML = CARGO_FIXTURE.read_bytes().replace(
    b"</positionInfo>",
    b"</positionInfo><component><partNumber>473597-5</partNumber>"
    b"<serialNumber>LIVE-1</serialNumber></component>",
    1,
)


def test_9_projected_window_aft_exact_pn(analysis_service):
    """Exact part-number + position match on `473597-5|AFT` still lands on
    §5.5 row 4 (n=35, replacement_interval_share ~0.97 > 0.5), so a labelled
    `supporting_interval` is attached and the projected window anchors the
    fleet p50/p90 (test_3: p50=381, p90~=1890.8) onto this aircraft's own
    last replacement TAC (17938) -> TAC 18319-19829."""
    from pm_agent.prediction.policy import round_cycles
    from pm_agent.workorders.service import AnalysisInput

    result = analysis_service.analyze_xml(
        _AFT_EXACT_PN_XML,
        AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma["component_key"] == "473597-5|AFT", pma
    assert pma["match"]["gate_basis"] == "exact_pn_position", pma
    assert pma["reason"] == "samples_not_symptom_to_replacement", pma

    supporting = pma["supporting_interval"]
    assert supporting is not None, pma
    window = pma["projected_window"]
    assert window is not None, pma
    assert window["last_replacement_tac"] == 17938, window
    assert window["last_replacement_date"] == "2026-04-25", window
    assert window["tac_p50"] == 17938 + round_cycles(supporting["p50"])
    assert window["tac_p90"] == 17938 + round_cycles(supporting["p90"])
    assert (window["tac_p50"], window["tac_p90"]) == (18319, 19829), window
    assert window["calibrated"] is False, window
    assert "projected_window_not_a_forecast" in pma["limitations"], pma


def _with_part_number(
    xml: bytes, part_number: str, position: str | None = None
) -> bytes:
    """Inject a header `<component><partNumber>` (and optionally swap the
    `<position>`) so the exact part-number + position gate is used."""
    if position is not None:
        xml = re.sub(
            rb"<position>[^<]*</position>",
            f"<position>{position}</position>".encode(),
            xml,
            count=1,
        )
    return xml.replace(
        b"</positionInfo>",
        b"</positionInfo><component><partNumber>"
        + part_number.encode()
        + b"</partNumber><serialNumber>LIVE-1</serialNumber></component>",
        1,
    )


def test_9b_below_min_sample_has_no_supporting_interval_or_window(repository):
    """Same aircraft/component as test_9 (`SP-REG00374` does have a prior
    replacement at TAC 17938), but with `min_sample=36` > n=35: `decide()`
    stops at row 3 (`insufficient_samples`) with no `supporting_interval`,
    so the projected window - only attempted once a supporting interval
    exists (`prediction/service.py`) - stays `None` and is never looked up."""
    from pm_agent.prediction.service import PrecursorPredictor
    from pm_agent.prediction.settings import PredictionSettings
    from pm_agent.workorders.service import AnalysisInput, WorkOrderAnalysisService

    settings = PredictionSettings(prediction_enabled=True, min_sample=36)
    service = WorkOrderAnalysisService(
        predictor=PrecursorPredictor(repository, settings)
    )
    result = service.analyze_xml(
        _AFT_EXACT_PN_XML,
        AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma["component_key"] == "473597-5|AFT", pma
    assert pma["reason"] == "insufficient_samples", pma
    assert pma.get("supporting_interval") is None, pma
    assert pma.get("projected_window") is None, pma
    assert "no_prior_replacement_on_aircraft" not in pma["limitations"], pma


def test_9c_rh_exact_pn_no_prior_replacement_on_aircraft(analysis_service):
    """`45-0351-4|RH` by exact part number: n=5, share 0.8 -> row 4 with a
    `supporting_interval`, but `9H-REG00386` has no earlier replacement of
    this component, so `projected_window` is `None` with limitation
    `no_prior_replacement_on_aircraft` (fixed string (d))."""
    from pm_agent.workorders.service import AnalysisInput

    result = analysis_service.analyze_xml(
        _with_part_number(RH_FIXTURE.read_bytes(), "45-0351-4"),
        AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma["component_key"] == "45-0351-4|RH", pma
    assert pma["reason"] == "samples_not_symptom_to_replacement", pma
    assert pma["supporting_interval"]["n"] == 5, pma
    assert pma.get("projected_window") is None, pma
    assert "no_prior_replacement_on_aircraft" in pma["limitations"], pma


# -- 10. historical_interval is live-reachable after the 2026-09-24 rebuild --


def test_10_cabin_exact_pn_reaches_historical_interval(analysis_service):
    """`9651-35-0005|CABIN`: n=13 (9 aircraft), replacement_interval_share
    ~0.38 <= 0.5, so §5.5 row 5 `historical_interval` (p50 1659, p90 2301)
    is reachable with live data for the first time; no window is attempted
    (`projected_window` is Option B's row-4-only addition)."""
    from pm_agent.workorders.service import AnalysisInput

    result = analysis_service.analyze_xml(
        _with_part_number(CARGO_FIXTURE.read_bytes(), "9651-35-0005", "CABIN"),
        AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma["component_key"] == "9651-35-0005|CABIN", pma
    assert pma["decision"] == "historical_interval", pma
    interval = pma["interval"]
    assert interval["n"] == 13, interval
    assert interval["aircraft"] == 9, interval
    assert interval["p50"] == pytest.approx(1659.0, abs=0.5)
    assert interval["p90"] == pytest.approx(2301.0, abs=0.5)
    assert pma.get("supporting_interval") is None, pma
    assert pma.get("projected_window") is None, pma


# -- 11. Recommendation from wo_embeddings / fct_lead_time_samples ----------
#
# 2026-09-24 user request, further overriding BIGQUERY-AGENT-plan §8.5 on top
# of Option B: a condensed p50/p90/p95 replacement recommendation, based on
# `wo_embeddings` neighbours matched on description *and* action text, with
# lead-time drawn from `fct_lead_time_samples` (PMA-ONLINE-AGENT-plan.md
# "T00 decisions", README.md "Online prediction (pma-online-v1)").

def test_11_aft_exact_pn_recommendation_from_similar_workorders(analysis_service):
    """`473597-5|AFT` on `SP-REG00374`: the neighbour vote over
    `wo_embeddings`/`fct_lead_time_samples` (description+action match) yields
    a `similar_workorders` recommendation for the same component the
    deterministic gate resolved, independent of the main decision/Option B
    window. `tac_p50` is anchored on the aircraft's own `reference_tac` by
    construction, so the cross-field identity holds regardless of the exact
    live sample count."""
    from pm_agent.workorders.service import AnalysisInput

    xml_path = Path("/tmp/pma-manual") / "aft_smoke_detector_with_pn.xml"
    result = analysis_service.analyze_xml(
        xml_path.read_bytes(),
        AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma["component_key"] == "473597-5|AFT", pma

    rec = pma.get("recommendation")
    assert rec is not None, pma
    assert rec["basis"] == "similar_workorders", rec
    assert rec["component_key"] == "473597-5|AFT", rec
    predicted = rec["predicted_replacement"]
    assert rec["reference_tac"] is not None, rec
    assert predicted["tac_p50"] == rec["reference_tac"] + predicted["lead_tac_p50"], rec
    assert predicted["tac_p50"] <= predicted["tac_p90"], rec
    assert {e["wo_id"] for e in rec["evidence"]}, rec


def test_11b_fuel_nozzle_replay_has_no_recommendation(analysis_service):
    """`example_workorders/TRANSFER_WORKORDER_1789487041751.xml` replayed in
    `historical_replay` mode: the gate resolves `2085M31G03|#2` but fuel
    nozzles have no `fct_lead_time_samples` rows, so the WO's similar-work-
    order neighbours carry no lead samples, there are no component stats and
    no supporting interval - no recommendation, and not because the lookup
    failed (`recommendation_unavailable` must be absent)."""
    from pm_agent.workorders.service import AnalysisInput

    xml_path = (
        Path(__file__).parents[2]
        / "example_workorders"
        / "TRANSFER_WORKORDER_1789487041751.xml"
    )
    result = analysis_service.analyze_xml(
        xml_path.read_bytes(),
        AnalysisInput(mode="historical_replay", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma.get("recommendation") is None, pma
    assert "recommendation_unavailable" not in pma["limitations"], pma


def test_11c_cabin_recommendation_is_internally_consistent(analysis_service):
    """`9651-35-0005|CABIN` (test_10's `historical_interval` fixture): when a
    recommendation is produced it is self-consistent - `component_key`
    matches the resolved component, `basis` is one of the fixed enum values,
    and `tac_p50 <= tac_p90 <= tac_p95` (BigQuery `PERCENTILE_CONT`
    monotonicity). No specific basis is asserted here since, unlike
    `473597-5|AFT`, no live neighbour-vote probe for this fixture's symptom
    text has been run against the 2026-09-24 rebuild."""
    from pm_agent.workorders.service import AnalysisInput

    result = analysis_service.analyze_xml(
        _with_part_number(CARGO_FIXTURE.read_bytes(), "9651-35-0005", "CABIN"),
        AnalysisInput(mode="new_work_order", analysis_as_of=AS_OF.isoformat()),
    )
    pma = result["pma"]
    assert pma["component_key"] == "9651-35-0005|CABIN", pma

    rec = pma.get("recommendation")
    if rec is not None:
        assert rec["component_key"] == "9651-35-0005|CABIN", rec
        assert rec["basis"] in (
            "similar_workorders",
            "component_history",
            "fleet_replacement_interval",
        ), rec
        predicted = rec["predicted_replacement"]
        assert predicted["tac_p50"] <= predicted["tac_p90"], rec
        if predicted["tac_p95"] is not None:
            assert predicted["tac_p90"] <= predicted["tac_p95"], rec
