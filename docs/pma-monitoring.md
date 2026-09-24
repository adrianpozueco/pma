# PMA online prediction: monitoring and the go-live data gate

This document covers the two operational pieces `PMA-ONLINE-AGENT-plan.md`
assigns to T18: the Cloud Logging queries required by §8.4 item 9
(prediction volume, no-prediction rate by reason, decision mix, latency),
and the read-only go-live data gate required by §8.4 item 11
(`scripts/pma_data_gate.py`). It is written to be merged into README.md's
"Online prediction (pma-online-v1)" section by T16; nothing here assumes a
standalone doc.

> Merged: the operational summary (log shape, Logs Explorer queries, data
> gate, reviewer workflow) now lives in README.md → "Online prediction
> (pma-online-v1)". This file keeps the long form and the forward-looking
> BigQuery SQL (§3, pending T19).

## 1. What gets logged

Every call to the online predictor (`pm_agent/prediction/service.py`) emits
one structured Cloud Logging entry through the `pma-agent` logger, whether
or not a prediction was produced. The entry's `jsonPayload` top level has:

| Field | Contents |
|---|---|
| `request_id` | Per-call correlation id |
| `wo_id` | Work order id (human-readable, not the BigQuery `wo_uuid`) |
| `wo_text_sha256` | Hash of the input text (no raw WO text is logged) |
| `pma` | The full `PredictionResult.to_dict()` - `decision`, `reason`, `component_key`, `interval`, `evidence_support`, etc. (§6.2/§6.3) |
| `table_creation_times` | The curated build's per-table creation timestamps at call time (§5.6) |
| `embedding_endpoint` | e.g. `text-embedding-005` |
| `settings_hash` | Hash of the effective `PredictionSettings`, so a threshold change is visible in the log stream without redeploying |
| `bq_job_ids` | BigQuery job ids issued for this call |
| `total_bytes_billed` | Summed billed bytes across those jobs (ties to the 200 MB/call acceptance check, §8.2 item 7) |
| `latency_ms` | Wall-clock time for the whole `predict()` call |
| `labels` | See below |

Every entry carries these labels (set on the `LogEntry`, not nested in the
payload, so they are directly filterable in Logs Explorer and cheap to
index on):

```
labels.type          = "agent_telemetry"
labels.service_name  = "pma-agent"
labels.event         = "pma_prediction"
```

`pma.decision` is one of `historical_interval`, `no_reliable_prediction`,
`out_of_scope`; `pma.reason` is set on the two `no_*` decisions and is one
of the `PredictionReason` values (`embedding_failed`,
`no_confident_component_match`, `insufficient_samples`,
`data_source_unavailable`, `prediction_disabled`, ...; full enum in
`pm_agent/prediction/contracts.py`).

### Known gap: no BigQuery-backed log sink yet (R14 / OQ11 / T19)

The repo's only Terraform log sink today
(`google_logging_project_sink.genai_logs_to_bq` in
`deployment/terraform/single-project/telemetry.tf`) filters on
`labels."event.name"="gen_ai.client.inference.operation.details"` - the
GenAI-inference telemetry event, not this one. It does **not** capture
`labels.type="agent_telemetry"` PMA prediction records. OQ11's default is
"no BigQuery writes by the agent; predictions go to the log sink only", and
a dedicated sink/table for this event is deferred to T19 (out of scope for
this task).

Practically: every query in §2 below that reads directly from **Cloud
Logging** (Logs Explorer filters, `gcloud logging read`) works today. The
Log Analytics / BigQuery SQL variants in §3 are forward-looking and will
only return rows once T19 stands up that sink - they are included so the
monitoring plan does not have to be rewritten when that lands, and each is
marked **(pending T19)**.

## 2. Logs Explorer / `gcloud logging read` queries (work today)

Run these in Cloud Console -> Logging -> Logs Explorer, or via
`gcloud logging read '<filter>' --project=<PROJECT> --freshness=1d --format=json`.
None of these write anything; they are plain reads against the existing log
stream.

### 2.1 Prediction volume

Total predictor calls in a window:

```
labels.type="agent_telemetry"
labels.service_name="pma-agent"
labels.event="pma_prediction"
timestamp>="2026-09-23T00:00:00Z"
```

