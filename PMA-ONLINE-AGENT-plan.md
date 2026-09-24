# PMA Online Agent Plan: wiring the new BigQuery curated tables and views into the online agent

| | |
|---|---|
| **Scope** | Update the online PMA agent (`pm_agent/`) so a work order uploaded through chat, A2A or `POST /workorders/analyze` is checked against the focus components. A matching WO gets a typed decision, supporting evidence and, only when the data supports it, an observed closing-TAC interval. The data is read from the live `pma_agent_curated` tables and the `pma_agent_analytics` views. Anything else gets an explicit, typed "no reliable prediction" or "out of scope" answer. |
| **Replaces** | `PMA-POC-plan.md` §11 (lines 940-1108). §11 stays in that file as history. This document is the plan to implement. |
| **Status** | DRAFT rev 2 (after 4 critic reviews, see Appendix A). Needs user decisions on §11 Open Questions before Wave 1 starts. |
| **Date** | 2026-09-23 |
| **Branch** | `dev` (repo `/Users/kulagas/caveman-agent`) |
| **GCP** | project `qwiklabs-asl-04-1726946cb8ab` (the live data project; code never hard-codes it), BigQuery location `us-central1` |
| **Source docs** | `PMA-POC-plan.md` (§5-§11), `PMA-POC-plan-review.md`, `PMA-POC-planB.md`, `BIGQUERY-AGENT-plan.md` (§8.5 is binding for output semantics), `handovers/parallel-evidence-handover.md`, `handovers/next-agent-handover.md`, `handovers/frontend-handover.md`, `deployment/terraform/single-project/curated.tf`, live BigQuery profiling and dry-runs on 2026-09-23 (all read-only) |
| **Hard rules** | Never change model names (`MODEL = "gemini-3.8-flash"`, `pm_agent/config.py:29`). BigQuery stays read-only from the agent (`WriteMode.BLOCKED`, SELECT-only templates). Terraform apply, pipeline re-runs and `agents-cli deploy` need explicit human approval. |

### T00 decisions (recorded 2026-09-23)

- User instruction: "spawn the team". No explicit answers given, so **every OQ takes its stated default** (§11).
- **T17a-c: excluded from this run** (they touch `curated.tf`, which the live pipeline depends on; the re-run needs separate approval).
- **T15 (frontend mock): excluded** (optional, needs frontend owner). **T19: deferred** as planned.
- **OQ7:** the user has set `project_id = "qwiklabs-asl-04-1726946cb8ab"` in `vars/env.tfvars` (uncommitted, outside task ownership). The other live tfvars are still unrecorded, so there is no `terraform plan/apply` in this run.
- **Execution deviation from §9:** tasks run in the shared `dev` working tree (not per-task worktrees, whose default base is `origin/main` and would miss earlier waves). Exclusive file ownership plus a lead gate after each wave replaces the merge step.

### T00 decisions (recorded 2026-09-24)

- **Option B approved (projected replacement window).** User approved, 2026-09-24: once a labelled `supporting_interval` is attached (row 4 of §5.5), project it forward from the aircraft's own last replacement TAC (`fct_replacement_events`) into a `projected_window` (`tac_p50`, `tac_p90`, `cycles_since_last_replacement`, `position`). This is a deliberate, scoped override of **OQ1's default** and of `BIGQUERY-AGENT-plan.md` §8.5 "no absolute due TAC" - **only** for this clearly-labelled, non-calibrated, fleet-pattern window. Everything else in this plan (no confidence tiers, no `due_counter`, no per-WO forecast) is unchanged.
- **OQ6 default flipped.** `PMA_SHOW_SUPPORTING_INTERVAL` now defaults to `true` (was `false`) - the projected window in the previous bullet has no fleet interval to anchor without it. New setting `PMA_SHOW_PROJECTED_WINDOW` (default `true`) gates the new step independently.
- Failure isolation: a failed `last_replacement` lookup never changes `decision`/`reason`; it leaves `projected_window` `null` and adds the limitation `projected_window_unavailable`. See §6.5 for the new fixed strings (a)-(e) and `README.md` "Projected replacement window (Option B)" for the full rendering contract.

### T00 decisions (recorded 2026-09-24, later): replacement recommendation

