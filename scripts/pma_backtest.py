#!/usr/bin/env python3
"""PMA anchor-vote backtest (plan §8.3).

Standalone, read-only research script - **not** part of the served agent.
It measures how ``policy.vote_component`` + ``policy.resolve_scope`` (the
exact production decision functions in ``pm_agent.prediction.policy``)
would have gated a set of symptom-only work-order queries against the
pre-cutoff anchor set, across a grid of similarity thresholds and vote
normalisations, so a threshold can be chosen with an explicit false-gate
budget instead of by guesswork.

Everything here is in-sample: positives/negatives/anchors are all mined
from the same curated artifacts the production gate itself was built from
(no held-out split exists yet). Every reported number is annotated with
that caveat - see the plan's §8.3 for the full spec this implements.

Data sources (all read via a single, unmodified ``bigquery.Client`` -
``v_symptom_records``/ad-hoc joins used here are not on
``QueryRunner``'s allowlist, so this script never goes through the
runtime's evidence-tool surface; every query is a plain ``SELECT``, no
DDL/DML, small scans only):

* Anchors: ``replacement_anchor_embeddings`` joined to ``dim_reference_set``,
  restricted to ``replacement_date < --as-of`` (mirrors
  ``sql/pma_anchor_neighbours.sql``'s own dedup/filter shape, minus its
  ``sim >= min_sim`` clause since this script needs every pre-cutoff anchor
  to build the full similarity matrix itself).
* Positives:
    - the 28 curated ``fct_lead_time_samples`` precursor work orders, and
    - every focus-component replacement work order (``dim_reference_set``),
  both restricted to their own event date >= --as-of (so a query's anchor
  can never also be a member of the pre-cutoff anchor set it is compared
  against - the two restrictions are complementary by construction for the
  replacement-WO positives, since both use the same ``replacement_date``
  field).
* Negatives: a seeded random sample of non-focus ``wo_embeddings`` work
  orders (via ``wo_workorders.issue.date >= --as-of``).

Query text for every positive/negative is *not* the WO's stored anchor/
wo_embeddings ``content`` - it is rebuilt symptom-only (no completed
``actions``) with ``pm_agent.workorders.service.build_pma_wo_text(row,
include_actions=False)``, the codebase's own canonical "remarks + step
descriptions" recipe (see that function's docstring; ``curated.tf``
mirrors it for the anchor/wo_embeddings pipelines). The plan's §8.3 says
this text should come from ``v_symptom_records``; that view has no
``remarks`` field of its own (only step-level ``description``), so this
script instead applies the already-implemented, already-tested recipe to
``wo_workorders`` rows directly - a deliberate, documented deviation (see
also this repo's T14 hand-off notes).

Similarity is plain cosine similarity between L2-normalised embedding
vectors (``ML.DISTANCE(..., 'COSINE')`` is ``1 - cosine_similarity``,
matching what ``anchor_neighbours`` computes live). This script requires
``numpy`` (present in the resolved environment, ``uv.lock``, though not a
direct ``[project.dependencies]`` entry - this script cannot add one
without touching files outside its ownership) to keep the ~1--1.5k
anchors x ~1k queries x 768-dim matrix multiply fast; it fails fast with a
clear message if ``numpy`` is unavailable.

Usage::

    uv run python scripts/pma_backtest.py --as-of 2026-03-01
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from dataclasses import dataclass, replace
from datetime import date

try:
    import numpy as np
except ImportError:  # pragma: no cover - environment guard, not test surface
    print(
        "ERROR: this script requires numpy (present in this project's "
        "uv.lock as a transitive dependency; run via `uv run python "
        "scripts/pma_backtest.py ...`).",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

from google.cloud import bigquery

from pm_agent.prediction.contracts import FocusComponent, Neighbour, PredictionReason
from pm_agent.prediction.policy import resolve_scope, vote_component
from pm_agent.prediction.repository import build_default_repository
from pm_agent.prediction.settings import PredictionSettings
from pm_agent.workorders.service import build_pma_wo_text

CURATED_DATASET = "pma_agent_curated"
ANALYTICS_DATASET = "pma_agent_analytics"
EMBEDDING_ENDPOINT = "text-embedding-005"
EMBEDDING_DIM = 768
EMBED_BATCH_SIZE = 1000  # plan §8.3: batched AI.EMBED, <=1000 texts/call.


@dataclass(frozen=True)
class AnchorRow:
    wo_uuid: str
    component_key: str
    aircraft_reg: str | None
    replacement_date: date | None
    emb: list[float]


@dataclass(frozen=True)
class QueryRow:
    wo_uuid: str
    source: str  # "precursor" | "replacement" | "negative"
    true_component_key: str | None  # None for negatives
    query_date: date | None
    text: str


@dataclass
class EmbeddedQuery:
    row: QueryRow
    emb: list[float] | None  # None if embedding failed/dropped


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--as-of",
        required=True,
        help="Cutoff date (YYYY-MM-DD). Anchors use replacement_date < as-of; "
        "query candidates use their own event date >= as-of.",
    )
    p.add_argument(
        "--project",
        default=None,
        help="GCP project (default: ADC/pm_agent.config.project_id())",
    )
    p.add_argument(
        "--thresholds",
        default="0.75,0.80,0.85,0.90",
        help="Comma-separated cosine-similarity thresholds to evaluate.",
    )
    p.add_argument(
        "--normalisations",
        default="none,sqrt",
        help="Comma-separated vote normalisations to evaluate (none, sqrt).",
    )
    p.add_argument(
        "--num-negatives",
        type=int,
        default=500,
        help="Number of negative (non-focus) queries to sample (plan default: 500).",
    )
    p.add_argument(
        "--seed", type=int, default=42, help="RNG seed for negative sampling."
    )
    p.add_argument(
        "--anchor-k",
        type=int,
        default=None,
        help="Max anchors per vote (default: PredictionSettings.anchor_k = 20).",
    )
    p.add_argument(
        "--min-vote-support",
        type=int,
        default=None,
        help="Default: PredictionSettings.min_vote_support = 3.",
    )
    p.add_argument(
        "--min-vote-share",
        type=float,
        default=None,
        help="Default: PredictionSettings.min_vote_share = 0.5.",
    )
    p.add_argument(
        "--false-gate-budget",
        type=float,
        default=0.05,
        help="Recommend the lowest threshold whose negative false-gate rate is "
        "<= this fraction (plan default: 5%%).",
    )
    return p.parse_args(argv)


def _l2_normalise(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def fetch_anchors(client: bigquery.Client, project: str, as_of: str) -> list[AnchorRow]:
    """Mirrors ``sql/pma_anchor_neighbours.sql``'s dedup/filter shape, minus
    the ``sim >= min_sim`` clause (every pre-cutoff anchor is needed here)."""
    sql = f"""
    WITH anchors AS (
      SELECT
        a.replacement_wo_uuid,
        ANY_VALUE(a.component_key)           AS component_key,
        ANY_VALUE(a.aircraft_reg)            AS aircraft_reg,
        ANY_VALUE(a.anchor_embedding.result) AS emb
      FROM `{project}.{CURATED_DATASET}.replacement_anchor_embeddings` a
      WHERE a.anchor_embedding.status = ''
        AND ARRAY_LENGTH(a.anchor_embedding.result) = {EMBEDDING_DIM}
      GROUP BY a.replacement_wo_uuid
    ),
    dated AS (
      SELECT replacement_wo_uuid, MIN(replacement_date) AS replacement_date
      FROM `{project}.{CURATED_DATASET}.dim_reference_set`
      GROUP BY replacement_wo_uuid
    )
    SELECT x.replacement_wo_uuid, x.component_key, x.aircraft_reg, x.emb,
           d.replacement_date
    FROM anchors x
    JOIN dated d USING (replacement_wo_uuid)
    WHERE d.replacement_date < DATE(@as_of)
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("as_of", "DATE", as_of)]
        ),
    )
    return [
        AnchorRow(
            wo_uuid=row["replacement_wo_uuid"],
            component_key=row["component_key"],
            aircraft_reg=row.get("aircraft_reg"),
            replacement_date=row.get("replacement_date"),
            emb=list(row["emb"]),
        )
        for row in job.result()
    ]