Count the matching entries (Logs Explorer's histogram, or
`gcloud logging read '...' --format=json | jq length`) for calls/hour or
calls/day. Split by `jsonPayload.mode` (if present in `pma.mode`, e.g.
`live` vs `replay`) to separate backtest traffic from production traffic.

### 2.2 No-prediction rate, by reason

Calls that did **not** produce a `historical_interval`:

```
labels.type="agent_telemetry"
labels.event="pma_prediction"
jsonPayload.pma.decision="no_reliable_prediction"
```

Add `jsonPayload.pma.reason="<value>"` to break out one reason (e.g.
`insufficient_samples`, `embedding_failed`, `data_source_unavailable`,
`no_confident_component_match`). Rate = count of this filter / count of
§2.1's filter over the same window. A rising
`data_source_unavailable` share is the leading indicator that the curated
build has drifted since the last `pma_data_gate.py` run (§4) and the gate
should be re-run before trusting further predictions.

### 2.3 Decision mix

Three-way split, one query per value of `jsonPayload.pma.decision`:

```
labels.type="agent_telemetry"
labels.event="pma_prediction"
jsonPayload.pma.decision="historical_interval"
```

(swap in `no_reliable_prediction` / `out_of_scope`). Compare relative
shares over time; a sudden shift (e.g. `out_of_scope` jumping) usually
means the focus-component set or an upstream part-number normalisation
changed, not that the data gate failed.

### 2.4 Latency

```
labels.type="agent_telemetry"
labels.event="pma_prediction"
jsonPayload.latency_ms>2000
```

lists slow calls directly (adjust the threshold). For percentiles, export
`jsonPayload.latency_ms` from a `gcloud logging read ... --format=json` pull
into `jq`/a notebook, or use a Log-based Metric (Cloud Console -> Logging ->
Log-based metrics -> create a distribution metric on `jsonPayload.latency_ms`
scoped to `labels.event="pma_prediction"`) and view its p50/p90 in Cloud
Monitoring - this is a metric-creation step, not a data mutation, and is
the recommended way to get a durable p50/p90 chart without waiting on T19.
Cross-reference `jsonPayload.total_bytes_billed` and
`jsonPayload.bq_job_ids` on the same entries when a call is slow, to tell
"slow BigQuery" from "slow embedding" apart.

## 3. Log Analytics / BigQuery SQL (pending T19's sink)

Once a dedicated sink lands (T19) with the PMA prediction logs routed into
a BigQuery table (proposed shape: one row per log entry, `jsonPayload.*`
flattened or kept as a `JSON`/`STRUCT` column, `labels` as a
`STRUCT<type STRING, service_name STRING, event STRING>` or a
`labels.<key>` pseudo-column the way Log Analytics exposes them today), the
same four views become plain `SELECT`s. Sketches below use `t` as a
placeholder for that future table; column names will need to match
whatever T19 actually ships.

```sql
-- 3.1 Prediction volume by day
SELECT DATE(timestamp) AS day, COUNT(*) AS n
FROM `t`
WHERE labels.event = 'pma_prediction'
GROUP BY day
ORDER BY day;

-- 3.2 No-prediction rate by reason
SELECT
  jsonPayload.pma.reason AS reason,
  COUNT(*) AS n,
  SAFE_DIVIDE(COUNT(*), SUM(COUNT(*)) OVER ()) AS share
FROM `t`
WHERE labels.event = 'pma_prediction'
  AND jsonPayload.pma.decision = 'no_reliable_prediction'
GROUP BY reason
ORDER BY n DESC;

-- 3.3 Decision mix
SELECT jsonPayload.pma.decision AS decision, COUNT(*) AS n
FROM `t`
WHERE labels.event = 'pma_prediction'
GROUP BY decision
ORDER BY n DESC;

-- 3.4 Latency percentiles
SELECT
  APPROX_QUANTILES(jsonPayload.latency_ms, 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(jsonPayload.latency_ms, 100)[OFFSET(90)] AS p90_ms
FROM `t`
WHERE labels.event = 'pma_prediction';
```

Until T19 ships, do not point a dashboard at these - they will silently
return zero rows against the existing `genai_logs_to_bq` sink table rather
than erroring, which looks like "no predictions happened" instead of "not
wired up yet". Use §2's live Logs Explorer queries for anything that needs
to be trustworthy today.

## 4. The go-live data gate: `scripts/pma_data_gate.py`