- **Recommendation approved.** User request, 2026-09-24: "you should base [it] on `wo_embeddings` (match description in work order provided and action) and also `fct_lead_time_samples` should be used." Adds a condensed `recommendation` block, independent of the main `decision`/`reason`, based on a `wo_embeddings` neighbour vote matched on **description and action text** (`PMA_QUERY_INCLUDE_ACTIONS`, new setting, default `true`), with lead-time drawn from `fct_lead_time_samples` and anchored onto the aircraft's own `reference_tac` the same way Option B anchors its window (`tac_p50 = reference_tac + lead_tac_p50`, etc. for p90/p95).
- This is a **second, independent** scoped override of `BIGQUERY-AGENT-plan.md` §8.5 "no absolute due TAC", on top of Option B's window override above - approved by the same user-approval channel, same date. It changes nothing else: still no confidence tiers as a calibrated probability (the `confidence.level` heuristic below is a display rule, not a calibration claim, exactly like `PMA_MIN_SAMPLE` elsewhere in this plan), still no per-WO forecast, `due_counter` still always `null`, legacy `timing`/`replacement_recommendation` blocks unaffected.
- **`basis` enum and precedence:** `similar_workorders` (matched `fct_lead_time_samples` of the WO's `wo_embeddings` neighbours - the gate's exact PN+position component if it has samples, else, only when there is no exact gate match, a sum-of-similarity vote with `n >= 3`, share `>= 0.5`, no `#1`/`#2` choice), then `component_history` (gate component's own lead-time stats, `n >= PMA_MIN_SAMPLE`), then `fleet_replacement_interval` (Option B `supporting_interval` + last replacement TAC), else none. Bases 1-2 project `tac_pX = reference_tac + round(lead pX)` and need a usable reference TAC (latest known, not counter-inconsistent, not observed after the cut-off); without one they fall through to `fleet_replacement_interval`.
- **`confidence.level` enum:** `high` = `similar_workorders`, `n >= PMA_REC_MIN_SAMPLE_HIGH` (8), CV `<= PMA_REC_MAX_CV` (0.5), mean similarity `>= PMA_REC_STRONG_SIM` (0.80); `medium` = `similar_workorders` with `n >= 3`, or `component_history` with `n >= 8` and CV `<= 0.5`; `low` otherwise.
- **`action` enum:** `recommend_inspection_or_part_planning` when level is `medium`/`high` or `reference_tac >= tac_p50`; otherwise `monitor`. Limitations: `recommendation_heuristic_not_calibrated` (always with a recommendation), `recommendation_unavailable` (lookup failed).
- New settings (§5.8): `PMA_QUERY_INCLUDE_ACTIONS` (`true`), `PMA_REC_NEIGHBOUR_K` (`100`), `PMA_REC_NEIGHBOUR_MIN_SIM` (`0.70`), `PMA_REC_MIN_SAMPLE_HIGH` (`8`), `PMA_REC_MAX_CV` (`0.5`), `PMA_REC_STRONG_SIM` (`0.80`), `PMA_SHOW_RECOMMENDATION` (`true`).
- Failure isolation, same as Option B: any exception from the neighbour vote or lead-time lookup is caught and logged; `recommendation` stays `null` and `decision`/`reason` are unaffected. See §6.2/§6.5 for the JSON shape and the fixed `**Recommendation:` header, and `README.md` "Replacement recommendation" for the full rendering contract.

### Data update (recorded 2026-09-24, ~10:45 UTC)

The user re-ran the curated pipeline from Step 02 with more parts. Figures in §1-§3 and §8.2 below that quote the 6-part build (n=27, p50 356, 718 anchors, "no live input reaches `historical_interval`") are historical; current values:

- `fct_replacement_events` was **not** rebuilt (created 2026-09-23 15:20:37); the 6 downstream tables were rebuilt 2026-09-24 10:36:37-10:45:44. `policy.check_build` now measures the spread over the 6 downstream tables only (plus the "no downstream table older than `fct_replacement_events`" check), and `scripts/pma_data_gate.py` replaces "dedup = 718" with a data-derived invariant (anchor dedup = distinct replacement WOs with anchor text in `dim_reference_set`, 2561).
- `dim_focus_components` 20 parts (was 6); `fct_lead_time_samples` 128 rows over 14 parts; `replacement_anchor_embeddings` 2561 distinct anchors (was 718).
- Live stats (`PERCENTILE_CONT`, as of 2026-09-23/24): `473597-5|AFT` n=35, 28 aircraft, p50 381, p90 1890.8, share 0.971 (still row 4); `45-0351-4|RH` n=5, 5 aircraft, p50 394, p90 1001.6, share 0.80 (now row 4); `9651-35-0005|CABIN` n=13, 9 aircraft, share 0.38, p50 1659 / p90 2301 and `9651-35-0002|CABIN` n=13, share 0.38 - **`historical_interval` is now live-reachable** (exact part number + `CABIN`).
- **Anchor threshold re-tuned 0.80 → 0.84** (`scripts/pma_backtest.py --as-of 2026-03-01`, 1720 pre-cutoff anchors, 500 negatives): 0.80 now gives 23.0% false gates; 0.84 is the lowest ≤ 5% (3.8%, coverage 11.0%). Consequence: both symptom-only fixtures (cargo AFT, RH) now return `no_confident_component_match`.
- Only 6 of the 20 keys can be resolved by the symptom-only vote; the other 14 share a part number (or alias) with a sibling position and return `ambiguous_position` by design (`_resolve_vote`). `72184025|FWDGALLY` is ambiguous even with an exact part number (alias `62197301001` also has `FWDGALLY`).
- AFT window for `SP-REG00374`: TAC 17938 + 381/1891 → 18319-19829, 722 cycles since, `between_p50_p90`.

---

## 1. TL;DR

- **The live curated data is real AMOS output, not synthetic.** Terraform built it with the real-data SQL pipeline (`curated.tf:63-415`, tables created 2026-09-23 15:20:37-15:26:30 UTC).
- **The data is thin and mostly not what it claims to be.** There are 6 focus components, but only 2 have lead-time samples, 28 rows in total: `473597-5|AFT` n=27 (24 aircraft, CV 1.26) and `45-0351-4|RH` n=1. **26 of 28 samples are closing-TAC to closing-TAC gaps between two consecutive replacements, not symptom-to-replacement lead times.** One of the only two non-replacement AFT precursors is a *FWD* cargo loop WO.
- **No backend code reads `pma_agent_curated` or any `v_*` view.** The tables the backend does read (`retrieval_documents`, `retrieval_embeddings`, `faa_sdr_wo_parts`) are empty, and the runner allowlists reject everything else.
- **Output semantics follow `BIGQUERY-AGENT-plan.md` §8.5.** No confidence tiers from n/CV, no absolute due TAC, no `due_counter`. A numeric interval is shown only as an observed closing-to-closing interval with support descriptors, and only when samples are mostly symptom-to-replacement (§5.5).
- **Target design**
  - One prediction core, `pm_agent/prediction/`, called from `WorkOrderAnalysisService.analyze_xml`, so chat, A2A and HTTP share it.
  - Deterministic part-number + position match first (with part-number aliases for superseded parts). The anchor-kNN vote is used only for symptom-only WOs, never to pick between engine `#1`/`#2`.
  - Query embedding through BigQuery `AI.EMBED(..., endpoint => 'text-embedding-005')` (768-d, the offline endpoint). Brute-force cosine over 718 deduplicated anchors (12.7 MB/call) and `wo_embeddings` (30.4 MB/call).
  - Per-component statistics from `fct_lead_time_samples`, evidence from the same table joined to `adjudicated_precursors` for reasons.
- **Expected behaviour on today's data (defaults)**
  - `473597-5|AFT`: gate passes for symptom-only AFT smoke-detector text; decision `no_reliable_prediction` / `samples_not_symptom_to_replacement` (share 0.93). The consecutive-replacement interval (n=27, p50 356, p90 1,968) is shown only as a labelled `supporting_interval` block if OQ6 allows it.
  - `45-0351-4|RH`: `insufficient_samples` when PN + position are present; symptom-only text currently fails the vote (`no_confident_component_match`).
  - Engine keys: `no_lead_time_samples` when the WO names the part number (with or without position); `no_confident_component_match` for symptom-only text.
  - Boiler and oven: `out_of_scope`.
  - **`historical_interval` is unreachable on today's data.** It is tested with fakes only. This is intended until T17 and a re-run produce real symptom-to-replacement samples.
- **The flag stays off in production until the T14 go-live gate passes** (§8.4 item 11): threshold chosen from a symptom-only backtest with negatives, false-gate rate ≤ 5%.

---

## 2. Review of old §11

Verdicts come from checking §11 against live BigQuery and the code on 2026-09-23.

| # | §11 item | Verdict | Evidence | Action in this plan |
|---|---|---|---|---|
| R1 | 11.1 Objective: recommend-only risk, TAC window, confidence, evidence | Intent valid, semantics conflict with §8.5 | Only 2 of 6 focus keys have lead samples. `BIGQUERY-AGENT-plan.md` §8.5: historical quantiles are "not the forecast", "no threshold such as eight pairs or low coefficient of variation establishes predictive confidence", a replacement deadline needs "a validated, reviewed decision policy". | Keep recommend-only. `no_reliable_prediction` and `out_of_scope` are first-class. No tiers, no due TAC (§5.5, OQ1). |
| R2 | 11.2.1 Inputs: `closingTac`, `ataChapter`, `wo_text` | Partly wrong | A new WO is open, so it has no closing TAC. `issue.tac` is NULL in 8,214 of 8,259 rows. `ata_chapter` is not exposed in `parsed_context` (`service.py:478`). The legacy `query_text` leaves out action text (`service.py:440-442`); `wo_embeddings.content` includes it (`curated.tf:145-172`). | O1 adds a separate `pma_wo_text` built with the exact offline recipe (§5.1); the legacy `query_text` is unchanged. `ata_chapter` and `position` are exposed. |
| R3 | 11.2.2 Artifacts `curated.*` | Wrong names, incomplete | The live dataset is `pma_agent_curated`; the analytics side is `pma_agent_analytics`. The list omits `adjudicated_precursors`, `replacement_anchor_embeddings`, `fct_replacement_events` and `v_component_frequency`. Step 10 validation was never implemented. | Section 3 has the full inventory. T17c adds a data-quality gate; T18 adds a read-only gate script. |
| R4 | 11.2.2 Vertex Vector Search index and endpoint | Drop it | No PMA vector index exists (`INFORMATION_SCHEMA.VECTOR_INDEXES` = 0 rows). The corpus is 4,639 + 718 vectors; brute force costs 12.7 + 30.4 MB per call. The only Vertex index, `cord19_embeddings`, is unrelated. | Brute-force `ML.DISTANCE` in BigQuery (§4.4), justified by size and latency only. |
| R5 | 11.2.3 Config `SIM_THRESHOLD`, `K_NEIGHBORS`, `MIN_SAMPLE`, `MAX_CV` | Ambiguous, not in code | `SIM_THRESHOLD` means precursor-vs-anchor offline (0.80 in `curated.tf`, 0.62 in §7). None of these exist in `config.py` or `.env`. | New `pm_agent/prediction/settings.py` with online-only names (§5.8). `MAX_CV` dropped (§8.5). |
| R6 | O1 Parse and normalize | Mostly implemented, differently | `amos_data.parse_workorders`. `wo_id` = uuid, else number (`service.py:190`). No early reject. | Extend with `pma_wo_text`, `ata_chapter`, `position`, part numbers and reject codes. |
| R7 | O1 `current_tac` = closingTac, else latest known | Conflicts with code and §8.5 | The code requires a user-supplied TAC no older than 24 h (`service.py:385-407`). §8.5 forbids adding aircraft TAC to another aircraft's interval. The data ends 2026-09-13. | `current_tac` becomes context only, with a source/staleness chain (§5.1). No absolute window. |
| R8 | O2 Optional LLM gate reusing Step 07, Top-3 | Wrong on 3 counts | (a) Step 07 needs a known replacement; none exists online (review:82). (b) The live focus set is the top 6 (`curated.tf:110`). (c) The backend gate is the legacy 3 part numbers (`service.py:15-38`), and position is never resolved (`workorder_upload.py:91-100`). | Deterministic PN(+alias) + position first, then an anchor vote for symptom-only WOs. No LLM in the gate (§5.2). |
| R9 | O3 Embed and kNN | Right idea, wrong mechanics | The code defaults to `gemini-embedding-001` 3072-d (`service.py:673`); curated vectors are `text-embedding-005` 768-d. `wo_embeddings` has no date column. | `AI.EMBED` in BigQuery; anchors for the gate, `wo_embeddings` for evidence; replay cut-off via `wo_workorders.closing` (§5.3). |
| R10 | O4 Map neighbours to precursors and aggregate | Structurally weak | Only 28 of 4,639 embedded WOs are precursors. Adjudications fan out: 39 rows for 30 distinct pairs vs 28 lead samples. | Stats and evidence both from `fct_lead_time_samples`; adjudications only add `reason`/text (§5.4). |
| R11 | O5 `current_tac + lead_p50/p90`; tiers | Wrong | Mixes TAC across aircraft; the lead starts at a closing TAC, not symptom onset; §8.5 forbids both the tiers and the addition. | Relative interval only, with support descriptors (§5.5). |
| R12 | O6 Explainable output | Missing | "Unavailable" is hard-coded in `evidence_branches.py:404-408`, `chat.py:380-387,445-448` (`render_uploaded_workorder_summary`, the analyzed path), `chat.py:540-542` (legacy path) and `service.py:582-651`. | Conditional wording from `pma`; fixed user-facing strings (§6.5). |
| R13 | 11.4 Response contract | Partly compatible; narrowed | No `analysis_as_of`, no `out_of_scope`, no provenance. Old O6 returned a ranked `recommendations[]` list; this plan returns one decision and, when positions tie, `component.candidates[]` with shares/n. The example key `15800-029-3|CABIN` is not in the focus set. | Reconciled contract in §6. The narrowing is deliberate. |
| R14 | 11.5 Guardrails | 1 and 3 hold; 4 and 5 missing | Telemetry sink and `completions_view` have 0 rows. The sink filter (`telemetry.tf:64`) only matches GenAI inference events; `telemetry_logs_filter` needs `labels.type="agent_telemetry"`. | Structured prediction log with the full `pma` block, routed to a queryable sink (T09 + T18, OQ11). |
| R15 | 11.6 Weekly calibration loop | Premise outdated; not dropped | The corpus is a static snapshot ending 2026-09-13. Nothing persists predictions. | Retrospective symptom-only backtest now (T14); outcome-join spec deferred and tracked (T19, OQ11). |
| R16 | 11.7 Go-live checklist | Outdated | No AMOS event consumer; "vector index smoke tests" no longer applies. | Acceptance §8.4 items 8-11 cover latency, monitoring, reviewer workflow and a mandatory data gate. |
| R17 | §6 and §7 feeding §11 | Outdated | SIM 0.62 / K 30 vs 0.80 / 50 live. LLM `gemini-2.5-pro` vs `gemini-3.8-flash` used; the Terraform default is spelled `gemini-3.8.flash`. | Recorded as drift. Model names are not changed (OQ8). |

---

## 3. Current state

### 3.1 Live BigQuery inventory (read-only profile, 2026-09-23)

**`pma_agent_curated`**: REAL data. The dataset description reads "Curated PMA artifacts generated from analytics source data by Terraform-run BigQuery SQL jobs". Built by `curated.tf` in real-data mode with `SIM_THRESHOLD=0.8`, `K_PRECURSORS=50`, `text-embedding-005` (768-d) and LLM `gemini-3.8-flash`. No table carries labels or embedding-provenance metadata.

| Object | Type | Rows | Key columns | Purpose / caveats |
|---|---|---|---|---|
| `fct_replacement_events` | TABLE | 8,956 | `aircraft_reg`, `component_key` (`PN\|POSITION`, PN = `COALESCE(part_on_number, part_off_number)`, `curated.tf:67-71`), `part_off_pn`, `part_on_pn`, `position`, `serial_off`, `serial_on`, `label_number`, `replacement_wo_id`, `replacement_wo_uuid`, `replacement_tac` INT64, `replacement_date` DATE, `anchor_text`, `is_supersession` BOOL | One row per serial swap. 737 keys, 627 aircraft, 7,552 WOs. 178 rows NULL `aircraft_reg`. Superseded removals: for `340-001-038-0\|#1`, 155 of 283 rows have `part_off_pn` `-036-0/-028-0/-026-0/-039-0`. |
| `dim_focus_components` | TABLE | 6 | `component_key`, `replacement_count`, `aircraft_with_replacement`, `freq_rank` | Top 6 by swap count (§3.2). |
| `v_component_frequency` | VIEW | 737 | `component_key`, `replacement_count`, `aircraft_with_replacement` | Full frequency list. Not read online. |
| `dim_reference_set` | TABLE | 1,720 | `component_key`, `aircraft_reg`, `replacement_wo_id`, `replacement_wo_uuid`, `replacement_tac`, `replacement_date`, `anchor_text` | 718 distinct WOs (per-serial fan-out); each maps to exactly one key. 157 rows NULL registration. Dates 2025-01-01 to 2026-09-11. Anchor split: AFT 321 (45%), RH 200 (28%), engine keys 38/43/54/62. |
| `wo_embeddings` | TABLE | 4,639 | `wo_uuid`, `wo_id`, `aircraft_reg`, `ata_chapter` (`NN-NN` text; 1 malformed row `05`), `tac`, `content`, `embedding STRUCT<result ARRAY<FLOAT64>, status STRING>` | Every WO on the 329 reference aircraft. All status `''`, all 768-d. Content = offline recipe (§5.1). No date column. 692 are focus replacement WOs; 647 of those have content byte-identical to their anchor. 26 anchors (NULL registration) have no row here. |
| `replacement_anchor_embeddings` | TABLE | 1,720 | `replacement_wo_uuid`, `component_key`, `aircraft_reg`, `replacement_tac`, `content`, `anchor_embedding STRUCT<result, status>` | 718 distinct anchors, duplicated per serial; one embedding per uuid. 1,667 of 1,720 contents contain `REPLAC\|INSTALL\|REMOV` (action text). |
| `candidate_precursors` | TABLE | 320 | candidate pairs | Offline intermediate. Not read online. |
| `scored_precursors` | TABLE | 441 | + `sim` (0.8006-0.9739) | Offline intermediate, fan-out inflated. Not read online. |
| `adjudicated_precursors` | TABLE | 39 | + `precursor_text`, `anchor_text`, `verdict`, `llm_conf`, `reason`, `llm_status`, `llm_full_response` | All `verdict='symptom'`. 30 distinct pairs vs 28 lead samples (2 pairs are not the earliest replacement for their precursor). Rejections not stored. |
| `fct_lead_time_samples` | TABLE | 28 | `component_key`, `aircraft_reg`, `precursor_wo_id`, `precursor_wo_uuid`, `precursor_tac`, `replacement_wo_uuid`, `replacement_tac`, `lead_cycles`, `sim`, `verdict`, `llm_conf` | One row per (key, aircraft, precursor), earliest replacement (`curated.tf:407-414`). `473597-5\|AFT` n=27 (24 aircraft, 2 chained samples), `45-0351-4\|RH` n=1. 26 of 28 precursors are same-key replacements. |

**`pma_agent_analytics`**: REAL data (AMOS export, registrations anonymised).

| Object | Type | Rows | Key columns | Purpose / caveats |
|---|---|---|---|---|
| `wo_workorders` | TABLE | 8,259 | `workorder_uuid`, `workorder_number`, `ata_chapter` (`NN-NN`; 3 rows `05`), `remarks`, `aircraft{...}`, `issue{date, ts, tac}`, `closing{date, ts, total_aircraft_cycles}`, `work_steps[]{sequence_number, description, headline, actions[]{action_text, component_changes[]}}`, `part_keys[]` | 629 aircraft. `issue.tac` NULL in 99.5%. Closing dates 2010-06-19 to 2026-09-13; 55 WOs closed before 2025-01-01. 16 per-aircraft TAC decreases and 28 jumps > 2,000 cycles when ordered by closing time, although `counter_regression` is 0 everywhere. |
| `v_work_orders` | VIEW | 8,259 | `workorder_uuid`, `workorder_number`, `full_registration`, `ata_chapter`, `closing_date`, `closing_ts`, `closing_aircraft_cycles`, `issue_aircraft_cycles`, `counter_basis`, `quality_flags` | Used online for the TAC context (0.71 MB/call). |
| `v_symptom_records` | VIEW | 8,276 | `workorder_uuid`, `step_id`, `step_sequence_number`, `description`, `headline`, `full_registration`, ... | Symptom-only text source for T14 backtest and T07 fixtures. Not read by the online agent. |
| `v_component_changes` | VIEW | 9,085 | `workorder_uuid`, `position`, `part_off_number`, `part_on_number`, ... | Offline analysis only (positions of `473597-5`: AFT 335, FWD 79, B3/AFTCARGO/CARGO 1 each). Not allowlisted. |
| `v_observed_removals` | VIEW | 9,014 | `removed_part_key`, `removal_reason_class`, ... | Not used. Not allowlisted. |
| `v_target_replacements` | VIEW | 2,358 | same | Hard-codes the legacy 3 part numbers. **Not used online** (OQ2). |
| `retrieval_documents`, `retrieval_embeddings` | TABLE | **0** | 3072-d F2 schema | Empty. Not used. |
| `faa_sdr_wo_parts` | TABLE | **0** | FAA SDR columns | Empty; load job commented out (`analytics.tf:136-160`). |

**`pma_agent_telemetry`**: sink table 0 rows, `completions_view` 0 rows.

**Vector indexes**: none in BigQuery. The one Vertex index (`cord19_embeddings`, endpoint `2963093676902842368`) is unrelated.

**IAM**: `app_sa` (`iam.tf:48`) has `roles/bigquery.jobUser` (`analytics_views.tf:142`), `dataViewer` on analytics only (`analytics_views.tf:149`) and `roles/aiplatform.user` via `var.app_sa_roles` (`variables.tf:43`). **No grant on `pma_agent_curated`.** Which principal the deployed Agent Runtime actually uses (README:1178 `--service-account pma-agent-app@` vs the Vertex service agent in `variables.tf:38`) is unverified (T06).

### 3.2 Focus components and their data depth

| rank | component_key | What it is | ref WOs | lead n | PN aliases (normalised) | Expected online outcome |
|---|---|---|---|---|---|---|
| 1 | `2085M31G03\|#1` | LEAP-1B fuel nozzles, engine 1 | 43 | 0 | `2085M31G03` | PN in WO → `no_lead_time_samples`; symptom-only → `no_confident_component_match` |
| 2 | `473597-5\|AFT` | Aft cargo fire/smoke detector | 321 | 27 | `4735975` | Symptom-only AFT text → gate passes → `samples_not_symptom_to_replacement` (+ `supporting_interval` if OQ6) |
| 3 | `2085M31G03\|#2` | Fuel nozzles, engine 2 | 38 | 0 | `2085M31G03` | as rank 1 |
| 4 | `340-001-038-0\|#1` | CFM56-7B fan blades (engineering-order task EO.18/086, scheduled, not symptom-driven), engine 1 | 62 | 0 | `3400010380`, `-0360`, `-0280`, `-0260`, `-0390` | PN in WO → `no_lead_time_samples`; symptom-only → `no_confident_component_match` (engine keys are never chosen by the vote) |
| 5 | `340-001-038-0\|#2` | CFM56-7B fan blades, engine 2 | 54 | 0 | `3400010380`, `-0360`, `-0280` | as rank 4 |
| 6 | `45-0351-4\|RH` | RH retractable landing light | 200 | 1 | `4503514` | PN + RH → `insufficient_samples`; symptom-only currently fails the vote (max sim 0.808 on a probe) |

Lead distribution for `473597-5|AFT` (n=27, 24 aircraft): min 4, max 3,158, mean 667, sd 839, CV 1.26. `PERCENTILE_CONT` p50 **356.0**, p90 **1,968.2** (the discrete `APPROX_QUANTILES` decile is 2,330; this plan uses `PERCENTILE_CONT` everywhere).

Live probes (symptom-only text, `AI.EMBED` vs deduplicated anchors, top-20):

| Probe text | Top-20 keys | max sim | ≥ 0.80 | ≥ 0.85 |
|---|---|---|---|---|
| "AFT CARGO SMOKE DETECTOR LOOP B INOP. FAULT CONFIRMED ON CARGO FIRE PANEL TEST." | 20 × AFT | 0.91 | 20 | 18 |
| "RH RETRACTABLE LANDING LIGHT INOP ON WALKAROUND" | 20 × RH | 0.808 | 2 | 0 |
| "ENG 1 FUEL NOZZLE LEAK FOUND DURING BORESCOPE" | 13 × `#1`, 7 × `#2` | 0.784 | 0 | 0 |

Gate false-positive check (all 4,639 `wo_embeddings` rows as queries vs 718 anchors, self excluded, K=20, support ≥ 3, share ≥ 0.5; full-text queries, so optimistic for focus WOs):

| threshold | non-focus WOs passing (of 3,947) | focus replacement WOs passing (of 692) |
|---|---|---|
| 0.80 | 1,242 (31%) | 689 |
| 0.85 | 297 (7.5%) | 640 |
| 0.90 | 76 (1.9%) | 406 |

### 3.3 Current backend architecture

```
pm_agent/agent.py:52-82  Workflow  (App name "pm_agent", agent.py:79)
START -> prepare_workorder_upload            nodes/workorder_upload.py:123
   "workorder_prompt"   -> display_workorder_upload (render_chat_upload, non-analyzed statuses only)  workorder_upload.py:153-157
   "workorder_evidence" -> bq_evidence || ipc_evidence          nodes/evidence_branches.py:243,263
                        -> join_evidence (JoinNode)             evidence_branches.py:283
                        -> compose_evidence_answer (markdown; render_uploaded_workorder_summary)  evidence_branches.py:357,377
   "chat"               -> router (route_classifier LLM)        nodes/router.py:84,125
                           "ipc" -> ipc_manual_retrieval (VertexAiSearchTool)
                           "bq"  -> bq_analytics (BigQueryToolset, WriteMode.BLOCKED, free SQL)  sub_agents/bq_analytics/agent.py:114
```

- **Model.** Every LLM call uses `gemini-3.8-flash` (`config.py:29`). Do not change it.
- **BigQuery access** via predefined templates through `QueryRunner` (`queries.py`): `_ALLOWED_DATASETS={pma_agent_analytics}`, `_ALLOWED_TABLES={wo_workorders, faa_sdr_wo_parts}` (`queries.py:65-66`), `__init__` rejects any other dataset (`:139`), `table()` always emits `` `project.self.dataset.name` `` (`:152-158`), placeholders are filled via `table_placeholders`, `@limit` is always bound as `limit+1` (`:178-206`), only scalar parameters (`:211-213`), and `_RunOutcome` holds `status, rows, truncated, error_detail` only (`:88-96`). Templates never hard-code a project.
- **Tools in `bq_evidence`** (`evidence_branches.py:177-225`) loop over `EvidenceRequest.part_candidates` (`:196`), which come only from `TARGET_PARTS` (`workorder_upload.py:20-27`, `service.py:262,296-314`). `AircraftContext.position` exists but is hard-coded `None` (`workorder_upload.py:60`).
- **HTTP.** `POST /workorders/analyze` (`fast_api_app.py:187-236`) constructs `WorkOrderAnalysisService` at `:227` inside `run_in_threadpool`; the only construction guard is the 503 for the history provider (`:215-226`).
- **Chat.** `analyze_chat_upload` (`chat.py:170`) runs `WorkOrderAnalysisService().analyze_xml` in `asyncio.to_thread` (`chat.py:304-310`) and returns `{status, source, analysis, uploaded_workorder}` (`:328-333`). Chat defaults `analysis_as_of=now()` (`:300`), and closed WOs default to `historical_replay`.
- **Third caller.** `analyze_current_session_artifact` (`pm_agent/workorders/artifacts.py:80`) takes an injected service.
- **Scope gate.** `TARGET_PARTS` = `2085M31G03`, `62197301001`, `820111000001` (`service.py:15-38`); `AnalysisInput.__post_init__` (frozen dataclass, no I/O) rejects other `target_part_number` values (`service.py:82-89`). `manual_evidence` indexes `TARGET_PARTS[item["part_key"]]` (`:217`).
- **Deployment.** `deployment_metadata.json`: `agent_runtime`, A2A. The engine gets env only from `deployment/terraform/single-project/service.tf:39-105` (no `PMA_*`); `GOOGLE_CLOUD_PROJECT` is injected by the platform (comment `service.tf:44`). The FastAPI `/workorders/analyze` route has no hosted backend on Agent Runtime.
- **Frontend** (`frontend/src/data/client.ts`) is mock-only; `Outlook` is `illustrative | unavailable` (`frontend/src/domain.ts`, README:67). `App.tsx:1062-1107` renders "Remaining cycles" and a calendar window from `cyclesPerDay`.
- **Tests.** 206 unit tests collect and pass offline. `[tool.pytest.ini_options]` has no markers. `frontend/package.json` scripts: `build`, `test:ui`, `test:e2e` (no `test`). Pinned current behaviour: `tests/integration/test_workorder_upload.py` (`prediction.status=="model_unavailable"`), `tests/eval/upload_workorder_metric.py` (asserts "BigQuery and the knowledge base were not queried"), `tests/unit/test_bq_predefined_queries.py`, `tests/unit/test_history_retrieval*.py`.

---

## 4. Target architecture

### 4.1 Diagram

```
                chat / A2A XML upload                          POST /workorders/analyze
                        |                                                 |
   prepare_workorder_upload (nodes/workorder_upload.py:123)       fast_api_app.py:227 (run_in_threadpool)
        -> analyze_chat_upload (chat.py, existing asyncio.to_thread)      |
                        +------------------------+------------------------+
                                                 v
                     WorkOrderAnalysisService(history_provider, *, predictor=None).analyze_xml
                      O1 parse/normalize (+pma_wo_text, ata_chapter, position, part numbers)
                                                 |   predictor injected (Protocol); None -> pma "prediction_disabled"
                   +-------------------------------------------------------------------+
                   | pm_agent/prediction/   (no import-time side effects)              |
                   |  service.PrecursorPredictor.predict(PredictionInput)              |
                   |   build info  -> repository.curated_build_info()   [TTL cache]    |
                   |   O2 scope    -> repository.focus_components()     [TTL cache]    |
                   |                  policy.resolve_scope()  (PN alias + position)    |
                   |   O3 embed    -> repository.embed_query()   (symptom-only path)   |
                   |      anchor kNN -> repository.anchor_neighbours()                 |
                   |      WO kNN     -> repository.wo_neighbours()     (evidence only) |
                   |   O2b vote    -> policy.vote_component()                          |
                   |   O4 stats    -> repository.lead_time_stats()                     |
                   |      evidence -> repository.precursor_evidence()                  |
                   |   O5 decide   -> policy.decide() ; current_tac context            |
                   |                  -> repository.latest_closing_tac()               |
                   |   O6 build    -> contracts.PredictionResult (+ prediction log)    |
                   +-------------------------------------------------------------------+
                                                 |
            fills analyze_xml.pma, parsed_context.{ata_chapter,position}; timing/replacement_recommendation (§6.1)
                        |                                                 |
   workorder_evidence route -> bq_evidence || ipc_evidence          JSON response
     (PartCandidate added for pma.component; AircraftContext.position filled)
     -> join -> compose_evidence_answer (render_uploaded_workorder_summary, §6.5 strings)
```

### 4.2 Agent graph changes

- **Topology unchanged.** No new node. The prediction runs inside `analyze_xml`, so chat, A2A and HTTP share one core.
- **No new thread wrapping.** The predictor runs inside the existing `asyncio.to_thread` (chat) and `run_in_threadpool` (HTTP). The real task is a patchable seam: `chat.py` and `fast_api_app.py` import `default_predictor` at module level so tests can monkeypatch it.
- `compose_evidence_answer` / `render_uploaded_workorder_summary` render the prediction section from `(upload_result.get("analysis") or {}).get("pma")`.
- `bq_evidence` and `ipc_evidence` code is unchanged, but `_build_evidence_request` adds `PartCandidate(part_key, part_number, "resolved", roles=("pma_component",))` when the gate resolved a component, and fills `AircraftContext.position` from the resolved position, falling back to `position_info.position`. `EvidenceRequest` gets **no** new fields.
- `bq_analytics` prompt: lists the analytics `v_*` views; curated tables stay out of the free-SQL prompt by default (OQ10). Model unchanged.
- `route_classifier`, `ipc_manual_retrieval` and `MODEL` untouched.

### 4.3 Tables and views each template reads (dry-run bytes, 2026-09-23)

| Template | Reads | Bytes processed | Cached |
|---|---|---|---|
| `pma_focus_components.sql` | `dim_focus_components`, `fct_replacement_events` (aliases) | 0.40 MB | TTL 600 s |
| `pma_embed_query.sql` | none (`AI.EMBED`, one Vertex call) | 0 | no |
| `pma_anchor_neighbours.sql` | `replacement_anchor_embeddings`, `dim_reference_set` | 12.7 MB | no |
| `pma_wo_neighbours.sql` | `wo_embeddings`, `wo_workorders` | 30.4 MB | no |
| `pma_lead_time_stats.sql` | `fct_lead_time_samples`, `fct_replacement_events` | 0.58 MB | no |
| `pma_precursor_evidence.sql` | `fct_lead_time_samples`, `adjudicated_precursors`, `fct_replacement_events` | 0.44 MB | no |
| `pma_latest_closing_tac.sql` | `v_work_orders` | 0.71 MB | no |
| `pma_curated_build_info.sql` | `INFORMATION_SCHEMA.TABLES`, `INFORMATION_SCHEMA.TABLE_OPTIONS` | 21 MB (2 × 10 MB minimum) | TTL 600 s |

Per-request budget (T01 check): anchor ≤ 15 MB, wo ≤ 35 MB, stats ≤ 5 MB, evidence ≤ 5 MB, TAC ≤ 5 MB, focus ≤ 5 MB, build info ≤ 25 MB. Billed bytes per `predict()` ≤ 200 MB including the 10 MB per-table minimum. The 5 GB runner cap stays.

### 4.4 Retrieval approach and why

| Option | Evidence | Decision |
|---|---|---|
| Vertex Vector Search (old §11 Step 09) | No PMA index/endpoint; corpus 4,639 + 718 vectors; a second stack; review:114 and BIGQUERY-AGENT-plan §7.2 reject it. | **Rejected** |
| `BQHistoryProvider` over `retrieval_embeddings` | 0 rows; 3072-d `gemini-embedding-001`, incompatible with 768-d curated vectors. | **Not used**; code stays for the F2 track. |
| BigQuery brute-force cosine (`ML.DISTANCE`) over `pma_agent_curated` | Vectors in BigQuery, 768-d, status `''`. Offline scoring used `1 - ML.DISTANCE(a, b, 'COSINE')` (`curated.tf:266-295`). 43 MB per request. | **Chosen** |

- **Embedding parity.** Query vectors come from `AI.EMBED(@wo_text, endpoint => '{embedding_endpoint}')`, the call `curated.tf:198,219` makes. Verified live: `AI.EMBED` of stored content returns 768-d, status `''`, cosine 1.0 against the stored vector. Any parity failure comes from the text recipe (§5.1).
- **Endpoint handling.** A constant from `settings.py`, checked against `{"text-embedding-005"}`, substituted like table names. Never user input. **Vector-space contract:** the build-info query reads the curated table label `embedding_endpoint` (added by T17c). Label present and different → `embedding_incompatible` (fail closed). Label absent (today) → limitation `embedding_provenance_unverified`.
- **Representation shift (OQ9).** Anchors and `wo_embeddings` are full completed-WO text (symptom + actions); online open WOs and replay queries are symptom-only. Similarities are lower for symptom-only text (§3.2 probes), so thresholds must come from a symptom-only backtest (T14), not from offline full-text quartiles.
- **Why anchors.** Anchors cover all 6 keys (718 WOs); precursors cover 2 keys (28 WOs). `wo_embeddings` neighbours are evidence only.

---

## 5. Online decision flow O1-O6, rewritten against the real schemas

All SQL lives in `pm_agent/sub_agents/bq_analytics/sql/` (force-included in the wheel). **No template contains a project id.** Every table is a `{table_name}` placeholder that the repository fills with `runner.table(name, dataset=...)`, which returns a backtick-quoted, allowlisted, fully-qualified name. User values are always named `@params`. Every template keeps the header-comment convention (placeholders and parameters listed, as in `get_workorder.sql`). The runner always binds `@limit`; templates that do not use it (embed, stats, build info) ignore it, which BigQuery accepts.

**Parameter hygiene (all templates).** The repository never binds NULL: a scalar uuid is `''` when the upload has no uuid, arrays are `[u for u in ids if u]` and may be empty. SQL additionally guards with `IFNULL(...)` where a column may be NULL. A unit test asserts that no bound parameter value or array element is `None`.

### 5.1 O1: parse and normalize (in `workorders/service.py`)

| Field | Rule |
|---|---|
| `wo_id` | `workorder_uuid`, else `workorder_number` (unchanged). `exclude_wo_uuids = [uuid] if uuid else []`. |
| `aircraft_reg` | `aircraft.full_registration` (for example `9H-REG00163`), matching curated `aircraft_reg`. |
| `ata_chapter` | Raw XML `ataChapter` (`NN-NN`). Exposed in `parsed_context.ata_chapter`. |
| `position` | Header `position_info.position` (`records.py:616-617`, XML `<positionInfo><position>`), else the unique `component_changes` position when component changes are available in the mode. Normalised `UPPER(TRIM())`, then the alias map `{"AFTCARGO": "AFT", "AFT CARGO": "AFT", "ENG1": "#1", "ENG 1": "#1", "1": "#1", "ENG2": "#2", "ENG 2": "#2", "2": "#2"}`. Other values (`RHD`, `LWR RH`, `FWD`, ...) pass through unchanged and therefore do not match a focus position. Exposed in `parsed_context.position`. |
| `part_numbers` | Header `component.part_number` (`service.py:269`), `required_parts.part_number`, `part_keys`, and `component_changes` `part_off_number`/`part_on_number`, normalised `re.sub(r"[^A-Z0-9]", "", x.upper())`. Availability rules are the same as `_resolve_targets`: no component-change parts in `new_work_order` mode, nothing from a closed export before its export timestamp. `header_part_number` is kept separately (used for the out-of-scope rule). |
| `pma_wo_text` | New field, separate from the legacy `query_text` (which is unchanged). Built by `build_pma_wo_text(row, include_actions)` below. `include_actions = (mode == "new_work_order")`. In `historical_replay` all action text is excluded, matching the existing policy ("Replay excludes completed action and closing text from query and feature context", `service.py:364-367`) and avoiding a label leak (anchors are the replacement action text). |

```python
def build_pma_wo_text(row, *, include_actions: bool) -> str:
    """Mirror of curated.tf:145-172 (wo_embeddings.content). headline is ignored."""
    remarks = row.get("remarks") or ""
    entries = []
    for step in sorted(row.get("work_steps") or [], key=lambda s: s.get("sequence_number") or 0):
        desc = step.get("description") or ""
        actions = (step.get("actions") or []) if include_actions else []
        for action in (actions or [None]):            # LEFT JOIN: a step without actions yields one entry
            text = ((action or {}).get("action_text") or "")
            if (desc + text).strip() == "":            # WHERE TRIM(CONCAT(desc, action)) <> ''
                continue
            suffix = ("\n" + text) if text.strip() else ""
            entries.append((desc + suffix).strip())     # one entry per (step, action)
    body = "\n\n".join(entries)
    return (remarks + ("\n" if remarks else "") + body).strip()
```

Offline element order is not guaranteed (`ARRAY(SELECT ...)` has no ORDER BY), so the parity test compares the multiset of `\n\n`-separated entries plus the remarks prefix. T10 adds a unit test against 3 stored `wo_embeddings.content` rows copied into a fixture; T14 adds live test 1b over 20 random rows.

**Early reject.** Return without calling BigQuery when:
- `pma_wo_text` is empty and no focus part number is present → `no_reliable_prediction` / `empty_text`.
- The mode or `analysis_as_of` is invalid → the existing 422 errors.
- `aircraft_reg` missing: still predict; `current_tac` unavailable.

**`current_tac` (context only; never added to an interval).** Priority chain:
1. User-supplied `current_aircraft_tac` with `observed_at` ≤ 24 h old (existing rule, `service.py:385-395`). Source `user_supplied`.
2. `issue.tac` from the uploaded XML. Source `workorder_issue_tac`. Stale if the issue date is > 7 days before `analysis_as_of`.
3. Latest closed WO for the registration before `analysis_as_of`. Source `latest_closing_tac`, `stale=true` if older than 7 days, `counter_inconsistent=true` if `tac < max_tac_before_cutoff`.

```sql
-- pma_latest_closing_tac.sql  placeholders: {v_work_orders}
-- params: @aircraft_reg STRING, @analysis_as_of TIMESTAMP, @exclude_wo_uuid STRING ('' when the upload has no uuid; never NULL), @limit INT64 (pass 1)
WITH w AS (
  SELECT
    workorder_number,
    closing_aircraft_cycles AS tac,
    COALESCE(closing_ts, TIMESTAMP(closing_date)) AS observed_at
  FROM {v_work_orders}
  WHERE full_registration = @aircraft_reg
    AND closing_aircraft_cycles IS NOT NULL
    AND COALESCE(closing_ts, TIMESTAMP(closing_date)) < @analysis_as_of
    AND IFNULL(workorder_uuid, '') != @exclude_wo_uuid
)
SELECT
  workorder_number, tac, observed_at,
  MAX(tac) OVER () AS max_tac_before_cutoff
FROM w
ORDER BY observed_at DESC, tac DESC
LIMIT @limit
```

### 5.2 O2: scope gate (deterministic first, vote only for symptom-only WOs; no LLM)

```sql
-- pma_focus_components.sql  placeholders: {dim_focus_components}, {fct_replacement_events}; params: @limit (unused)
WITH aliases AS (
  SELECT r.component_key,
         ARRAY_AGG(DISTINCT REGEXP_REPLACE(UPPER(pn), r'[^A-Z0-9]', '') IGNORE NULLS) AS part_aliases
  FROM {fct_replacement_events} r, UNNEST([r.part_off_pn, r.part_on_pn]) AS pn
  WHERE r.component_key IN (SELECT component_key FROM {dim_focus_components})
    AND pn IS NOT NULL AND TRIM(pn) <> ''
  GROUP BY r.component_key
)
SELECT
  f.component_key,
  SPLIT(f.component_key, '|')[OFFSET(0)] AS part_number,
  SPLIT(f.component_key, '|')[SAFE_OFFSET(1)] AS position,
  REGEXP_REPLACE(UPPER(SPLIT(f.component_key, '|')[OFFSET(0)]), r'[^A-Z0-9]', '') AS part_key,
  IFNULL(a.part_aliases, []) AS part_aliases,
  f.freq_rank, f.replacement_count, f.aircraft_with_replacement
FROM {dim_focus_components} f
LEFT JOIN aliases a USING (component_key)
ORDER BY f.freq_rank
```

Live output: `2085M31G03|#1/#2` → `[2085M31G03]`; `473597-5|AFT` → `[4735975]`; `340-001-038-0|#1` → 5 aliases; `|#2` → 3 aliases; `45-0351-4|RH` → `[4503514]`. The repository also always includes `part_key` in the alias set.

**Gate logic** (`policy.resolve_scope`, first match wins):
1. **Deterministic part match.** `matches` = focus keys whose alias set intersects `part_numbers`.
   - a. `position` present and exactly one key in `matches` has that position → gate passes, basis `exact_pn_position`.
   - b. `position` present and no key in `matches` has it → `out_of_scope` / `position_not_in_focus_set` (for example a FWD `473597-5` detector).
   - c. `position` missing → run `lead_time_stats` for every key in `matches`. If all have n=0 → `no_reliable_prediction` / `no_lead_time_samples`, basis `exact_pn`. Otherwise → `no_reliable_prediction` / `ambiguous_position` with `component.candidates[]` (key, n). The vote never picks between positions.
2. **Header part number not in focus** (for example `62197301001` boiler, `820111000001` oven) → `out_of_scope` / `component_not_in_focus_set`, no embedding call.
3. **No focus part number** (symptom-only WO, the main case): embed and vote (§5.3).
   - The vote winner is accepted only if its part has a single focus position. A winner whose part has several focus positions (`2085M31G03`, `340-001-038-0`) → `ambiguous_position`. Engine keys are therefore reachable only through rule 1.
   - Vote fails and the WO named non-focus part numbers (for example in `required_parts`) → `out_of_scope` / `component_not_in_focus_set`; otherwise `no_reliable_prediction` / `no_confident_component_match`.
4. **Legacy `target_part_number`.** `AnalysisInput.__post_init__` stays static (no I/O): it accepts legacy `TARGET_PARTS` keys plus `settings.FOCUS_PART_FALLBACK` (the 6 focus part keys and aliases, a code constant refreshed when the curated set is rebuilt). Anything else is still 422 `out_of_scope_target`. The live focus check happens inside the predictor, which returns `out_of_scope` if the fallback is stale.

### 5.3 O3: embed and retrieve (symptom-only path, and WO evidence for every gated decision)

```sql
-- pma_embed_query.sql  placeholders: {embedding_endpoint} (allowlisted constant); params: @wo_text, @limit (unused)
WITH e AS (SELECT AI.EMBED(@wo_text, endpoint => '{embedding_endpoint}') AS v)
SELECT e.v.result AS embedding, e.v.status AS status
FROM e
```

One Vertex call. The repository asserts `status == ''` and `len(embedding) == settings.embedding_dim` (768), else `embedding_failed`. The vector is bound as `("query_embedding", "ARRAY<FLOAT64>", vec)`.

```sql
-- pma_anchor_neighbours.sql  placeholders: {replacement_anchor_embeddings}, {dim_reference_set}
-- params: @query_embedding ARRAY<FLOAT64>, @exclude_wo_uuids ARRAY<STRING> (never NULL elements), @analysis_as_of TIMESTAMP, @min_sim FLOAT64, @limit INT64
WITH anchors AS (
  SELECT
    a.replacement_wo_uuid,
    ANY_VALUE(a.component_key)           AS component_key,
    ANY_VALUE(a.aircraft_reg)            AS aircraft_reg,
    ANY_VALUE(a.replacement_tac)         AS replacement_tac,
    ANY_VALUE(a.content)                 AS content,
    ANY_VALUE(a.anchor_embedding.result) AS emb
  FROM {replacement_anchor_embeddings} a
  WHERE a.anchor_embedding.status = ''
    AND ARRAY_LENGTH(a.anchor_embedding.result) = 768
    AND a.replacement_wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
  GROUP BY a.replacement_wo_uuid
),
dated AS (
  SELECT replacement_wo_uuid, MIN(replacement_date) AS replacement_date,
         ANY_VALUE(replacement_wo_id) AS replacement_wo_id
  FROM {dim_reference_set}
  GROUP BY replacement_wo_uuid
),
scored AS (
  SELECT
    x.replacement_wo_uuid, d.replacement_wo_id, x.component_key, x.aircraft_reg,
    x.replacement_tac, d.replacement_date, SUBSTR(x.content, 1, 400) AS snippet,
    1 - ML.DISTANCE(x.emb, @query_embedding, 'COSINE') AS sim
  FROM anchors x
  JOIN dated d USING (replacement_wo_uuid)
  WHERE d.replacement_date < DATE(@analysis_as_of)
)
SELECT * FROM scored
WHERE sim >= @min_sim
ORDER BY sim DESC
LIMIT @limit
```

```sql
-- pma_wo_neighbours.sql  placeholders: {wo_embeddings}, {wo_workorders}
-- params: @query_embedding ARRAY<FLOAT64>, @exclude_wo_uuids ARRAY<STRING>, @analysis_as_of TIMESTAMP, @min_sim FLOAT64, @limit INT64
WITH scored AS (
  SELECT
    e.wo_uuid, e.wo_id, e.aircraft_reg, e.ata_chapter, e.tac,
    w.closing.date AS closing_date,
    SUBSTR(e.content, 1, 400) AS snippet,
    1 - ML.DISTANCE(e.embedding.result, @query_embedding, 'COSINE') AS sim
  FROM {wo_embeddings} e
  JOIN {wo_workorders} w ON w.workorder_uuid = e.wo_uuid
  WHERE e.embedding.status = ''
    AND e.wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
    AND COALESCE(w.closing.ts, TIMESTAMP(w.closing.date)) < @analysis_as_of
)
SELECT * FROM scored
WHERE sim >= @min_sim
ORDER BY sim DESC
LIMIT @limit
```

Both templates dry-run clean (12.7 MB and 30.4 MB). `QUALIFY` is not used here (BigQuery requires an analytic function for it).

- **Exclusions.** `@exclude_wo_uuids` contains the uploaded WO's uuid when it has one. Replay also filters on dates.
- **ATA filter.** Not applied: the offline ATA-4 join emptied 4 of 6 keys. `ata_chapter` is `NN-NN` text with 1 malformed `05` row; it is returned for display only.

**Vote** (`policy.vote_component`):
- Over anchor neighbours (K = `PMA_ANCHOR_K`, `sim >= PMA_ONLINE_ANCHOR_SIM_THRESHOLD`): per key `support` = count, `score` = sum of `sim` (divided by `sqrt(anchor_count[key])` when `PMA_VOTE_NORMALISATION=sqrt`), `share` = score / total score.
- Passes when `support >= PMA_MIN_VOTE_SUPPORT` and `share >= PMA_MIN_VOTE_SHARE`, subject to the single-position rule in §5.2 step 3.
- `top_sim` = highest `sim` for the winning key; reported in `component.vote`.
- Class prior: AFT has 45% and RH 28% of anchors. T14 evaluates `none` vs `sqrt` normalisation.

### 5.4 O4: lead-time statistics and precursor evidence

```sql
-- pma_lead_time_stats.sql  placeholders: {fct_lead_time_samples}, {fct_replacement_events}
-- params: @component_key STRING, @analysis_as_of TIMESTAMP, @exclude_wo_uuids ARRAY<STRING>, @limit (unused)
-- Always returns exactly one row. n=0 -> all stats NULL.
WITH repl_dates AS (
  SELECT replacement_wo_uuid, MIN(replacement_date) AS replacement_date
  FROM {fct_replacement_events}
  GROUP BY replacement_wo_uuid
),
same_key_repl AS (
  SELECT DISTINCT component_key, replacement_wo_uuid AS wo_uuid
  FROM {fct_replacement_events}
),
s AS (
  SELECT
    t.*,
    r.replacement_date,
    k.wo_uuid IS NOT NULL AS precursor_is_same_key_replacement
  FROM {fct_lead_time_samples} t
  JOIN repl_dates r ON r.replacement_wo_uuid = t.replacement_wo_uuid
  LEFT JOIN same_key_repl k
    ON k.component_key = t.component_key AND k.wo_uuid = t.precursor_wo_uuid
  WHERE t.component_key = @component_key
    AND r.replacement_date < DATE(@analysis_as_of)
    AND t.replacement_wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
    AND t.precursor_wo_uuid  NOT IN UNNEST(@exclude_wo_uuids)
),
q AS (
  SELECT
    PERCENTILE_CONT(lead_cycles, 0.5) OVER () AS p50,
    PERCENTILE_CONT(lead_cycles, 0.9) OVER () AS p90
  FROM s
  LIMIT 1
)
SELECT
  @component_key AS component_key,
  COUNT(s.lead_cycles) AS n,
  COUNT(DISTINCT s.aircraft_reg) AS aircraft_n,
  COUNT(DISTINCT s.replacement_wo_uuid) AS replacement_n,
  MIN(s.lead_cycles) AS lead_min,
  MAX(s.lead_cycles) AS lead_max,
  ANY_VALUE(q.p50) AS lead_p50,
  ANY_VALUE(q.p90) AS lead_p90,
  AVG(s.lead_cycles) AS lead_mean,
  STDDEV_SAMP(s.lead_cycles) AS lead_sd,
  SAFE_DIVIDE(STDDEV_SAMP(s.lead_cycles), AVG(s.lead_cycles)) AS cv,
  SAFE_DIVIDE(COUNTIF(s.precursor_is_same_key_replacement), COUNT(s.lead_cycles)) AS replacement_interval_share,
  COUNTIF(s.precursor_wo_uuid IN (SELECT replacement_wo_uuid FROM s)) AS chained_sample_n,
  ARRAY_AGG(DISTINCT s.replacement_wo_uuid IGNORE NULLS) AS replacement_wo_uuids
FROM s LEFT JOIN q ON TRUE
```

Live, `analysis_as_of=2026-09-23`: `473597-5|AFT` → n 27, aircraft_n 24, replacement_n 27, p50 356.0, p90 1968.2, CV 1.2572, share 0.9259, chained 2. `2085M31G03|#1` → one row, n 0, all stats NULL, empty array. The repository maps n=0 to `no_lead_time_samples`; it must not use the runner's `NO_MATCH` status as the n=0 signal.

```sql
-- pma_precursor_evidence.sql  placeholders: {fct_lead_time_samples}, {adjudicated_precursors}, {fct_replacement_events}
-- params: @component_key STRING, @analysis_as_of TIMESTAMP, @exclude_wo_uuids ARRAY<STRING>, @neighbour_wo_uuids ARRAY<STRING>, @limit INT64
WITH repl_dates AS (
  SELECT replacement_wo_uuid, MIN(replacement_date) AS replacement_date
  FROM {fct_replacement_events}
  GROUP BY replacement_wo_uuid
),
adj AS (
  SELECT replacement_wo_uuid, precursor_wo_uuid, reason, precursor_text
  FROM {adjudicated_precursors}
  WHERE component_key = @component_key AND verdict = 'symptom'
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY replacement_wo_uuid, precursor_wo_uuid ORDER BY llm_conf DESC, sim DESC) = 1
)
SELECT
  t.component_key, t.aircraft_reg, t.precursor_wo_id, t.precursor_wo_uuid, t.precursor_tac,
  t.replacement_wo_uuid, t.replacement_tac, r.replacement_date, t.lead_cycles, t.sim,
  adj.reason,
  SUBSTR(adj.precursor_text, 1, 400) AS precursor_snippet,
  t.precursor_wo_uuid IN UNNEST(@neighbour_wo_uuids) AS is_neighbour_hit
FROM {fct_lead_time_samples} t
JOIN repl_dates r ON r.replacement_wo_uuid = t.replacement_wo_uuid
LEFT JOIN adj
  ON adj.replacement_wo_uuid = t.replacement_wo_uuid AND adj.precursor_wo_uuid = t.precursor_wo_uuid
WHERE t.component_key = @component_key
  AND r.replacement_date < DATE(@analysis_as_of)
  AND t.replacement_wo_uuid NOT IN UNNEST(@exclude_wo_uuids)
  AND t.precursor_wo_uuid  NOT IN UNNEST(@exclude_wo_uuids)
ORDER BY is_neighbour_hit DESC, t.sim DESC
LIMIT @limit
```

Evidence rows are exactly the lead samples (27 for AFT, all with a `reason`), date-filtered in SQL. No Python post-filter. `llm_conf` is not exposed in the response (review #6).

### 5.5 O5: decision and interval (no tiers, no due TAC)

`policy.decide(scope, vote, stats)`, first match wins:

| # | Condition | Decision / reason |
|---|---|---|
| 1 | Gate did not pass (§5.2/§5.3) | as returned by the gate |
| 2 | `n == 0` | `no_reliable_prediction` / `no_lead_time_samples` |
| 3 | `n < PMA_MIN_SAMPLE` (5) | `no_reliable_prediction` / `insufficient_samples` |
| 4 | `replacement_interval_share > PMA_MAX_REPLACEMENT_INTERVAL_SHARE` (0.5) | `no_reliable_prediction` / `samples_not_symptom_to_replacement`. If `PMA_SHOW_SUPPORTING_INTERVAL=true` (OQ6), attach `supporting_interval` (basis `consecutive_replacement_closing_tac_interval`, n, aircraft, p50, p90, min, max). Never mapped into `timing`. |
| 5 | otherwise | `historical_interval` with `interval` (basis `closing_tac_to_closing_tac_interval`) |

`PMA_MIN_SAMPLE` is a display rule (below 5 samples percentiles are not shown), not a confidence claim.

**`evidence_support`** (always present when stats ran): `sample_size`, `independent_aircraft` (primary support count), `distinct_replacement_events`, `chained_sample_n`, `cv`, `replacement_interval_share`. **`match`**: `gate_basis`, `vote.{support, share, top_sim}` (vote basis only). There is no `level` field.

**Always-on limitations**: `not_calibrated`, `outcome_selected_sample` (no event-free/censored cases), `no_installation_eligibility_check`, `closing_tac_basis` (issue TAC is missing for 99.5% of WOs), plus `in_sample_curated_artifacts` in replay, `embedding_provenance_unverified` while the label is absent, `current_tac_stale` / `counter_inconsistent` when set.

Today: `473597-5|AFT` → row 4. `45-0351-4|RH` → row 3. Engine keys → row 2. Row 5 is unreachable on current data.

### 5.6 O6: explainable output and prediction log

- `PredictionResult` (§6) is built by `prediction/service.py`; `analyze_xml` maps it (§6.1).
- **Evidence list:** top 5 anchor neighbours (`similar_past_replacement`: WO id, date, key, sim, snippet), top 5 lead samples (`adjudicated_precursor`: lead, reason, snippet; neighbour hits first), top 3 `wo_embeddings` neighbours (`similar_past_workorder`). Titles unique (the frontend keys on `title`).
- **`to_dict()` emits only JSON primitives** (ISO-8601 strings for `date`/`datetime`, float, int, str, bool, None, lists, dicts). The result is stored in ADK session state (`workorder_upload.py:137-140`), and live BigQuery `date`/`datetime` values have broken this before (`evidence_branches.py:246-249`).
- **Prediction log.** One structured JSON record per request via `google.cloud.logging` structured payload (fallback: stdlib logger) with labels `{"type": "agent_telemetry", "service_name": "pma-agent", "event": "pma_prediction"}` so the existing sink filter (`variables.tf:35`) matches. Payload: `request_id`, `wo_id`, `sha256(pma_wo_text)`, the full `pma` block, per-table `creation_time`, `embedding_endpoint`, `settings_hash`, `bq_job_ids`, `total_bytes_billed`, `latency_ms`. No raw WO text. T16 verifies that the record reaches the sink on Agent Runtime; if not, OQ11 decides on a dedicated sink.

```sql
-- pma_curated_build_info.sql  placeholders: {curated_information_schema_tables}, {curated_information_schema_table_options}; params: @limit (unused)
SELECT t.table_name, t.creation_time, o.option_value AS labels
FROM {curated_information_schema_tables} t
LEFT JOIN {curated_information_schema_table_options} o
  ON o.table_name = t.table_name AND o.option_name = 'labels'
WHERE t.table_name IN ('dim_focus_components','fct_replacement_events','dim_reference_set','replacement_anchor_embeddings','wo_embeddings','adjudicated_precursors','fct_lead_time_samples')
ORDER BY t.table_name
```

**Build consistency (fail closed).** `data_source_unavailable` (detail `curated_build_inconsistent`) if any of the 7 tables is missing, if `max(creation_time) - min(creation_time) > PMA_BUILD_MAX_SPREAD_S` (3,600 s; today 353 s), or if a downstream table is older than `fct_replacement_events`. Guards against reading a half-refreshed CTAS rebuild.

### 5.7 Replay, leakage and chat defaults

- Always exclude the uploaded WO's uuid everywhere (when it has one).
- `historical_replay`: anchors filtered by `dim_reference_set.replacement_date`, WO neighbours by `wo_workorders.closing`, lead samples and evidence by `fct_replacement_events.replacement_date`; `pma_wo_text` excludes all action text.
- `fct_lead_time_samples` was mined on all data without a split, so replay is partly in-sample: limitation `in_sample_curated_artifacts`.
- **Chat defaults:** `analysis_as_of=now()` and closed WOs → `historical_replay`. A corpus upload in chat therefore replays with no effective cut-off (fully in-sample), and `current_tac` from `latest_closing_tac` is always stale (data ends 2026-09-13). Eval expectations account for this.

### 5.8 Parameters (`pm_agent/prediction/settings.py`, read from env at call time)

| Env var | Initial value | Justification |
|---|---|---|
| `PMA_PREDICTION_ENABLED` | `false` | Feature flag. Enabled only after IAM apply **and** the §8.4 item 11 go-live gate. Set in `service.tf` env (T06). |
| `PMA_BQ_PROJECT` | unset → `pm_agent.config.project_id()` evaluated at call time | `GOOGLE_CLOUD_PROJECT` is platform-injected on Agent Runtime (`service.tf:44`). |
| `PMA_CURATED_DATASET` | `pma_agent_curated` | Allowlisted. |
| `PMA_ANALYTICS_DATASET` | `pma_agent_analytics` | Allowlisted. |
| `PMA_EMBEDDING_ENDPOINT` | `text-embedding-005` (allowlist: only this) | Must match offline vectors (768-d). |
| `PMA_EMBEDDING_DIM` | `768` | All 4,639 + 1,720 vectors are 768-d. |
| `PMA_ONLINE_ANCHOR_SIM_THRESHOLD` | `0.84` (**re-tuned 2026-09-24 after the 20-part rebuild**: 2561 anchors, 1720 before the 2026-03-01 backtest cut-off; 0.84 → 3.8% false gates, 11.0% coverage; 0.80 → 23.0%; 0.85 → 0.6%, 7.2% coverage. Earlier value `0.80`, **set by T16 from the T14 backtest 2026-09-24** on the 718-anchor build: lowest threshold with symptom-only false-gate rate ≤ 5%; 0.80 → 2.2% false gates, 56.4% coverage; 0.75 → 6.0%; 0.85 → 0.6%, 20.8% coverage; in-sample. Was `0.85` provisional) | At 0.80 about 31% of non-focus WOs pass the gate (1,242/3,947; mostly to RH/AFT); at 0.85, 7.5%. The old "quartiles .844/.862/.885" came from `scored_precursors`, already filtered at ≥ 0.80 and fan-out inflated (deduplicated: .837/.858/.880), so they do not justify 0.80. Symptom-only queries score lower (RH probe max 0.808), so 0.85 will reject most RH symptom-only WOs; T14 decides. |
| `PMA_ANCHOR_K` | `20` | 718 anchors; smallest key 38. Class imbalance (AFT 45%, RH 28%) noted; see normalisation. |
| `PMA_VOTE_NORMALISATION` | `none` (`none`\|`sqrt`) | T14 compares. |
| `PMA_MIN_VOTE_SUPPORT` | `3` | Avoids gating on one lucky anchor. |
| `PMA_MIN_VOTE_SHARE` | `0.5` | Majority among neighbours above threshold. |
| `PMA_WO_NEIGHBOUR_K` | `10` | Evidence only (3 shown). |
| `PMA_WO_NEIGHBOUR_SIM_THRESHOLD` | `0.80` | Evidence only; not a gate. |
| `PMA_MIN_SAMPLE` | `5` | Display rule. Suppresses RH (n=1). |
| `PMA_MAX_REPLACEMENT_INTERVAL_SHARE` | `0.5` | Above this the samples are not symptom-to-replacement (§5.5 row 4). |
| `PMA_SHOW_SUPPORTING_INTERVAL` | `true` (OQ6, flipped 2026-09-24 - Option B) | Whether to attach the labelled consecutive-replacement interval. |
| `PMA_SHOW_PROJECTED_WINDOW` | `true` (Option B, added 2026-09-24) | Whether to project `supporting_interval` onto the aircraft's own last replacement TAC. No-op unless `supporting_interval` was attached. |
| `PMA_TAC_STALE_DAYS` | `7` | Snapshot ends 2026-09-13. |
| `PMA_FOCUS_CACHE_TTL_S` | `600` | Focus and build info change only on rebuild. |
| `PMA_BUILD_MAX_SPREAD_S` | `3600` | §5.6 build consistency. |
| `PMA_EVIDENCE_LIMIT` | `5` | UI readability. |
| `FOCUS_PART_FALLBACK` | code constant: `2085M31G03`, `4735975`, `3400010380`, `3400010360`, `3400010280`, `3400010260`, `3400010390`, `4503514` | Static `AnalysisInput` validation (§5.2 step 4). |
| `PMA_QUERY_INCLUDE_ACTIONS` | `true` (recommendation, added 2026-09-24) | Match `wo_embeddings` on description **and** action text per the user's 2026-09-24 request, not description-only. |
| `PMA_REC_NEIGHBOUR_K` | `100` (recommendation, added 2026-09-24) | Neighbour search width over `wo_embeddings` feeding the recommendation vote. |
| `PMA_REC_NEIGHBOUR_MIN_SIM` | `0.70` (recommendation, added 2026-09-24) | Similarity floor for a `wo_embeddings` neighbour to count toward the recommendation vote. Deliberately looser than `PMA_ONLINE_ANCHOR_SIM_THRESHOLD` (0.84): this is a recommendation signal, not the scope gate. |
| `PMA_REC_MIN_SAMPLE_HIGH` | `8` (recommendation, added 2026-09-24) | `confidence.level="high"` sample-size floor. |
| `PMA_REC_MAX_CV` | `0.5` (recommendation, added 2026-09-24) | `confidence.level="high"` coefficient-of-variation ceiling on the lead-time sample. |
| `PMA_REC_STRONG_SIM` | `0.80` (recommendation, added 2026-09-24) | `confidence.level="high"` mean-neighbour-similarity floor. |
| `PMA_SHOW_RECOMMENDATION` | `true` (recommendation, added 2026-09-24) | Toggles the `recommendation` block. No-op without a resolved component. |

`MODEL` in `config.py` is not touched.

---

## 6. Response contract

### 6.1 Changes to the existing `analyze_xml` response (HTTP and chat share it)

- **Legacy keys keep their current values unless `pma.decision == "historical_interval"`.** Current `prediction.status` values (`out_of_scope`, `missing_input`, `model_unavailable`, `service.py:595-631`) are unchanged.
- When `pma.decision == "historical_interval"`: `prediction.status = "historical_interval_only"`; `timing.status = "historical_interval_only"`; `timing.estimable_quantiles = {"p50", "p90", "unit": "aircraft_flight_cycles", "basis": "closing_tac_to_closing_tac_interval"}`; `timing.forecast = null`.
- **`replacement_recommendation.due_counter` is always `null`** and `status` stays unavailable with reason `no_validated_decision_policy` (§8.5: a deadline needs a reviewed decision policy).
- **New key `pma`** (§6.2-6.3). `predictor is None` (flag off or construction failure) → `pma = PredictionResult.disabled(reason).to_dict()` with `prediction_disabled` or `data_source_unavailable`.
- **`parsed_context`** gains `ata_chapter` and `position` (additive).
- `targets` stays legacy-only; `manual_evidence` keeps working (`service.py:217` uses `.get`).

### 6.2 `pma` block, decision `no_reliable_prediction` with supporting interval (today's AFT case, `PMA_SHOW_SUPPORTING_INTERVAL=true`)

```json
{
  "contract_version": "pma-online-v1",
  "decision": "no_reliable_prediction",
  "reason": "samples_not_symptom_to_replacement",
  "aircraft": "9H-REG00163",
  "workorder_id": "PMA-FIXTURE-0001",
  "analysis_as_of": "2026-09-23T10:00:00Z",
  "mode": "new_work_order",
  "component": {
    "component_key": "473597-5|AFT",
    "part_number": "473597-5",
    "position": "AFT",
    "candidates": [],
    "match": {"gate_basis": "anchor_vote", "vote": {"support": 18, "share": 1.0, "top_sim": 0.91}}
  },
  "interval": null,
  "supporting_interval": {
    "basis": "consecutive_replacement_closing_tac_interval",
    "unit": "aircraft_flight_cycles",
    "label": "Observed interval between consecutive replacements (closing TAC to closing TAC). Not a forecast.",
    "p50": 356.0, "p90": 1968.2, "min": 4, "max": 3158
  },
  "evidence_support": {"sample_size": 27, "independent_aircraft": 24, "distinct_replacement_events": 27, "chained_sample_n": 2, "cv": 1.26, "replacement_interval_share": 0.93},
  "current_tac": {"value": 19107, "source": "latest_closing_tac", "observed_at": "2026-09-06T03:14:00Z", "stale": true, "counter_inconsistent": false},
  "evidence": [
    {"kind": "similar_past_replacement", "title": "Replacement WO 190529876 (2026-05-02)", "wo_id": "190529876", "date": "2026-05-02", "component_key": "473597-5|AFT", "sim": 0.91, "detail": "AFT CARGO SMOKE DET LOOP B INOP ..."},
    {"kind": "adjudicated_precursor", "title": "Earlier WO 190736258 -> replacement (6 cycles later)", "wo_id": "190736258", "sim": 0.926, "lead_cycles": 6, "detail": "AFT CARGO FIRE LOOP A - INOP ..."}
  ],
  "missing": ["symptom_to_replacement_samples"],
  "action": "Recommend-only. No reliable lead time: past samples are mostly intervals between consecutive replacements.",
  "limitations": ["not_calibrated", "outcome_selected_sample", "no_installation_eligibility_check", "closing_tac_basis", "embedding_provenance_unverified", "current_tac_stale"],
  "provenance": {"curated_dataset": "pma_agent_curated", "curated_tables_created": {"fct_lead_time_samples": "2026-09-23T15:26:30Z", "...": "..."}, "embedding_endpoint": "text-embedding-005", "settings_hash": "...", "bq_job_ids": ["..."]}
}
```

For `decision == "historical_interval"` the same shape carries `interval = {"basis": "closing_tac_to_closing_tac_interval", "calibrated": false, "unit": "aircraft_flight_cycles", "p50", "p90", "min", "max"}` and `supporting_interval = null`. Values are illustrative; tests assert structure.

### 6.2b `recommendation` block (added 2026-09-24, further §8.5 override - see "T00 decisions")

Independent of `decision`/`reason`/`interval`/`supporting_interval` above: a top-level `pma.recommendation` key, `null` when `PMA_SHOW_RECOMMENDATION=false`, no component was resolved, or the neighbour vote/lead-time lookup failed. Same AFT example as §6.2 (values illustrative; tests assert structure and the cross-field identity `tac_p50 = reference_tac + lead_tac_p50`, similarly for p90/p95):

```json
{
  "decision": "recommendation",
  "basis": "similar_workorders",
  "component_key": "473597-5|AFT",
  "reference_tac": 18660,
  "lead_tac_p50": 381, "lead_tac_p90": 1891, "lead_tac_p95": 2340,
  "tac_p50": 19041, "tac_p90": 20551, "tac_p95": 21000,
  "confidence": {"level": "medium", "n": 16, "mean_sim": 0.76, "cv": 1.1},
  "action": "recommend_inspection_or_part_planning",
  "evidence_support": {"sample_size": 16, "neighbour_k": 100, "neighbour_min_sim": 0.70}
}
```

Rendered with the fixed `"**Recommendation:"` header (§6.5) and this JSON fenced as ```json; `tests/eval/pma_contract_metric.py` whitelists TAC/cycles figures inside that header-to-fence span without hardcoding the prose, since the sentence wording is `chat.py`'s to choose.

### 6.3 Fallback contracts

```json
{"contract_version":"pma-online-v1","decision":"no_reliable_prediction","reason":"no_lead_time_samples",
 "component":{"component_key":null,"candidates":[{"component_key":"2085M31G03|#1","n":0},{"component_key":"2085M31G03|#2","n":0}],"match":{"gate_basis":"exact_pn"}},
 "interval":null,"supporting_interval":null,"evidence_support":{"sample_size":0},
 "missing":["lead_time_samples_for_component"],"evidence":[],"limitations":["..."],"provenance":{"...":"..."}}
```

```json
{"contract_version":"pma-online-v1","decision":"out_of_scope","reason":"component_not_in_focus_set",
 "reason_detail":{"part_numbers":["62197301001"],"focus_components":["2085M31G03|#1","473597-5|AFT","..."]},
 "interval":null,"supporting_interval":null,"evidence":[],"missing":[],"provenance":{"...":"..."}}
```

**`reason` enum and `missing[]`:**

| `reason` | decision | `missing[]` |
|---|---|---|
| `empty_text` | no_reliable_prediction | `workorder_text` |
| `embedding_failed` | no_reliable_prediction | `query_embedding` |
| `embedding_incompatible` | no_reliable_prediction | `compatible_embedding_space` |
| `no_confident_component_match` | no_reliable_prediction | `component_match` |
| `ambiguous_position` | no_reliable_prediction | `component_position` |
| `component_not_in_focus_set` | out_of_scope | none |
| `position_not_in_focus_set` | out_of_scope | none |
| `aircraft_type_not_in_scope` | out_of_scope | none (reserved, OQ13) |
| `no_lead_time_samples` | no_reliable_prediction | `lead_time_samples_for_component` |
| `insufficient_samples` | no_reliable_prediction | `min_sample_size` |
| `samples_not_symptom_to_replacement` | no_reliable_prediction | `symptom_to_replacement_samples` |
| `prediction_disabled` | no_reliable_prediction | `feature_flag` |
| `data_source_unavailable` | no_reliable_prediction | `bigquery_access` (IAM, timeout, construction failure, inconsistent build) |

`different_component_matched` is a frontend-only adapter reason (§6.4), not emitted by the backend.

### 6.4 Frontend reconciliation (`frontend/src/domain.ts`) — mock-only in this plan

| `pma` field | UI |
|---|---|
| `decision=historical_interval` | New `Outlook` variant `{status:"historical_interval", quantiles:{p50, p90}, basis, sampleSize, independentAircraft, componentKey, evidence, limitations}`. **No `cyclesPerDay`, no calendar window, no "Remaining cycles" label.** Rendered as "Historical interval between closing TACs (not a forecast)". Do not reuse `illustrative`. |
| `supporting_interval` | Rendered inside the `unavailable` card as supporting evidence with its label; no dates. |
| `no_reliable_prediction` / `out_of_scope` | `{status:"unavailable", reason, missing, evidence}` (distinguish `no_match` from `unavailable` as the frontend handover requires). |
| `evidence[]` | `Evidence{title, source: kind, detail: date · sim · snippet}` |
| One component per UI request | The adapter compares `pma.component.component_key` with the selected component and shows `unavailable` / `different_component_matched` when they differ. |
| Samples | Mock only: `473597-5 AFT`, `2085M31G03 #1` (no samples), boiler (out of scope). |

A live adapter needs a hosted HTTP backend, `ALLOW_ORIGINS`, a sample-listing endpoint and an injection seam in `App.tsx:13` (frontend handover items 1-6). That is out of scope until OQ12 is answered.

### 6.5 Fixed user-facing strings (T13 renders them, T11 asserts them)

| Case | String |
|---|---|
| `historical_interval` header | `Observed historical interval (not a forecast)` |
| interval line | `Between closing TACs: p50 {p50} cycles, p90 {p90} cycles (n={n}, {aircraft} aircraft).` |
| supporting interval (superseded by (a), kept for fakes/back-compat tests) | `Observed interval between consecutive replacements (not a forecast): p50 {p50}, p90 {p90} cycles (n={n}).` |
| no prediction | `No reliable prediction: {reason_text}` |
| out of scope | `Out of scope for PMA prediction: {reason_text}` |
| stale TAC, no window | `Current aircraft TAC is stale (last seen {observed_at}); no due TAC is given.` |
| component | `Matched component: {component_key} ({gate_basis}).` |

**Option B fixed strings (added 2026-09-24, T00 decisions).** Rendered whenever `PMA_SHOW_SUPPORTING_INTERVAL`/`PMA_SHOW_PROJECTED_WINDOW` are on and the relevant `pma` field is present. All cycle/TAC figures are rendered as integers, rounded half-up (`policy.round_cycles`; 380.5 → 381, like BigQuery `ROUND`). Both renderers use the same line builders in `chat.py`. When a window is present, section (d) of the composed answer uses the gap text "Failure probability, remaining life and a replacement deadline are not given; the projected window above is a fleet pattern, not a forecast or a deadline, and closing TAC is not the current counter or component age."

| Case | String |
|---|---|
| (a) supporting interval | `Fleet pattern (not a forecast): this component was replaced again after p50 {p50} / p90 {p90} cycles (n={n}, {aircraft} aircraft).` |
| (b) projected window | `Projected window for this aircraft (fleet pattern, not a forecast): last replaced at TAC {last_tac} ({last_date}); if the pattern repeats, next replacement around TAC {tac_p50}–{tac_p90}.` |
| (c) window position - `before_p50` | `Latest known TAC is {since} cycles after that replacement, before the fleet median.` |
| (c) window position - `between_p50_p90` | `Latest known TAC is {since} cycles after that replacement, past the fleet median but inside p90.` |
| (c) window position - `past_p90` | `Latest known TAC is {since} cycles after that replacement, beyond the fleet p90; replacement is overdue against the fleet pattern.` |
| (d) no prior replacement (limitation `no_prior_replacement_on_aircraft`; not rendered when the lookup failed or `PMA_SHOW_PROJECTED_WINDOW=false`) | `No earlier replacement of this component on this aircraft is recorded, so no aircraft-specific window is given.` |
| (e) stale TAC, with window | `Current aircraft TAC is stale (last seen {observed_at}); the window is not adjusted for cycles flown since.` |

Every line naming "Projected window" always carries "not a forecast" (asserted by `pma_contract_metric.py`). The old sentences "No configured target part could be resolved at this cutoff" and "BigQuery and the knowledge base were not queried" are removed from the analyzed path when `pma` is present.

**Recommendation fixed string (added 2026-09-24, further §8.5 override - see "T00 decisions" and §6.2b).** Rendered whenever `PMA_SHOW_RECOMMENDATION` is on and `pma.recommendation` is not `null`, independent of `decision`/`reason`.

| Case | String |
|---|---|
| recommendation header | `**Recommendation:` followed by a fenced ```json block with the §6.2b shape (`decision`, `basis`, `component_key`, `reference_tac`, `lead_tac_p50/p90/p95`, `tac_p50/p90/p95`, `confidence`, `action`) |

`pma_contract_metric.py` whitelists every TAC/cycles figure between the header and the end of its JSON fence, and separately asserts the fence contains `decision: "recommendation"` and a `basis` from the fixed enum - it does not assert the prose between the header and the fence, which is `chat.py`'s to choose.

---

## 7. Code change list

Paths relative to `/Users/kulagas/caveman-agent`. **Model settings are preserved everywhere.**

**Repository Protocol** (`pm_agent/prediction/contracts.py`, frozen in T04):

```python
class Repository(Protocol):
    def curated_build_info(self) -> BuildInfo: ...
    def focus_components(self) -> list[FocusComponent]: ...
    def embed_query(self, wo_text: str) -> list[float]: ...
    def anchor_neighbours(self, emb: list[float], as_of: datetime, exclude: list[str], k: int, min_sim: float) -> list[Neighbour]: ...
    def wo_neighbours(self, emb: list[float], as_of: datetime, exclude: list[str], k: int, min_sim: float) -> list[Neighbour]: ...
    def lead_time_stats(self, component_key: str, as_of: datetime, exclude: list[str]) -> LeadStats: ...
    def precursor_evidence(self, component_key: str, as_of: datetime, exclude: list[str], neighbour_uuids: list[str], limit: int) -> list[PrecursorEvidence]: ...
    def latest_closing_tac(self, reg: str, as_of: datetime, exclude_uuid: str) -> CurrentTac | None: ...
    @property
    def job_log(self) -> list[JobInfo]: ...   # job_id, total_bytes_billed per call
```

Errors are raised as `DataSourceUnavailable` / `EmbeddingFailed` (defined in contracts). `pm_agent/prediction/__init__.py` stays docstring-only; consumers import `pm_agent.prediction.service.default_predictor` and `pm_agent.prediction.contracts.*` directly.

| File | Op | Change | Task |
|---|---|---|---|
| `pm_agent/sub_agents/bq_analytics/sql/pma_*.sql` (8 files) | create | §5 SQL exactly as written (placeholders, no project id) | T01 |
| `pm_agent/sub_agents/bq_analytics/queries.py` | modify | (a) `_ALLOWED_TABLES` becomes `{pma_agent_analytics: {wo_workorders, faa_sdr_wo_parts, v_work_orders}, pma_agent_curated: {dim_focus_components, fct_replacement_events, dim_reference_set, replacement_anchor_embeddings, wo_embeddings, adjudicated_precursors, fct_lead_time_samples, INFORMATION_SCHEMA.TABLES, INFORMATION_SCHEMA.TABLE_OPTIONS}}`; `_ALLOWED_DATASETS` = both keys. (b) `table(name, *, dataset=None)` (default `self.dataset`, backward compatible). (c) `__init__` unchanged in signature, accepts either dataset. (d) Parameter triples accept `"ARRAY<FLOAT64>"`/`"ARRAY<STRING>"` → `ArrayQueryParameter(name, inner, value)`; `None` values rejected with `ValueError`. (e) `_RunOutcome` gains `job_id: str \| None` and `total_bytes_billed: int \| None`. (f) `_ALLOWED_TEMPLATE_CONSTANTS = {"embedding_endpoint": {"text-embedding-005"}}` validated before substitution. Existing `run()` callers keep working. | T02 |
| `pm_agent/prediction/__init__.py` | create | Docstring only | T04 |
| `pm_agent/prediction/settings.py` | create | Frozen dataclass, `from_env()` evaluated at call time, `settings_hash`, `FOCUS_PART_FALLBACK`, `POSITION_ALIASES` | T04 |
| `pm_agent/prediction/contracts.py` | create | `PredictionInput`, `FocusComponent`, `Neighbour`, `LeadStats`, `PrecursorEvidence`, `CurrentTac`, `BuildInfo`, `JobInfo`, `PredictionResult` (`to_dict` JSON-primitive only, `disabled(reason)`), `Repository`/`Predictor` Protocols, reason enum, exceptions | T04 |
| `pm_agent/prediction/policy.py` | create | Pure functions: `resolve_scope`, `vote_component`, `decide`, `resolve_current_tac`, `check_build`, `build_evidence`, `limitations` | T04 |
| `pm_agent/prediction/repository.py` | create | `BigQueryPredictionRepository(runner: QueryRunner, settings: PredictionSettings)` composes (does not subclass) the runner; fills placeholders via `runner.table(name, dataset=...)`; `limit=` for K; TTL cache for focus and build info; never binds NULL; maps `Forbidden`/timeout to `DataSourceUnavailable`; NULL stats → n=0 | T08 |
| `pm_agent/prediction/service.py` | create | `PrecursorPredictor(repository, settings).predict()` orchestrating §5.2-5.6; prediction log; `default_predictor()` returns None when disabled, lazily imports the repository, and returns a predictor that yields `data_source_unavailable` if construction fails (for example `DefaultCredentialsError`) | T09 |
| `pm_agent/workorders/service.py` | modify | `build_pma_wo_text`; `ata_chapter`/`position` in `parsed_context`; part-number collection (§5.1); `WorkOrderAnalysisService(history_provider=None, *, predictor=None)` keyword-only; `pma` key; legacy keys mapped per §6.1; `timing.status="historical_interval_only"` when quantiles filled; `AnalysisInput` accepts `FOCUS_PART_FALLBACK`; `manual_evidence` uses `TARGET_PARTS.get(...)`; legacy `query_text` unchanged | T10 |
| `pm_agent/workorders/chat.py` | modify | Module-level `default_predictor` import; pass `predictor=default_predictor()` at `:305`; `render_uploaded_workorder_summary` (`:380-387`, `:445-448`) uses `pma.component` and §6.5 strings; `render_chat_upload` `:540-542` legacy cleanup | T13a |
| `pm_agent/fast_api_app.py` | modify | Pass `predictor=default_predictor()` at `:227`; construction failure → `data_source_unavailable` fallback in `pma`, never 500/503 | T13a |
| `pm_agent/nodes/workorder_upload.py` | modify | `_build_evidence_request`: add `PartCandidate(..., roles=("pma_component",))` for `pma.component` when resolved; `AircraftContext.position` from the resolved position, else `parsed_context.position` | T13b |
| `pm_agent/nodes/evidence_branches.py` | modify | Section (d) (`:391-408`) renders from `(upload_result.get("analysis") or {}).get("pma")`; `_run_bq_tools` unchanged in code but now receives the pma candidate | T13b |
| `pm_agent/workorders/artifacts.py` | note | `analyze_current_session_artifact` callers must pass a predictor-enabled service; otherwise `pma` reports `prediction_disabled` (documented, no code change) | T16 |
| `pm_agent/sub_agents/bq_analytics/agent.py` | modify | `INSTRUCTION` only (`:51-72`): list analytics `v_*` views; curated tables excluded unless OQ10 = yes (then add a second dataset constant, the 403 caveat and the "lead_cycles is not a forecast" rule). No model change. | T12 |
| `deployment/terraform/single-project/iam.tf` | modify | `google_bigquery_dataset_iam_member.app_sa_curated_data_viewer` (`roles/bigquery.dataViewer`) on `google_bigquery_dataset.curated[0]`, `count = local.create_curated_dataset ? 1 : 0` | T06 |
| `deployment/terraform/single-project/service.tf` | modify | Add `env { name = "PMA_PREDICTION_ENABLED" value = var.pma_prediction_enabled }` (default `"false"`) and the variable in `variables.tf` | T06 |
| `deployment/terraform/single-project/curated.tf` | modify (T17a-c, OQ6) | a: dedup `dim_reference_set` on `(replacement_wo_uuid, component_key)`, partition top-K and join anchors on both columns; b: exclude precursors that are same-key replacement WOs (and cross-position precursors such as FWD for AFT); c: persist all verdicts to `adjudicated_precursors_all`, Step 10 quality-gate query, table label `embedding_endpoint`. Endpoint variables untouched. | T17a-c |
| `tests/fixtures/workorders/open_cargo_smoke_detector.xml`, `open_landing_light_rh.xml`, `tests/fixtures/pma/wo_embeddings_content_sample.json` | create | §9 T07 | T07 |
| `tests/unit/test_prediction_policy.py`, `test_prediction_repository.py`, `test_prediction_service.py` | create | §8.1 | T04/T08/T09 |
| `tests/unit/test_bq_predefined_queries.py`, `tests/unit/test_bq_evidence_adapters.py` | modify | Allowlist map, dataset-scoped `table()`, array params, job metadata, no project literal in `pma_*.sql` | T02 |
| `tests/unit/test_workorder_analysis.py` | modify | §8.1 | T10 |
| `tests/unit/test_evidence_branches.py`, `tests/integration/test_adk_workorder_upload.py`, `tests/integration/test_adk_evidence_branches.py` | modify | Fake predictor; §6.5 strings | T13b |
| `tests/integration/test_workorder_upload.py` | modify | Fake predictor; chat vs HTTP `pma` equality | T13a |
| `tests/integration/test_pma_prediction_live.py` | create | Live tests (§8.2), `skipif(os.getenv("PMA_LIVE_BQ") != "1")` + `@pytest.mark.live` | T14 |
| `pyproject.toml` | modify | `[tool.pytest.ini_options] markers = ["live: needs BigQuery"]` | T14 |
| `scripts/pma_backtest.py` | create | §8.3 backtest | T14 |
| `scripts/pma_data_gate.py` | create | Read-only data-quality gate (§8.4 item 11) | T18 |
| `tests/eval/pma_eval_config.yaml`, `tests/eval/pma_contract_metric.py`, `tests/eval/datasets/pma-online.json`, `tests/eval/upload_workorder_metric.py`, `tests/eval/datasets/adk-workorder-upload.json`, `tests/eval/upload_eval_config.yaml` | create/modify | §8.4 eval | T11 |
| `frontend/src/domain.ts`, `frontend/src/data/client.ts` (mock), `frontend/src/App.tsx`, `frontend/tests/ui.test.mjs`, `frontend/README.md` | modify (optional, T15, with frontend owner's agreement) | §6.4 mock-only variant | T15 |
| `README.md` | modify | "Online prediction (pma-online-v1)": env vars, flag, limitations, monitoring queries, reviewer workflow, deploy env list | T16/T18 |

**Not changed on purpose:** `pm_agent/config.py` (MODEL), `history.py`, `router.py`, `ipc_manual_retrieval/*`, `EvidenceRequest` fields, `v_target_replacements` (OQ2), Terraform model endpoint variables (OQ8).

---

## 8. Testing and evaluation

### 8.1 Unit tests (offline, fakes only)

- **`test_prediction_policy.py`** (T04): scope rules 1a/1b/1c/2/3/4 from §5.2 (including superseded alias `3400010360` → `340-001-038-0|#1` with position `#1`; FWD detector → `position_not_in_focus_set`; `2085M31G03` without position and both n=0 → `no_lead_time_samples`; one candidate n>0 → `ambiguous_position`; vote winner with twin positions → `ambiguous_position`); vote thresholds and `sqrt` normalisation; `decide` rows 1-5 (share 0.93 → `samples_not_symptom_to_replacement`; n=1 → `insufficient_samples`); `current_tac` chain incl. `counter_inconsistent`; build consistency; unique evidence titles; position alias map.
- **`test_prediction_repository.py`** (T08): templates render only allowlisted, dataset-scoped tables; `@query_embedding` bound `ARRAY<FLOAT64>`, uuid arrays `ARRAY<STRING>`; uuid-less WO → no `None` in any bound value; K passed as `limit=`; wrong dimension / non-empty status → `EmbeddingFailed`; `Forbidden` → `DataSourceUnavailable`; stats row with n=0 and NULL stats → `LeadStats(n=0)`; focus and build info cached; job ids and bytes collected.
- **`test_prediction_service.py`** (T09): with a fake repository, every reason in §6.3 plus `historical_interval`; `json.dumps(result.to_dict())` succeeds for every decision (fake rows contain `date`/`datetime`); prediction log record has the required keys and labels; `default_predictor()` honours the flag and turns construction errors into `data_source_unavailable`.
- **`test_bq_predefined_queries.py` / `test_bq_evidence_adapters.py`** (T02): allowlist map; `table(name, dataset=...)`; other datasets rejected; array params; `None` rejected; `_RunOutcome.job_id/total_bytes_billed`; no `pma_*.sql` contains `qwiklabs`; existing adapters unchanged.
- **`test_workorder_analysis.py`** (T10): `build_pma_wo_text` equals 3 stored `wo_embeddings.content` rows (entry multiset); replay excludes all action text; `ata_chapter`/`position` exposed; `predictor=None` → `pma.reason == "prediction_disabled"` and `prediction`/`timing`/`replacement_recommendation` identical to a golden copy of today's output; fake `historical_interval` fills `timing.estimable_quantiles` and `timing.status`; `due_counter` always null; positional construction `WorkOrderAnalysisService(provider)` still works.
- **`test_evidence_branches.py`** (T13b): section (d) per decision uses §6.5 strings.

Command: `uv run pytest tests/unit -q` (baseline 206; must stay green and grow).

### 8.2 Integration tests

- **Offline** (fake predictor):
  - `test_workorder_upload.py` (T13a): cargo fixture → `pma.decision` from the fake; boiler → `out_of_scope`; chat vs HTTP `pma` identical for the same XML and explicit `analysis_as_of`, comparing with `provenance.bq_job_ids` and `request_id` removed.
  - `test_adk_workorder_upload.py`, `test_adk_evidence_branches.py` (T13b): interval wording only when the decision allows it.
- **LIVE** (T14; `PMA_LIVE_BQ=1 uv run pytest tests/integration/test_pma_prediction_live.py -q`):
  1. Embed parity: `AI.EMBED` of a stored `wo_embeddings.content` row vs stored vector, cosine ≥ 0.999.
  1b. Text-recipe parity: for 20 random `wo_workorders` rows converted to the parser row shape, `build_pma_wo_text(include_actions=True)` matches `wo_embeddings.content` (entry multiset, whitespace-normalised).
  2. Anchor kNN for the cargo fixture text: top-1 `473597-5|AFT` (in-sample smoke test, labelled as such).
  3. Stats `473597-5|AFT`, `analysis_as_of=2026-09-23`: n=35, aircraft_n=28, p50 = 381.0, p90 ≈ 1890.8, share ≈ 0.971 (2026-09-24 rebuild; was n=27, p50 356, p90 1968.2).
  4. RH fixture: decision equals what the T14-chosen threshold gives (at defaults: `no_confident_component_match`); stats `45-0351-4|RH` → n=1.
  5. Stats `2085M31G03|#1` → one row, n=0.
  6. Latest closing TAC for `9H-REG00163` not null.
  7. Billed bytes per `predict()` ≤ 200 MB (from `JobInfo`); p95 latency over 10 calls recorded.
  8. `AI.EMBED` and a curated SELECT run under `app_sa` impersonation (`--impersonate-service-account` / `google.auth.impersonated_credentials`), not user ADC (after the IAM apply).
- `test_agent.py` / `test_server_e2e.py` (live, unmarked) run once in T16.

### 8.3 Backtest (`scripts/pma_backtest.py`, T14)

- **Queries are symptom-only**: remarks + step descriptions from `v_symptom_records`, no action text, part numbers stripped (`r"\b[0-9A-Z]{2,}-[0-9A-Z-]+\b"` removed), embedded with `AI.EMBED` in one batched query (≤ 1,000 texts, ≤ 100 MB).
- **Positives**: the 28 lead-sample precursor WOs plus symptom-only text of focus replacement WOs (their stored `content` is not used). **Negatives**: 500 random `wo_embeddings` WOs that are not focus replacements (seeded).
- Anchors filtered `replacement_date < --as-of`; query WOs with date ≥ `--as-of`; each query excludes its own uuid.
- Output to stdout, per threshold 0.75/0.80/0.85/0.90 and normalisation none/sqrt: false-gate rate on negatives, precision and recall per key (explicitly `#1` vs `#2`, which must be routed to `ambiguous_position`), coverage, no-prediction rate, the similarity distribution of symptom-only positives. Every number is printed with the note "in-sample: curated artifacts mined without a split".
- The chosen threshold is the lowest with false-gate rate ≤ 5% (OQ14); it is recorded in §5.8 by T16.

### 8.4 Acceptance criteria and eval

1. Unit tests pass (≥ 206 + new). `agents-cli lint` clean.
2. Live tests 1-8 pass against the live data project.
3. Chat and HTTP return identical `pma` blocks for the same XML and explicit `analysis_as_of`, excluding `provenance.bq_job_ids` and `request_id`.
4. No response contains a due TAC or a non-null `due_counter`.
5. Every numeric interval carries `basis` and `calibrated=false`; `supporting_interval` always carries its label.
6. With `PMA_PREDICTION_ENABLED=false`: `prediction`, `timing` and `replacement_recommendation` identical to today; additive keys `pma` (`prediction_disabled`) and `parsed_context.{ata_chapter, position}` only (golden-diff test in T10).
7. T14 backtest report produced; `PMA_ONLINE_ANCHOR_SIM_THRESHOLD` confirmed or updated from it.
8. p95 `predict()` latency ≤ 8 s on live BigQuery (T14 measurement).
9. Cloud Logging queries for prediction volume, no-prediction rate by reason, decision mix and latency documented in README (T18).
10. Reviewer workflow section in README: who reviews a PMA output, how to find the prediction log record, how to report a wrong match (T16, OQ15).
11. **Go-live gate (mandatory, blocks setting the flag to true):** `scripts/pma_data_gate.py` passes (7 curated tables present, 768-d/status checks, anchor dedup count = distinct replacement WOs with anchor text in `dim_reference_set` (2561 on 2026-09-24; was a hard-coded 718), lead-sample share of replacement-to-replacement reported, build consistency) **and** the T14 false-gate rate at the chosen threshold ≤ 5%.

**Eval** (`agents-cli eval`, T11 after T13):
- `tests/eval/datasets/pma-online.json`, XML as base64 `inline_data` (`base64 -i tests/fixtures/workorders/open_cargo_smoke_detector.xml`), same pattern as `adk-workorder-upload.json`:
  1. Cargo fixture → contains `Matched component: 473597-5|AFT` and either `No reliable prediction` or `Observed historical interval (not a forecast)`; if any cycle numbers appear, `not a forecast` appears.
  2. `closed_boiler.xml` → `Out of scope for PMA prediction`; no cycle numbers.
- `pma_contract_metric.py` (deterministic): numbers only with a §6.5 interval string; no regex match for `due at TAC \d+` or `TAC \d+ due`; stale-TAC string present whenever a TAC value is printed.
- `upload_workorder_metric.py` and `adk-workorder-upload.json` / `upload_eval_config.yaml` updated to the §6.5 wording.
- Environment: `PMA_PREDICTION_ENABLED=true`, user ADC with curated read access. Run: `PMA_PREDICTION_ENABLED=true agents-cli eval run --config tests/eval/pma_eval_config.yaml`.
- After baseline: `open_nozzle.xml` (exact PN, no position → `no_lead_time_samples`), a `data_source_unavailable` fake, RH fixture, replay cut-off, and a chat free-SQL case ("how many cycles until the aft smoke detector fails?") asserting refusal or basis wording.

---

## 9. Work breakdown for the agent team

**Execution model.**
- One git worktree and branch per task (`pma/T04`, ...), created from `dev` at wave start.
- The lead (opus) merges at the end of each wave in this order: T04 → T02 → T01 → others. After each wave merge the lead runs `uv run pytest tests/unit -q`.
- Until merge, each task's verify runs only its owned tests.
- File ownership is exclusive within a wave. Tasks code against `pm_agent/prediction/contracts.py` (§7 Protocol). T04 is merged before Wave 2 starts.
- No agent runs `terraform plan/apply`, `agents-cli deploy`, pipeline re-runs or any BigQuery DDL/DML. Live reads: SELECT and dry-runs only, small scans.
- Every task ends by running its verify command and reporting the output.

**Model tiers:** `haiku` for mechanical work with a fully specified output; `sonnet` for implementation; `opus` for merge, integration and review.

### Wave 0: decisions (human + lead)
| ID | Title | Owner | Done when |
|---|---|---|---|
| T00 | Resolve OQ1-OQ15 (defaults unless the user says otherwise), record the live tfvars (OQ7), approve the Terraform IAM + env change for later apply | User + lead (opus) | Decisions and tfvars recorded at the top of this file |

### Wave 1 (parallel)
| ID | Title | Files owned | Deps | Tier | Done when | Verify |
|---|---|---|---|---|---|---|
| T04 | Prediction contracts, settings, policy | `pm_agent/prediction/{__init__,settings,contracts,policy}.py`, `tests/unit/test_prediction_policy.py` | T00 | sonnet | §7 Protocol and dataclasses frozen; policy tests per §8.1; `env -i PATH=$PATH uv run python -c "import pm_agent.prediction.contracts, pm_agent.prediction.policy, pm_agent.prediction.settings"` succeeds with no side effects (no `google.auth`, no `config.py` import) | `uv run pytest tests/unit/test_prediction_policy.py -q` |
| T02 | QueryRunner: dataset-scoped allowlist, array params, job metadata | `pm_agent/sub_agents/bq_analytics/queries.py`, `tests/unit/test_bq_predefined_queries.py`, `tests/unit/test_bq_evidence_adapters.py` | T00 | sonnet | §7 `queries.py` items (a)-(f); existing callers unchanged | `uv run pytest tests/unit/test_bq_predefined_queries.py tests/unit/test_bq_evidence_adapters.py -q` |
| T01 | SQL templates | `pm_agent/sub_agents/bq_analytics/sql/pma_*.sql` (8 files) | T00 | sonnet | Files equal §5 SQL; each dry-runs within the §4.3 budget; `grep -l qwiklabs pm_agent/sub_agents/bq_analytics/sql/pma_*.sql` prints nothing | loop below |
| T05 | (removed) `EvidenceRequest` gets no new fields | – | – | – | – | – |
| T06 | IAM + env for curated (code only) | `deployment/terraform/single-project/iam.tf`, `service.tf`, `variables.tf` | T00 | haiku | Diff adds exactly one `google_bigquery_dataset_iam_member` block, one `env` block and one variable (default `"false"`); records the deployed runtime identity from `README.md:1178` / `deployment_metadata.json` in the task report. **No `terraform plan`/`apply`.** | `cd deployment/terraform/single-project && terraform fmt -check iam.tf service.tf variables.tf && terraform validate` |
| T07 | Fixtures from real symptom text | `tests/fixtures/workorders/open_cargo_smoke_detector.xml`, `open_landing_light_rh.xml`, `tests/fixtures/pma/wo_embeddings_content_sample.json` | T00 | haiku | Cargo: registration `SP-REG00374`, remarks/description copied from `v_symptom_records` of precursor WO `190736258` ("AFT CARGO FIRE LOOP A - INOP"), `<positionInfo><position>AFT</position></positionInfo>` in the header, a fresh uuid not in `wo_workorders`, open state, no `componentChange`. RH: registration `9H-REG00386`, description copied from a `v_symptom_records` step of a `45-0351-4\|RH` replacement WO's symptom (no action text), position `RH`, fresh uuid. Sample JSON: 3 `wo_workorders` rows (parser row shape) with their `wo_embeddings.content`. Both XMLs parse with `amos_data.parse_workorders`. | `uv run python -c "from amos_data import parse_workorders; import pathlib; [print(parse_workorders(pathlib.Path(p).read_bytes())[0]['position_info']) for p in ['tests/fixtures/workorders/open_cargo_smoke_detector.xml','tests/fixtures/workorders/open_landing_light_rh.xml']]"` (adapt to the parser's real signature) plus one anchor similarity SELECT for each fixture text, reported |
| T12 | bq_analytics prompt | `pm_agent/sub_agents/bq_analytics/agent.py` | T00 | haiku | `INSTRUCTION` (`:51-72`) lists analytics `v_*` views; curated only if OQ10 = yes (with second constant + 403 caveat); diff touches the prompt string only | `git diff pm_agent/sub_agents/bq_analytics/agent.py` + `uv run pytest tests/unit -q -k bq` |
| T17a | (Optional, OQ6) curated: dedup + partition keys | `curated.tf` (Steps 04b/05 heredocs) | T00 | sonnet | Changes per §7; endpoint variables untouched | extract heredoc SQL with `var.*` substituted, dry-run the SELECT part (no CTAS) |
| T17b | (Optional) curated: same-key / cross-position precursor exclusion | `curated.tf` (Step 06-08 heredocs) — sequential after T17a | T17a | sonnet | as above | as above |
| T17c | (Optional) curated: `adjudicated_precursors_all`, Step 10 gate, `embedding_endpoint` label | `curated.tf` — sequential after T17b | T17b | sonnet | as above | as above |

T01 verify loop (strip comment lines, which `bq` otherwise parses as flags):

```bash
P=qwiklabs-asl-04-1726946cb8ab; C="\`$P.pma_agent_curated"; A="\`$P.pma_agent_analytics"
for f in pm_agent/sub_agents/bq_analytics/sql/pma_*.sql; do
  sed -e "s/{dim_focus_components}/$C.dim_focus_components\`/g;s/{fct_replacement_events}/$C.fct_replacement_events\`/g;s/{replacement_anchor_embeddings}/$C.replacement_anchor_embeddings\`/g;s/{dim_reference_set}/$C.dim_reference_set\`/g;s/{wo_embeddings}/$C.wo_embeddings\`/g;s/{fct_lead_time_samples}/$C.fct_lead_time_samples\`/g;s/{adjudicated_precursors}/$C.adjudicated_precursors\`/g;s/{curated_information_schema_tables}/$C.INFORMATION_SCHEMA.TABLES\`/g;s/{curated_information_schema_table_options}/$C.INFORMATION_SCHEMA.TABLE_OPTIONS\`/g;s/{wo_workorders}/$A.wo_workorders\`/g;s/{v_work_orders}/$A.v_work_orders\`/g;s/{embedding_endpoint}/text-embedding-005/g" "$f" | grep -v '^--' > /tmp/q.sql
  printf '%s: ' "$f"
  bq query --project_id=$P --dry_run --use_legacy_sql=false \
    --parameter='wo_text::x' --parameter='query_embedding:ARRAY<FLOAT64>:[0.1]' \
    --parameter='exclude_wo_uuids:ARRAY<STRING>:["x"]' --parameter='neighbour_wo_uuids:ARRAY<STRING>:["x"]' \
    --parameter='analysis_as_of:TIMESTAMP:2026-09-23 00:00:00' --parameter='min_sim:FLOAT64:0.85' \
    --parameter='limit:INT64:20' --parameter='component_key::473597-5|AFT' \
    --parameter='aircraft_reg::9H-REG00163' --parameter='exclude_wo_uuid::x' "$(cat /tmp/q.sql)" 2>&1 | tail -1
done
```

Expected (2026-09-23): focus 403,751 B; embed 0; anchor 12,691,739; wo 30,379,457; stats 583,224; evidence 441,256; TAC 706,242; build info 20,971,520.

### Wave 2 (parallel; needs T04, T02, T01 merged)
| ID | Title | Files owned | Deps | Tier | Done when | Verify |
|---|---|---|---|---|---|---|
| T08 | BigQuery repository | `pm_agent/prediction/repository.py`, `tests/unit/test_prediction_repository.py` | T01, T02, T04 | sonnet | §7 row; §8.1 tests; never binds NULL | `uv run pytest tests/unit/test_prediction_repository.py -q` |
| T09 | Predictor orchestrator + prediction log | `pm_agent/prediction/service.py`, `tests/unit/test_prediction_service.py` | T04 | sonnet | §7 row; all reasons against a fake repository; lazy repository import in `default_predictor()` | `uv run pytest tests/unit/test_prediction_service.py -q` |
| T10 | O1 + response mapping in the analysis service | `pm_agent/workorders/service.py`, `tests/unit/test_workorder_analysis.py` | T04, T07 | sonnet | §7 row; §8.1 tests incl. golden diff and recipe fixture | `uv run pytest tests/unit/test_workorder_analysis.py -q` |

### Wave 3 (parallel)
| ID | Title | Files owned | Deps | Tier | Done when | Verify |
|---|---|---|---|---|---|---|
| T13a | Wiring: chat service + HTTP | `pm_agent/workorders/chat.py`, `pm_agent/fast_api_app.py`, `tests/integration/test_workorder_upload.py` | T09, T10 | sonnet | Module-level `default_predictor` seam; predictor passed at `chat.py:305` and `fast_api_app.py:227`; construction failure → fallback; `render_uploaded_workorder_summary` uses `pma` and §6.5 strings; chat/HTTP equality test | `uv run pytest tests/integration/test_workorder_upload.py -q` |
| T13b | Wiring: upload node + compose | `pm_agent/nodes/workorder_upload.py`, `pm_agent/nodes/evidence_branches.py`, `tests/unit/test_evidence_branches.py`, `tests/integration/test_adk_workorder_upload.py`, `tests/integration/test_adk_evidence_branches.py` | T09, T10 | sonnet | PartCandidate for pma component; `AircraftContext.position`; section (d) from `analysis.pma` with §6.5 strings | `uv run pytest tests/unit/test_evidence_branches.py tests/integration/test_adk_workorder_upload.py tests/integration/test_adk_evidence_branches.py -q` |
| T14 | Live tests + backtest + pytest marker | `tests/integration/test_pma_prediction_live.py`, `scripts/pma_backtest.py`, `pyproject.toml` (markers only) | T08, T09 | sonnet | §8.2 live 1-8 (8 after IAM apply); §8.3 report; p95 latency | `PMA_LIVE_BQ=1 uv run pytest tests/integration/test_pma_prediction_live.py -q`; `uv run python scripts/pma_backtest.py --as-of 2026-03-01` |
| T18 | Data gate + monitoring queries | `scripts/pma_data_gate.py`, `docs/pma-monitoring.md` (merged into README by T16) | T04 | sonnet | §8.4 item 11 checks (read-only, exits non-zero on failure); Logging queries for item 9 | `uv run python scripts/pma_data_gate.py` |
| T15 | (Optional) Frontend mock variant | `frontend/src/domain.ts`, `frontend/src/data/client.ts`, `frontend/src/App.tsx`, `frontend/tests/ui.test.mjs`, `frontend/README.md` | T04 | sonnet | §6.4 mock-only; no calendar window for the new variant; frontend owner agreed | `cd frontend && npm run build && npm run test:ui` |

### Wave 4: eval, integration and review
| ID | Title | Files owned | Deps | Tier | Done when | Verify |
|---|---|---|---|---|---|---|
| T11 | Eval assets | §7 eval files | T13a, T13b, T07 | sonnet | §8.4 eval; strings from §6.5 | `PMA_PREDICTION_ENABLED=true agents-cli eval run --config tests/eval/pma_eval_config.yaml` |
| T16 | Integration, review, docs | `README.md`; may touch any file to fix integration defects, after announcing it | all | opus | Full suite green; lint clean; eval 2/2; §8.4 checked; diff reviewed for model-name changes (none), project literals in SQL (none), SQL interpolation of user values (none), write paths (none); threshold updated from T14; prediction-log sink routing verified or OQ11 raised; deploy request with env list (`PMA_PREDICTION_ENABLED` and any `PMA_*` overrides) for human approval | `uv run pytest tests/unit tests/integration -q -m "not live" --ignore=tests/integration/test_agent.py --ignore=tests/integration/test_server_e2e.py`; `agents-cli lint`; `git diff main... -- pm_agent/config.py` empty |
| T19 | (Deferred) Outcome-join spec | design note only | T16, OQ11 | opus | SQL joining persisted predictions to later `fct_replacement_events`, retention proposal | review |

**Critical path:** T00 → (T04, T02, T01) → T08 → T14 → go-live gate → T16. In parallel: T07 → T10 → T13a/b → T11. The IAM apply (human) must happen before live test 8 and before enabling the flag.

---

## 10. Risks, guardrails, rollback

| Risk | Impact | Mitigation |
|---|---|---|
| Lead samples are mostly replacement-to-replacement (26/28); one AFT precursor is a FWD loop | Misleading "lead time" | Decision row 4 suppresses the interval; `supporting_interval` only with its label (OQ6); T17a-c fixes |
| Wide spread (p50-p90 356-1,968) | Not actionable | No tiers, no due TAC; recommend-only wording |
| Gate false positives (31% at 0.80) | Wrong component attributed | Threshold 0.85 provisional; T14 false-gate rate ≤ 5% before go-live; twin-position winners → `ambiguous_position` |
| Symptom-only vs full-text representation | Low similarity, few gates pass | OQ9; thresholds from symptom-only backtest |
| Embedding mismatch / silent endpoint drift | Garbage neighbours | Allowlisted endpoint; dim/status asserted; label check (T17c); parity tests 1/1b |
| Cross-aircraft TAC mixing / stale TAC | Wrong due TAC | No absolute TAC at all; `current_tac` context only |
| Replay leakage / in-sample artifacts | Over-optimistic evals | Date filters; no action text in replay; `in_sample_curated_artifacts`; symptom-only backtest with negatives |
| Fan-out duplicates | Double-counted votes/evidence | `GROUP BY replacement_wo_uuid`; evidence from lead samples |
| Half-refreshed rebuild | Mixed builds | Build-consistency check (§5.6) |
| No IAM on curated | 403 in production | T06 + human apply; `data_source_unavailable` fallback |
| Runtime identity for `AI.EMBED` unknown | Embed fails | T06 records identity; live test 8 impersonates `app_sa` |
| Cost and latency | Slow answers | ≈ 45 MB processed per request; caches; 5 GB cap, 20 s timeout; p95 ≤ 8 s acceptance |
| Terraform drift (unrecorded tfvars) | A future apply replaces tables | T00 records tfvars before any apply |
| Free-SQL chat quotes `lead_cycles` as a forecast | Guardrail bypass | Curated tables not in the chat prompt by default (OQ10); eval case |
| Prediction log not routed | No audit trail | Labels matching `telemetry_logs_filter`; T16 verifies; OQ11 |

**Guardrails:** read-only agent (`WriteMode.BLOCKED`, SELECT-only templates, dataset-scoped allowlist); no LLM in the prediction path (only the embedding); numbers never shown without a basis label; no due TAC; typed out-of-scope and no-prediction answers; model names unchanged.

**Rollback:**
1. Redeploy with `PMA_PREDICTION_ENABLED=false` (Terraform env or `agents-cli deploy` env; **needs human approval**). The service returns today's legacy keys plus `pma.reason=prediction_disabled`.
2. Revert the merge commit on `dev`. The contract is additive.
3. The IAM grant can stay (read-only).
4. T17 data fixes are reverted by re-running the previous `curated.tf` (human-approved).

**Cleanup noted, not part of this plan (user decision):**
- The costly `cord19_embeddings` Vertex endpoint `2963093676902842368`.
- `pm_agent/.adk/session.db` in the package tree.
- Stale `TARGET_PNS` in `amos_data/cohorts.py:22` and `scripts/evaluate_history_retrieval.py:18`.
- The empty `faa_sdr_wo_parts`, `retrieval_*` tables and their disabled load jobs.
- Handover pending items (owner: evidence stream): `get_part_coverage` needs a coverage window on `EvidenceRequest`; IPC citation doc corrections. (`AircraftContext.position` always None is fixed by T13b.)
- View parity harness (parallel-evidence handover step 4).

---

## 11. Open questions (defaults apply if there is no answer)

1. **OQ1: Output semantics vs BIGQUERY-AGENT-plan §8.5.** §8.5 says no n/CV threshold establishes confidence, closing-to-closing differences must be labelled as such, and a deadline needs a validated decision policy. **Default:** follow §8.5 strictly: no confidence tiers, no absolute due TAC, `due_counter` always null, interval labelled `closing_tac_to_closing_tac_interval` with support descriptors. Answer "no" only if the stakeholders explicitly accept a departure from §8.5.
2. **OQ2: Component scope.** **Default:** scope from `dim_focus_components` (live top-6) plus the static `FOCUS_PART_FALLBACK`. The legacy 3 remain valid for IPC evidence and `target_part_number` but return `out_of_scope` for prediction. `v_target_replacements` unused.
3. **OQ5: `current_tac`.** **Default:** context only (user-supplied 24 h → XML `issue.tac` → latest closing TAC), with `stale` / `counter_inconsistent` flags. Never added to an interval.
4. **OQ6: Ship the numeric path before the T17 re-run?** **Default: no.** Wire now; samples with replacement-interval share > 0.5 give `samples_not_symptom_to_replacement`. Sub-question: show the labelled `supporting_interval` (consecutive-replacement interval, n=27) as evidence? **Default: no** (`PMA_SHOW_SUPPORTING_INTERVAL=false`). T17a-c run in parallel; re-run only with human approval (expect AFT n to drop to about 1-5).
5. **OQ7: Terraform state.** The live apply used unrecorded tfvars (checked-in `vars/*.tfvars` points at `qwiklabs-asl-04-2a1ac15646fc`). **Default:** T00 records the actual tfvars (`run_curated_real_data_pipeline=true`, `load_curated_data=false`, endpoints) before any T06/T17 apply.
6. **OQ8: LLM endpoint name `gemini-3.8.flash` in `variables.tf`.** The jobs ran with `gemini-3.8-flash`. **Default:** not changed here; confirm before any pipeline re-run.
7. **OQ9: Representation: full-WO vs symptom-only.** Anchors and `wo_embeddings` embed full completed-WO text; online queries are symptom-only. The parallel-evidence handover lists this as unresolved. **Default:** keep full-text vectors for now, query with symptom-only text, set thresholds from the symptom-only backtest (T14). A symptom-only anchor re-embedding is a T17 follow-up.
8. **OQ10: Should the free-SQL `bq_analytics` chat agent see curated tables?** **Default: no** until the eval refusal case passes; the prompt lists analytics `v_*` views only.
9. **OQ11: Persist predictions?** Needed for the §11.6 outcome join, but it is a write path against the read-only rule. **Default:** no BigQuery writes by the agent; predictions go to the log sink only (`labels.type="agent_telemetry"`); a dedicated sink table and retention policy need approval (T19 deferred).
10. **OQ12: Hosting of `/workorders/analyze` for the frontend.** Agent Runtime does not serve it. **Default:** no hosted HTTP path in this plan; T15 is mock-only.
11. **OQ13: Aircraft-type scope (NG vs MAX).** LEAP-1B nozzles apply to the MAX, CFM56-7B fan blades to the NG. **Default:** no type filter now; `aircraft_type_not_in_scope` reserved; T14 reports per-type results.
12. **OQ14: Go-live threshold for enabling the flag.** **Default:** data gate passes and T14 false-gate rate ≤ 5% at the chosen threshold; recall is reported, not gated.
13. **OQ15: Human reviewer / consumer of PMA outputs.** **Default:** maintenance-control engineer reviews every non-`out_of_scope` output; README documents the workflow (acceptance item 10). Name the role owner.
14. **OQ16: Expose `llm_conf`?** Review #6 forbids LLM confidence as confidence. **Default:** not exposed (neither in `evidence_support` nor in evidence rows); logged only.

15. **OQ-B1 (Option B follow-ups, raised 2026-09-24 review).** (a) *Window basis:* `supporting_interval` p50/p90 come from `fct_lead_time_samples` (row 4 = *mostly* replacement intervals; up to half can be symptom leads, and the sample is bounded by the precursor search window). All consecutive same-key gaps in `fct_replacement_events` for `473597-5|AFT` give n=165, p50 415, p90 1777. **Default:** keep as approved, documented in the `ProjectedWindow` docstring and README; alternative is a replacement-interval-only statistic. (b) *Wording:* string (c) `past_p90` says "replacement is overdue against the fleet pattern"; "overdue" implies a deadline. Proposed: "later than 90% of fleet replacements". (c) *Boundary:* `cycles_since == round(p50)` renders "past the fleet median"; proposed `<=` for `before_p50`. (d) *Failed lookup:* `projected_window_unavailable` renders nothing; a fixed string ("The aircraft's replacement history could not be read, so no aircraft-specific window is given.") needs approval. **Default for (b)-(d):** unchanged (spec as approved) until the user decides.

(OQ3 "what O4 aggregates over" and OQ4 "integration point" from rev 1 are closed: the answers are in §5.4 and §4.2.)

---

## Appendix A. Revision log (rev 1 → rev 2)

Verification method: BigQuery dry-runs and small SELECTs (read-only), and reading the cited code. "Acc" = accepted, "Part" = partly accepted, "Rej" = rejected.

**Critic: data-correctness**

| # | Finding | Verdict | Reason |
|---|---|---|---|
| 1 | QUALIFY without analytic fn (anchor SQL) | Acc | Reproduced the error; rewritten with a `scored` CTE, dry-runs 12.7 MB |
| 2 | QUALIFY in WO SQL; T01 done-when | Acc | Same; 30.4 MB |
| 3 | 0.80 threshold → ~30% false gates | Acc | Reproduced: 1,242/3,947 at 0.80, 297 at 0.85; default 0.85 provisional, T14 measures negatives |
| 4 | Backtest measures anchor vs anchor | Acc | 1,667/1,720 anchors carry action text; §8.3 uses symptom-only text + negatives |
| 5 | Vote cannot split `#1`/`#2` | Acc | Probe split 13/7; twin-position winners → `ambiguous_position` |
| 6 | Superseded PNs missed | Acc | Reproduced 155/283 superseded `part_off_pn`; alias set in focus SQL |
| 7 | Non-focus position predicted as focus | Acc | FWD 79 rows for `473597-5`; `position_not_in_focus_set` + alias map |
| 8 | Evidence shows non-sample pairs | Acc | 30 pairs vs 28 samples; evidence now from `fct_lead_time_samples` |
| 9 | Stats return no uuid set | Acc | `ARRAY_AGG` added; evidence date-filtered in SQL |
| 10 | p90 2330 vs 1968.2 | Acc | Reproduced 1968.2 (`PERCENTILE_CONT`), 2330 is APPROX decile |
| 11 | `wo_text` recipe misstated | Acc | Confirmed in `curated.tf:145-172`; exact pseudocode + tests |
| 12 | NULL exclusion params | Acc | Never bind NULL; `IFNULL` guard; test |
| 13 | n=0 returns one row | Acc | Reproduced |
| 14 | Embed SQL calls AI.EMBED twice | Acc | CTE form only |
| 15 | ATA format misdescribed | Acc | `NN-NN` text, 1 malformed |
| 16 | "LEAP fan blades" wrong | Acc | CFM56-7B EO task; engine keys only via deterministic match |
| 17 | `label_number` missing | Acc | Added |
| 18 | Closing date range wrong | Acc | Reproduced min 2010-06-19, 55 pre-2025; `counter_inconsistent` flag |
| 19 | Byte estimates wrong | Acc | Replaced with own dry-run values |
| 20 | Tier rules undefined/misleading | Acc (superseded) | Tiers removed entirely (completeness #1) |
| 21 | K ignores class imbalance | Acc | Noted; `PMA_VOTE_NORMALISATION` evaluated in T14 |
| 22 | 5,000-row vector-index minimum unverified | Acc | Claim dropped |
| 23 | `9H-REG00135` not found | Rej (fact) / Acc (fix) | `9H-REG00135` exists in `v_work_orders` (20 WOs); examples still switched to `9H-REG00163`/`9H-REG00386`, which have AFT reference WOs |

**Critic: code-feasibility**

| # | Finding | Verdict | Reason |
|---|---|---|---|
| 1 | QUALIFY blocker | Acc | As above |
| 2 | Hard-coded project; runner single-dataset | Acc | Confirmed `queries.py:65-66,139,152-158`; placeholders + dataset-scoped `table()`; repository composes runner |
| 3 | `pma` read from wrong path | Acc | `chat.py:328-333`, `evidence_branches.py:391` |
| 4 | Wording change targets unreachable code | Acc | `render_uploaded_workorder_summary` is the analyzed path; added to T13a |
| 5 | New request fields unconsumed; no part candidate | Acc | Fields dropped; PartCandidate added |
| 6 | `EvidenceRequest.position` duplicates `AircraftContext.position` | Acc | Confirmed `evidence.py:157-176` |
| 7 | Focus validation cannot run in `__post_init__` | Acc | Static fallback constant; live check in predictor |
| 8 | Replay action-text rule misstated / label leak | Acc | Replay excludes all action text |
| 9 | Recipe mismatch | Acc | As DC-11 |
| 10 | Header PN and position missing | Acc | Added with `_resolve_targets` availability rules |
| 11 | n=0 note wrong | Acc | As DC-13 |
| 12 | No job ids / bytes | Acc | `_RunOutcome` extension in T02 |
| 13 | JSON safety | Acc | `to_dict` primitives + test |
| 14 | `service.tf` env missing | Part | Env added to T06; but `GOOGLE_CLOUD_PROJECT` is platform-injected (`service.tf:44`), so only the call-time fallback is adopted |
| 15 | RH/engine expected outcomes unreachable | Acc | Reproduced (RH max 0.808, engine 0.784); PN path without vote; expectations rewritten |
| 16 | Byte-identical criterion contradicts changes | Acc | Legacy `query_text` untouched; criterion restated |
| 17 | Identical `pma` impossible | Acc | Exclude job ids / request id |
| 18 | Embed double call | Acc | As DC-14 |
| 19 | `@k` vs `@limit` | Acc | `LIMIT @limit` |
| 20 | TAC exclusion drops rows | Acc | `IFNULL` + `''` sentinel |
| 21 | `to_thread` already done | Acc | Instruction dropped; diagram fixed |
| 22 | Wrong line; construction failures | Acc | `:227`; keyword-only `predictor`; fallback |
| 23 | `manual_evidence` KeyError; `timing.status` | Acc | `targets` legacy-only, `.get`; `historical_interval_only` |
| 24 | Views allowlisted for no task; T18 undefined | Acc | Allowlist only template reads; T18 redefined |
| 25 | `live` marker unregistered | Acc | T14 owns `pyproject.toml` markers |
| 26 | `npm test` missing | Acc | `npm run build && npm run test:ui` |
| 27 | Build-info cost | Acc | 21 MB with TABLE_OPTIONS |
| 28 | T08 needs unowned `queries.py` changes | Acc | T02 done-when explicit |
| 29 | Third `analyze_xml` caller | Acc | Noted in §7 |

**Critic: team-executability**

| # | Finding | Verdict | Reason |
|---|---|---|---|
| 1 | QUALIFY | Acc | As above |
| 2 | Tier evaluation order | Acc (superseded) | Tiers removed; `decide` is first-match with explicit order |
| 3 | Runner cross-dataset + job metadata | Acc | Via `table(name, dataset=)` rather than `dataset_placeholders`; same effect |
| 4 | Project literal | Acc | Placeholders; grep check |
| 5 | `@k` vs `@limit` | Acc | As CF-19 |
| 6 | No dry-run script | Acc | Literal loop in §9 (comment lines stripped; `bq` otherwise errors); T01 → sonnet |
| 7 | Two embed versions | Acc | As DC-14 |
| 8 | NULL params | Acc | As DC-12 |
| 9 | Recipe; new_work_order masking | Acc | Pseudocode; `include_actions` rule stated |
| 10 | Header position | Acc | As CF-10 |
| 11 | Engine outcomes | Acc | As CF-15 |
| 12 | `pma` key path | Acc | As CF-3 |
| 13 | T13 cannot get fields | Acc | `parsed_context.position` + `pma.component`; `wo_text` not needed by T13 |
| 14 | Scope validation mechanism | Acc | As CF-7 |
| 15 | Protocol signatures | Acc | Listed in §7 |
| 16 | Predictor None contradiction | Acc | `PredictionResult.disabled()` emitted by the service |
| 17 | Eval: base64, strings, order, env | Acc | §6.5 strings; T11 in Wave 4 after T13 |
| 18 | T06 `terraform plan` | Acc | fmt + validate only; plan/apply forbidden |
| 19 | T17 too big | Acc | Split T17a-c, sequential, dry-run verify |
| 20 | Parallel execution model | Acc | Worktree per task; lead merges |
| 21 | n=0 note | Acc | As DC-13 |
| 22 | Double thread wrapping | Acc | As CF-21 |
| 23 | Array type by name | Acc | Explicit type strings |
| 24 | Fixture values | Part | Concrete values given, but taken from real `v_symptom_records` text (completeness #3) instead of authored probe text; `9H-REG00163` has 4 distinct AFT reference WOs, not 6 |
| 25 | Backtest spec | Part | Spec made concrete, but uses symptom-only text + `AI.EMBED` (not stored full-text vectors, which leak labels) |
| 26 | Marker; T16 command | Acc | As CF-25; T16 ignores unmarked live tests |
| 27 | `npm test` | Acc | As CF-26 |
| 28 | Chat/HTTP equality owner | Acc | T13a |
| 29 | Tier fit; split T13 | Acc | T11 → sonnet after T13; T13a/T13b |
| 30 | `__init__.py` exports | Acc | Docstring only |
| 31 | Prompt dataset + 403 | Acc | Applied under OQ10 |

**Critic: completeness**

| # | Finding | Verdict | Reason |
|---|---|---|---|
| 1 | Plan breaks §8.5 | Acc | Verified §8.5 text; tiers, due TAC, `tac_p50` removed; basis relabelled; OQ1 rewritten |
| 2 | Known-invalid numbers shipped as positive case | Acc | Decision row 4; OQ6 default "no" |
| 3 | Validations leak the answer | Acc | Symptom-only backtest with negatives; real-text fixtures; in-sample labels |
| 4 | Representation shift | Acc | OQ9; threshold from T14 (0.85 provisional instead of "unset", so the code has a fail-closed default) |
| 5 | Vector-space contract | Part | Label check added; absent label → limitation rather than fail-closed, since no table has labels today and fail-closed would disable everything until T17c |
| 6 | Guardrails 4/5 dropped | Acc | Full `pma` in log, sink labels, T19 deferred, OQ11 |
| 7 | Go-live checklist gaps | Acc | §8.4 items 8-11 |
| 8 | Free-SQL chat bypass | Acc | OQ10 default no; eval case |
| 9 | Frontend displays a forecast | Acc | New variant has no calendar window |
| 10 | Hosting; rollback is a redeploy | Acc | OQ12; T15 mock-only; rollback step 1 rewritten |
| 11 | Byte-identical criterion | Acc | As CF-16 |
| 12 | Eval env and coverage | Acc | Env stated; stale dataset under T11; follow-up cases |
| 13 | Runtime identity; impersonation test | Acc | T06 records identity; live test 8 |
| 14 | Sample independence; eligibility | Acc | `independent_aircraft`, `chained_sample_n` (2 for AFT), `no_installation_eligibility_check` |
| 15 | Half-refreshed rebuild | Acc | Build-consistency check |
| 16 | Reason enum incomplete | Acc | Codes added; precedence in §5.2 |
| 17 | T18 undefined; T03 gap; allowlist | Acc | T18 defined (data gate); allowlist narrowed; T05 marked removed |
| 18 | Handover items not carried | Acc | Added to cleanup; position fixed by T13b |
| 19 | `to_thread`; chat defaults | Acc | §5.7 |
| 20 | Missing OQs; close OQ3/OQ4 | Acc | OQ9-OQ16 added; OQ3/OQ4 closed |
| 21 | Narrowed `recommendations[]` | Acc | R13 states it; `component.candidates[]` |
| 22 | Identical `pma` | Acc | As CF-17 |