def fetch_precursor_positives(
    client: bigquery.Client, project: str, as_of: str
) -> tuple[list[QueryRow], int]:
    """The 28 curated lead-time-sample precursor WOs (plan §8.3), text
    rebuilt symptom-only from their own ``wo_workorders`` row, restricted to
    their own event date >= --as-of."""
    sql = f"""
    SELECT DISTINCT
      s.precursor_wo_uuid AS wo_uuid,
      s.component_key,
      w.remarks,
      w.work_steps,
      COALESCE(w.issue.date, w.closing.date) AS query_date
    FROM `{project}.{CURATED_DATASET}.fct_lead_time_samples` s
    JOIN `{project}.{ANALYTICS_DATASET}.wo_workorders` w
      ON w.workorder_uuid = s.precursor_wo_uuid
    WHERE COALESCE(w.issue.date, w.closing.date) >= DATE(@as_of)
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("as_of", "DATE", as_of)]
        ),
    )
    rows = list(job.result())
    out = []
    for row in rows:
        d = dict(row)
        text = build_pma_wo_text(d, include_actions=False).strip()
        if not text:
            continue
        out.append(
            QueryRow(
                wo_uuid=row["wo_uuid"],
                source="precursor",
                true_component_key=row["component_key"],
                query_date=row.get("query_date"),
                text=text,
            )
        )
    return out, len(rows)


def fetch_replacement_positives(
    client: bigquery.Client, project: str, as_of: str
) -> list[QueryRow]:
    """Every focus-component replacement WO whose own ``replacement_date``
    is >= --as-of (mutually exclusive with the anchor set's
    ``replacement_date < as-of`` filter by construction)."""
    sql = f"""
    SELECT DISTINCT
      d.replacement_wo_uuid AS wo_uuid,
      d.component_key,
      d.replacement_date,
      w.remarks,
      w.work_steps
    FROM `{project}.{CURATED_DATASET}.dim_reference_set` d
    JOIN `{project}.{ANALYTICS_DATASET}.wo_workorders` w
      ON w.workorder_uuid = d.replacement_wo_uuid
    WHERE d.replacement_date >= DATE(@as_of)
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("as_of", "DATE", as_of)]
        ),
    )
    out = []
    for row in job.result():
        d = dict(row)
        text = build_pma_wo_text(d, include_actions=False).strip()
        if not text:
            continue
        out.append(
            QueryRow(
                wo_uuid=row["wo_uuid"],
                source="replacement",
                true_component_key=row["component_key"],
                query_date=row.get("replacement_date"),
                text=text,
            )
        )
    return out