`PMA_PREDICTION_ENABLED` must not be set to `true` in production until this
script passes **and** the T14 backtest's false-gate rate at the chosen
threshold is <= 5% (§8.4 item 11's other half; checked by
`scripts/pma_backtest.py`, not by this script). This script covers the
data-side half of that gate only.

### What it checks

The script is entirely read-only (SELECT / `INFORMATION_SCHEMA` reads
through the same allowlisted `QueryRunner` the online predictor uses); it
issues no DDL/DML and never touches Terraform.

1. **Curated build presence** - all 7 tables
   `pm_agent.prediction.policy.REQUIRED_BUILD_TABLES` requires
   (`dim_focus_components`, `fct_replacement_events`, `dim_reference_set`,
   `replacement_anchor_embeddings`, `wo_embeddings`,
   `adjudicated_precursors`, `fct_lead_time_samples`) exist in
   `pma_agent_curated`.
2. **Build consistency** (`policy.check_build`, fail-closed, §5.6) -
   creation-time spread across those 7 tables is <=
   `PMA_BUILD_MAX_SPREAD_S` (3,600 s default) and no downstream table is
   older than the `fct_replacement_events` anchor. This is the same check
   the online service treats as a hard `data_source_unavailable` at
   request time; running it standalone here catches a half-refreshed
   curated rebuild before any production traffic hits it.
3. **Embedding integrity (768-d/status)** - every row of
   `wo_embeddings.embedding` and
   `replacement_anchor_embeddings.anchor_embedding` has `status = ''` and
   an embedding of exactly `PMA_EMBEDDING_DIM` (768) dimensions. The
   online kNN templates already filter on this per-row, so a failure here
   does not mean predictions are wrong - it means the *filtered* result
   set may be silently thinner than the raw table suggests, which can look
   like `insufficient_samples`/`no_confident_component_match` noise in the
   logs (§2.2) that actually has a curated-build root cause.
4. **Anchor fan-out dedup count** - `replacement_anchor_embeddings` has one
   row per registered serial per replacement (1,720 rows on the live
   2026-09-23 profile), so `COUNT(DISTINCT replacement_wo_uuid)` must equal
   the true number of distinct replacement events (718 on that profile).
   A mismatch usually means the anchor-build fan-out logic changed
   upstream; override the expectation with
   `--expected-anchor-dedup-count` only after confirming the new number is
   correct (e.g. after T17a's dedup rework lands).

Also printed, **informational only, never fails the gate**: the
lead-sample replacement-to-replacement share per focus component
(`LeadStats.replacement_interval_share` / `chained_sample_n`, §5.5 row 4).
On the live profile this is already known to run high for some components
(most of `473597-5|AFT`'s 27 samples are closing-TAC-to-closing-TAC gaps
between two consecutive replacements, not symptom-to-replacement lead
times). It is not a pass/fail condition of item 11 - the decision policy
already routes a component whose share exceeds
`PMA_MAX_REPLACEMENT_INTERVAL_SHARE` to
`samples_not_symptom_to_replacement` at request time - this script prints
it purely so a human reviews the number, per component, before flipping the
flag.

### Usage

```bash
uv run python scripts/pma_data_gate.py
uv run python scripts/pma_data_gate.py --as-of 2026-09-23T00:00:00Z
uv run python scripts/pma_data_gate.py --expected-anchor-dedup-count 718
```

`--as-of` only affects the cutoff used for the informational lead-sample
report (default: now, UTC); it does not change any of the four gating
checks. `--expected-anchor-dedup-count` overrides the anchor-dedup
expectation (default 718, the live profile in plan §3.1).

### Exit codes

- `0` - every gating check passed. This satisfies the data-side half of
  §8.4 item 11; still confirm the T14 false-gate rate separately before
  setting `PMA_PREDICTION_ENABLED=true`.
- `1` - at least one gating check failed (or BigQuery access itself failed,
  e.g. an ADC/credentials problem or a missing `dataViewer` grant on
  `pma_agent_curated` - see plan §3.1's IAM note). The printed detail names
  the exact failing check and, for build-consistency failures, one of
  `curated_build_inconsistent:missing_tables:...`,
  `curated_build_inconsistent:spread_s:...`, or
  `curated_build_inconsistent:stale_downstream:...`.
- `2` - bad CLI arguments (e.g. an unparsable `--as-of`).

The informational lead-sample report is printed regardless of the other
checks' outcome and never affects the exit code.
