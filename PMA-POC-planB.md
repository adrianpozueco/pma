# PMS Plan B - Direct Synthetic Tables for POC

## Objective

Deliver a working POC for online prediction by bypassing the XML ingest and transform pipeline.
Generate only the minimum synthetic tables required by the online agent:

- `curated.wo_embeddings`
- `curated.fct_lead_time_samples`

Optional support tables (if current runtime expects them):

- `curated.dim_focus_components`
- `curated.dim_reference_set`

This plan is intended for delegation and execution by another engineer.

---

## Scope and non-scope

### In scope

1. Generate schema-compatible synthetic rows for required curated tables.
2. Load tables in BigQuery (or local equivalent for demo).
3. Validate retrieval and lead-time projection behavior in the online path.

### Out of scope

1. XML parsing correctness.
2. Real replacement detection from `componentChange` in raw AMOS data.
3. Full offline precursor-mining quality proof from production-like history.

---

## Why this Plan B is acceptable for POC

It proves online decisioning behavior (retrieve -> rank -> project TAC window -> return explainable recommendation) without blocking on pipeline readiness.

It must be clearly labeled as synthetic so stakeholders know what is and is not validated.

---

## Required table contracts

## 1) `curated.wo_embeddings`

Generate rows with fields used by current queries:

- `wo_uuid`
- `wo_id`
- `aircraft_reg`
- `ata_chapter`
- `tac`
- `content`
- `embedding`

If SQL expects BigQuery `AI.EMBED` output structure, keep compatibility:

- `embedding.status = ""`
- `embedding.result = [float, ...]` with fixed dimension.

## 2) `curated.fct_lead_time_samples`

Generate rows as if Step 08 already completed:

- `component_key`
- `aircraft_reg`
- `precursor_wo_id`
- `precursor_wo_uuid`
- `precursor_tac`
- `replacement_wo_uuid`
- `replacement_tac`
- `lead_cycles`
- `sim`
- `verdict` (set to `"symptom"`)
- `llm_conf`

Hard constraint: `lead_cycles > 0` and `replacement_tac = precursor_tac + lead_cycles`.

## Optional 3) `curated.dim_focus_components`

Minimal fields:

- `component_key`
- `replacement_count`
- `aircraft_with_replacement`
- `freq_rank` (1..3)

## Optional 4) `curated.dim_reference_set`

Minimal fields:

- `component_key`
- `aircraft_reg`
- `replacement_wo_id`
- `replacement_wo_uuid`
- `replacement_tac`
- `replacement_date`
- `anchor_text`

---

## Synthetic truth model (design before generation)

Define 3 focus component profiles with explicit target behavior:

- `component_key`
- ATA family
- lead-time distribution target (`p50`, `p90`)
- failure text style

Example baseline targets:

- C1: `p50=80`, `p90=220`
- C2: `p50=140`, `p90=320`
- C3: `p50=60`, `p90=180`

This truth model is the reference used to verify generated outputs.

---

## Generation strategy

## A) `wo_embeddings`

1. Create aircraft timeline metadata with monotonic TAC progression.
2. Produce WO text in three classes:
   - symptom-like (close to component failure language)
   - routine (inspection/check/service)
   - unrelated/noise
3. Build vectors using synthetic centroids:
   - one centroid per component/failure mode
   - symptom rows near centroid (small noise)
   - routine/unrelated rows farther away
4. Normalize vectors to unit length for stable cosine behavior.
5. Keep embedding dimension fixed and equal to runtime expectation.

## B) `fct_lead_time_samples`

1. For each component, sample `lead_cycles` from configured distribution.
2. Generate precursor TAC and derive replacement TAC.
3. Set confidence-like fields:
   - `sim` in realistic range (for example 0.7-0.95)
   - `llm_conf` in realistic range (for example 0.6-0.98)
   - `verdict = "symptom"`
4. Ensure sample volume is large enough for stable quantiles.

Recommended minimum: at least 100 lead-time samples per focus component.

---

## Realism controls (must include)

Add controlled hard negatives in `wo_embeddings` population:

1. Similar wording but wrong component context.
2. Same ATA family but unrelated issue.
3. Near-duplicate admin/routine text.

Target mix for embedding corpus:

- 15-25% symptom-like
- 60-70% routine/unrelated
- 10-15% hard negatives

This avoids over-optimistic demo behavior.

---

## Suggested dataset sizes

Create at least one medium dataset for demo stability:

- 300 aircraft
- 50,000 WOs in `wo_embeddings`
- 300-600 lead-time rows in `fct_lead_time_samples` (100+ per focus component)

Optional tiers:

- Small smoke: 5,000 WOs
- Large stress: 200,000+ WOs

---

## Validation checklist (required before handoff)

1. Schema validation
   - no missing required fields
   - embedding structure matches query expectations
2. Vector integrity
   - constant dimensions
   - no null vectors
3. Lead-time integrity
   - all `lead_cycles > 0`
   - `replacement_tac - precursor_tac = lead_cycles`
4. Distribution checks
   - per-component `p50` and `p90` near configured targets
5. Retrieval sanity
   - known symptom-like query returns expected component neighbors
6. Confidence behavior
   - weak/noisy queries can produce `no_reliable_prediction`

---

## Deliverables for delegated owner

1. Synthetic data generator (script/package) with fixed random seed support.
2. Config file for component profiles and distribution parameters.
3. Output artifacts:
   - load-ready files for `curated.wo_embeddings`
   - load-ready files for `curated.fct_lead_time_samples`
   - optional `dim_focus_components` and `dim_reference_set`
4. Short runbook:
   - generate
   - load
   - validate
   - smoke test online retrieval and prediction contract
5. Validation report with metric snapshots and caveats.

---

## Risks and mitigations

1. Risk: synthetic data too clean, demo appears unrealistically strong.
   - Mitigation: enforce hard negatives and broad routine/noise share.
2. Risk: schema mismatch with existing SQL/runtime.
   - Mitigation: validate against current query field paths before load.
3. Risk: wrong embedding structure (nested vs flat) breaks joins/scoring.
   - Mitigation: mirror exact structure currently consumed (`status`, `result`).
4. Risk: stakeholders assume pipeline is proven.
   - Mitigation: add explicit disclaimer in demo deck and report.

---

## Exit criteria for Plan B

Plan B is complete when:

1. Online endpoint returns recommendations with explainable evidence and TAC p50/p90 windows.
2. Fallback behavior (`no_reliable_prediction`) triggers correctly on weak evidence.
3. Synthetic validation report is approved for POC demo use.