def fetch_negative_candidates(
    client: bigquery.Client, project: str, as_of: str
) -> list[QueryRow]:
    """Non-focus ``wo_embeddings`` WOs with issue date >= --as-of (the full
    candidate pool; sampling down to ``--num-negatives`` happens in Python
    with a seeded RNG so the sample is reproducible without relying on
    BigQuery's own RNG)."""
    sql = f"""
    SELECT DISTINCT
      e.wo_uuid,
      w.remarks,
      w.work_steps,
      w.issue.date AS query_date
    FROM `{project}.{CURATED_DATASET}.wo_embeddings` e
    JOIN `{project}.{ANALYTICS_DATASET}.wo_workorders` w
      ON w.workorder_uuid = e.wo_uuid
    LEFT JOIN `{project}.{CURATED_DATASET}.dim_reference_set` d
      ON d.replacement_wo_uuid = e.wo_uuid
    WHERE d.replacement_wo_uuid IS NULL
      AND w.issue.date >= DATE(@as_of)
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("as_of", "DATE", as_of)]
        ),
    )
    out = []
    for row in job.result():
        d = dict(row)
        text = build_pma_wo_text(d, include_actions=False).strip()
        if not text:
            continue
        out.append(
            QueryRow(
                wo_uuid=row["wo_uuid"],
                source="negative",
                true_component_key=None,
                query_date=row.get("query_date"),
                text=text,
            )
        )
    return out


def embed_texts(
    client: bigquery.Client, project: str, texts: list[str]
) -> list[list[float] | None]:
    """Batched ``AI.EMBED`` (plan §8.3: <=1000 texts / <=100MB per call).
    Returns one embedding per input text, in order; ``None`` for any text
    whose embedding failed or had the wrong dimension."""
    results: list[list[float] | None] = [None] * len(texts)
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        sql = f"""
        SELECT idx, embedding.result AS emb, embedding.status AS status
        FROM UNNEST(@texts) AS txt WITH OFFSET idx,
        UNNEST([AI.EMBED(txt, endpoint => '{EMBEDDING_ENDPOINT}')]) AS embedding
        """
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("texts", "STRING", batch)
                ]
            ),
        )
        for row in job.result():
            idx = start + row["idx"]
            status = row.get("status") or ""
            emb = list(row.get("emb") or [])
            if status == "" and len(emb) == EMBEDDING_DIM:
                results[idx] = emb
    return results


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    normalisations = [n.strip() for n in args.normalisations.split(",") if n.strip()]

    settings = PredictionSettings.from_env()
    if args.project:
        settings = replace(settings, bq_project=args.project)
    anchor_k = args.anchor_k if args.anchor_k is not None else settings.anchor_k
    min_vote_support = (
        args.min_vote_support
        if args.min_vote_support is not None
        else settings.min_vote_support
    )
    min_vote_share = (
        args.min_vote_share
        if args.min_vote_share is not None
        else settings.min_vote_share
    )

    repo = build_default_repository(settings)
    project = repo._runner.project
    client = bigquery.Client(project=project)

    print(f"# PMA anchor-vote backtest  (as_of={args.as_of}, project={project})")
    print(
        "# All numbers below are IN-SAMPLE: curated artifacts mined without a split.\n"
    )

    t0 = time.monotonic()
    focus_components: list[FocusComponent] = repo.focus_components()
    print(f"focus_components: {len(focus_components)} rows")

    anchors = fetch_anchors(client, project, args.as_of)
    print(f"anchors (replacement_date < {args.as_of}): {len(anchors)}")

    precursor_positives, precursor_total = fetch_precursor_positives(
        client, project, args.as_of
    )
    print(
        f"precursor positives: kept {len(precursor_positives)} of {precursor_total} "
        f"curated lead-time-sample precursors after the issue_date/closing_date >= "
        f"{args.as_of} filter"
    )

    replacement_positives = fetch_replacement_positives(client, project, args.as_of)
    print(f"replacement positives: {len(replacement_positives)}")

    negative_pool = fetch_negative_candidates(client, project, args.as_of)
    rng = random.Random(args.seed)
    negatives = (
        rng.sample(negative_pool, args.num_negatives)
        if len(negative_pool) > args.num_negatives
        else negative_pool
    )
    print(
        f"negative candidates: {len(negative_pool)} available, "
        f"sampled {len(negatives)} (seed={args.seed})"
    )

    all_queries: list[QueryRow] = (
        precursor_positives + replacement_positives + negatives
    )
    print(f"total queries to embed: {len(all_queries)}\n")

    if not anchors or not all_queries:
        print("Not enough data to run the backtest at this --as-of value.")
        return 1

    print("Embedding anchors...")
    anchor_embs = np.array([a.emb for a in anchors], dtype=np.float64)
    anchor_embs = _l2_normalise(anchor_embs)

    print("Embedding queries (batched AI.EMBED)...")
    query_embed_start = time.monotonic()
    embeddings = embed_texts(client, project, [q.text for q in all_queries])
    query_embed_elapsed = time.monotonic() - query_embed_start

    embedded_queries: list[EmbeddedQuery] = []
    dropped = 0
    for q, emb in zip(all_queries, embeddings, strict=True):
        if emb is None:
            dropped += 1
            continue
        embedded_queries.append(EmbeddedQuery(row=q, emb=emb))
    if dropped:
        print(f"WARNING: dropped {dropped} queries with failed/malformed embeddings")

    query_mat = np.array([eq.emb for eq in embedded_queries], dtype=np.float64)
    query_mat = _l2_normalise(query_mat)

    print(
        f"embedded {len(embedded_queries)} queries in {query_embed_elapsed:.1f}s "
        f"({1000.0 * query_embed_elapsed / max(len(embedded_queries), 1):.0f} ms/query, "
        f"batched)\n"
    )

    # Full similarity matrix: queries x anchors.
    sim_matrix = query_mat @ anchor_embs.T  # (n_queries, n_anchors)

    # Similarity distribution for positives (max sim to any pre-cutoff
    # anchor), independent of threshold - feeds the threshold discussion.
    positive_idx = [
        i for i, eq in enumerate(embedded_queries) if eq.row.source != "negative"
    ]
    if positive_idx:
        max_sims = sim_matrix[positive_idx].max(axis=1)
        sorted_sims = sorted(max_sims.tolist())
        print("Similarity distribution, positives (max sim to any pre-cutoff anchor):")
        print(
            f"  n={len(sorted_sims)} min={sorted_sims[0]:.3f} "
            f"p50={statistics.median(sorted_sims):.3f} "
            f"p90={sorted_sims[int(0.9 * (len(sorted_sims) - 1))]:.3f} "
            f"mean={statistics.fmean(sorted_sims):.3f} max={sorted_sims[-1]:.3f}"
        )
        print("  (in-sample: curated artifacts mined without a split)\n")

    latencies_ms: list[float] = []
    best_reco: tuple[float, str, float] | None = None  # (threshold, norm, false_gate)

    for normalisation in normalisations:
        for threshold in thresholds:
            n_neg = 0
            n_false_gate = 0
            n_ambiguous_neg = 0
            per_key_tp: dict[str, int] = {}
            per_key_fp: dict[str, int] = {}
            per_key_total_true: dict[str, int] = {}
            n_pos = 0
            n_pos_gate_passed_correct = 0
            n_pos_gate_passed_any = 0
            n_pos_ambiguous = 0
            n_pos_no_match = 0
            n_neg_no_match = 0

            for idx, eq in enumerate(embedded_queries):
                t_start = time.monotonic()
                row_sims = sim_matrix[idx]
                candidate_idx = np.where(row_sims >= threshold)[0]
                if candidate_idx.size:
                    order = candidate_idx[np.argsort(-row_sims[candidate_idx])][
                        :anchor_k
                    ]
                else:
                    order = candidate_idx
                neighbours = [
                    Neighbour(
                        wo_uuid=anchors[j].wo_uuid,
                        wo_id=None,
                        aircraft_reg=anchors[j].aircraft_reg,
                        sim=float(row_sims[j]),
                        snippet="",
                        component_key=anchors[j].component_key,
                    )
                    for j in order
                    if anchors[j].wo_uuid != eq.row.wo_uuid
                ]
                vote = vote_component(
                    neighbours,
                    min_support=min_vote_support,
                    min_share=min_vote_share,
                    normalisation=normalisation,
                )
                gate = resolve_scope(
                    part_numbers=(),
                    position=None,
                    header_part_number=None,
                    focus_components=focus_components,
                    vote=vote,
                )
                latencies_ms.append((time.monotonic() - t_start) * 1000.0)

                is_ambiguous = gate.reason == PredictionReason.AMBIGUOUS_POSITION
                is_no_match = (
                    gate.reason == PredictionReason.NO_CONFIDENT_COMPONENT_MATCH
                )

                if eq.row.source == "negative":
                    n_neg += 1
                    if gate.passed:
                        n_false_gate += 1
                    elif is_ambiguous:
                        n_ambiguous_neg += 1
                    elif is_no_match:
                        n_neg_no_match += 1
                else:
                    n_pos += 1
                    true_key = eq.row.true_component_key
                    per_key_total_true[true_key] = (
                        per_key_total_true.get(true_key, 0) + 1
                    )
                    if gate.passed:
                        n_pos_gate_passed_any += 1
                        predicted_key = gate.component_key
                        if predicted_key == true_key:
                            n_pos_gate_passed_correct += 1
                            per_key_tp[true_key] = per_key_tp.get(true_key, 0) + 1
                        else:
                            per_key_fp[predicted_key] = (
                                per_key_fp.get(predicted_key, 0) + 1
                            )
                    elif is_ambiguous:
                        n_pos_ambiguous += 1
                    elif is_no_match:
                        n_pos_no_match += 1

            false_gate_rate = n_false_gate / n_neg if n_neg else float("nan")
            coverage = n_pos_gate_passed_any / n_pos if n_pos else float("nan")
            recall_correct = (
                n_pos_gate_passed_correct / n_pos if n_pos else float("nan")
            )
            pos_ambiguous_rate = n_pos_ambiguous / n_pos if n_pos else float("nan")
            pos_no_match_rate = n_pos_no_match / n_pos if n_pos else float("nan")

            print(
                f"--- normalisation={normalisation} threshold={threshold:.2f} "
                f"(in-sample: curated artifacts mined without a split) ---"
            )
            print(
                f"  negatives: n={n_neg} false_gate_rate={false_gate_rate:.3%} "
                f"ambiguous_rate={n_ambiguous_neg / n_neg if n_neg else float('nan'):.3%} "
                f"no_match_rate={n_neg_no_match / n_neg if n_neg else float('nan'):.3%}"
            )
            print(
                f"  positives: n={n_pos} coverage(any_key)={coverage:.3%} "
                f"recall(correct_key)={recall_correct:.3%} "
                f"ambiguous_position_rate={pos_ambiguous_rate:.3%} "
                f"no_prediction_rate={pos_no_match_rate:.3%}"
            )
            for key in sorted(per_key_total_true):
                total_true = per_key_total_true[key]
                tp = per_key_tp.get(key, 0)
                fp = per_key_fp.get(key, 0)
                precision = tp / (tp + fp) if (tp + fp) else float("nan")
                recall = tp / total_true if total_true else float("nan")
                flag = " <- #1/#2 sibling key" if "|#1" in key or "|#2" in key else ""
                print(
                    f"    {key}: n_true={total_true} tp={tp} fp={fp} "
                    f"precision={precision:.3f} recall={recall:.3f}{flag}"
                )
            print()

            if false_gate_rate <= args.false_gate_budget:
                if best_reco is None or threshold < best_reco[0]:
                    best_reco = (threshold, normalisation, false_gate_rate)

    if latencies_ms:
        sorted_lat = sorted(latencies_ms)
        p95 = sorted_lat[int(0.95 * (len(sorted_lat) - 1))]
        print(
            f"vote+gate latency (pure-Python decision logic only, excludes network/embedding): "
            f"n={len(sorted_lat)} p50={statistics.median(sorted_lat):.2f}ms "
            f"p95={p95:.2f}ms max={sorted_lat[-1]:.2f}ms"
        )
    print(
        f"end-to-end query embedding: {len(embedded_queries)} texts in "
        f"{query_embed_elapsed:.1f}s total "
        f"({1000.0 * query_embed_elapsed / max(len(embedded_queries), 1):.1f} ms/query amortised "
        f"over one batched AI.EMBED call - NOT representative of live single-query "
        f"embed_query() latency, which issues one AI.EMBED per request)."
    )

    print()
    if best_reco is not None:
        threshold, normalisation, false_gate_rate = best_reco
        print(
            f"RECOMMENDATION: anchor_sim_threshold={threshold:.2f} "
            f"normalisation={normalisation} "
            f"(lowest threshold with false-gate rate <= {args.false_gate_budget:.0%}; "
            f"measured false-gate rate={false_gate_rate:.3%}). "
            f"In-sample: curated artifacts mined without a split."
        )
    else:
        print(
            f"RECOMMENDATION: no evaluated threshold/normalisation combination reached "
            f"a false-gate rate <= {args.false_gate_budget:.0%} on this negative sample "
            f"(n={args.num_negatives}, seed={args.seed}). Consider raising thresholds "
            f"further or tightening min_vote_support/min_vote_share."
        )

    print(f"\nTotal wall time: {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
