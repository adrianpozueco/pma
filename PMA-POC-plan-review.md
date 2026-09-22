# PMA-POC plan validation

Reviewed on 2026-09-22 against repository commit `68e6581`, `PMA-POC-plan.md`, `CLAUDE-CODE-HANDOVER.md`, the checked-in agent/infrastructure code, and the local AMOS snapshot. Three parallel reviewers covered data/BigQuery, ADK integration, and delivery/evaluation; findings were consolidated and checked against source.

**Verdict: revise before implementation.** The plan contains useful ideas for retrospective case matching, but it is not an executable plan for this repository's next BigQuery increment. Complete useful, grounded analytics and historical-case retrieval in the existing BigQuery specialist first. Treat precursor identification as a later experiment, and keep prospective risk/timing unavailable until the required evidence exists.

The plan assessment did not change the application, source data or original plan. A subsequent user request also authorized fixing fresh-project knowledge-base provisioning; that separate change is recorded below. We did not query live BigQuery, run an agent evaluation, deploy resources, or verify live IAM. Counts below describe local files, not confirmed cloud contents. Public Google documentation was checked for the generated-text response contract and BigQuery vector search.

## Current baseline and document authority

| Area | Actual state | Consequence for the plan |
|---|---|---|
| Agent | `pm_agent/agent.py` routes each turn to exactly one of `ipc_manual_retrieval` or `bq_analytics`. | Extend the existing BQ leaf; a new root agent is unnecessary. Combined manual-plus-history answers would need a separate, explicit orchestration increment. |
| Manuals | IPC retrieval is implemented; the user reports it working. The prompt describes a small set of parts-catalogue chapters. | Preserve this branch and its citations. IPC coverage does not establish complete AMM/FIM/SRM procedure coverage. |
| BigQuery | An importable specialist exposes a generic read-only BigQuery toolset over `pma_agent_analytics.wo_workorders` and `faa_sdr_wo_parts`. | Curated queries/tools and answer-quality evaluation are still missing. |
| Ingestion | `scripts/wo_xml.py` and `scripts/xml_to_ndjson.py` already preserve nested steps, actions and component changes with typed fields and UUIDs. | Reuse and harden these assets; derive flat views with `UNNEST` instead of starting a parallel parser. |
| Infrastructure | `deployment/terraform/single-project/analytics.tf` owns the analytics dataset, two source tables, staging objects and load jobs. | Extend this Terraform module. Preserve the intentional `pm-agent` / `pm_agent` / `pma-agent` naming differences. |
| Environment | Checked-in project: `qwiklabs-asl-04-1726946cb8ab`; analytics region: `us-central1`; agent Gemini location: `global`; current agent model: `gemini-3.8-flash`. | Reuse current configuration. Embedding model and BigQuery remote-model compatibility require their own check. |
| Tests/evals | Unit test is a dummy; integration checks mostly establish transport/text; eval prompts are greeting/weather/Paris. | These do not validate BQ answer correctness, retrieval or predictions. |

The handover's methodological constraints remain relevant, especially source separation, timestamp/installation eligibility, leakage prevention and uncertainty. Its original inventory is historical: it describes another root and a state before this implementation. Referenced research/catalog files and `scripts/verify.py` are absent here. Likewise, the plan's `infra/`, `ingest/`, `transforms/` and `asl_*` reuse paths do not exist. New transform files could be added, but should not be described as already scaffolded.

Current code takes precedence over stale README status notes: `apis.tf` already includes Discovery Engine and `variables.tf` already includes `discoveryengine.viewer`. The missing BigQuery runtime grants remain a real checked-in configuration gap.

## Measured data readiness

The aggregate evidence is saved in [pma-poc-data-profile-2026-09-22.json](docs/audits/pma-poc-data-profile-2026-09-22.json). The audit read every row of `data/processed/wo_workorders.ndjson.gz` and every XML file, counted actual XML date/namespace fields, and reproduced the plan's swap and L0/L1 predicates locally. Candidate counts below precede embedding/LLM filtering and do not establish relevant precursors.

Audited normalized-input SHA-256: `7b6ae65bfd52ab9c2703a1851049662e783742f498536549a2f9c79dcc6ae001`. The two scope variants apply the plan's ranking to all source aircraft and to replacements with `B737-8` / `M73-82`; candidate WOs are selected by the plan's same-registration, lower-closing-TAC and ATA-prefix rules.

| Observation | Measured result | Implication |
|---|---:|---|
| XML files / work orders / distinct WO UUIDs | 8,259 / 8,259 / 8,259 | A usable local source snapshot exists. No multi-WO envelope occurs in this snapshot. |
| Closed work orders | 8,259 | No original open-WO version history is demonstrated by these files. |
| Component changes / distinct change UUIDs | 9,085 / 9,085 | Component-change UUID is available for event identity. |
| WOs containing a component change | 7,631, or 92.4% | Strong selection toward maintenance actions; completeness of symptom/exposure history is unproven. |
| Swaps satisfying the plan's different-present-serial predicate and non-null closing TAC | 8,956 across 7,552 WOs | These are observed serialized changes, not automatically failures. |
| WOs with multiple eligible swaps | 392; maximum 24 swaps in a WO | WO UUID cannot identify a replacement event. |
| Eligible changes with different off/on PNs | 1,555 | Installed-PN and removed-PN rankings differ; a PN change alone does not prove supersession. |
| XML closing date representation | 8,256 `closingDateTime`; 3 `closingDate` | The plan's sample field map omits the dominant representation. |
| XML WO namespaces | 0 namespace-qualified work orders | The plan's assertion of a default WO namespace is false for the current snapshot; still test namespace support for future inputs. |
| Closing TAC / issue TAC populated | 8,259 / 45 | A closing counter is broadly available; the symptom-onset counter usually is not. |
| Counter regressions | 2 across 7,201 adjacent, distinct-closing-date comparisons, on 2 aircraft | Require exception handling; this check is not proof of full timeline consistency. |
| Aircraft types | 5,632 `B737-8`; 2,579 `M73-82`; 19 `32S`; 1 `B737-7`; 28 missing | Explicitly apply the agreed NG/MAX scope. The current source includes out-of-scope and unknown aircraft. |
| Aircraft identities | 629 known aircraft; 28 WOs missing identity; maximum 39 WOs per aircraft | Sparse longitudinal coverage despite a long overall date span. |
| Dates | Issues: 2006-02-07 to 2026-09-13; closures: 2010-06-19 to 2026-09-13 | Range is not continuous coverage: 8,204 closures are in 2025-2026. |

The proposed automatic Top-3 selection is particularly weak. Applying the plan's installed `PN|position` key to all source aircraft gives:

| Rank / key | Change rows | Distinct replacement WOs | L0/L1 change-to-WO pairs | Distinct replacement-WO / earlier-WO pairs |
|---|---:|---:|---:|---:|
| 1: `2085M31G03|#1` | 406 | 43 | 21 | 2 |
| 2: `473597-5|AFT` | 335 | 321 | 147 | 135 |
| 3: `2085M31G03|#2` | 294 | 38 | 37 | 3 |

Restricting replacements to the repository's NG/MAX mapping changes the change counts to 398 / 335 / 276, with the same ranked keys and 2 / 135 / 3 distinct WO pairs. These distinct pairs still are not necessarily independent failure episodes. For the first and third keys, even the plan's proposed minimum of eight independently supported examples is unreachable from these candidates. The same PN and position occur up to 18 times in one WO, so `PN|position` is a grouping key, not a unique physical installation. Select an initial family using eligible history and reviewed episode support, not raw change frequency alone. The second-ranked key warrants inspection; it is not yet a validated target.

## Findings that change the implementation

1. **High: the plan has no integration milestone for the existing BQ agent.** Its final `raw.*`, `curated.*` and Vector Search artifacts have no consumer in this code. The specialist currently instructs the model to use only the two existing source tables. Completing the offline DAG would leave that specialist unchanged. Add explicit tools, source contracts, routing coverage and grounded-answer acceptance checks in `pm_agent/sub_agents/bq_analytics/`.

   Evidence: `PMA-POC-plan.md:671`; `pm_agent/sub_agents/bq_analytics/agent.py:25`, `:46`, `:51`, `:108`; `pm_agent/agent.py:44`.

2. **High: preserve event grain throughout the SQL.** Step 01 discards `cc_uuid`. Steps 06 and 07 join anchors on `replacement_wo_uuid` alone, and Step 06 limits candidates per WO. Multiple swaps therefore cross-match anchors, multiply rows and compete for the same top-K budget. Adding only `component_key` is insufficient because repeated changes of the same PN/position occur within one WO.

   Propagate `source_namespace`, `wo_uuid`, `source_version`, `work_step_uuid`, `work_step_seq`, `action_uuid`, `cc_uuid` and a stable `replacement_event_id`. Derive event identity from the audited source/WO/change identity, preserve revision identity separately, and key every anchor, join and per-event ranking on it. Use a deterministic tie-breaker. Do not count several precursor WOs leading to one event as independent replacement outcomes.

   Evidence: plan `:298`, `:364`, `:414`, `:487`, `:491`, `:547`, `:588`; parser `scripts/xml_to_ndjson.py:302`, `:344`, `:380`; handover `:150`.

3. **High: fix field coverage and audit the existing parser before reusing it for broader feeds.** The current parser already handles both date and timestamp forms; a new parser built literally from the plan would lose most closing dates. The current code is nevertheless not a general multi-WO/version-aware importer: `build_record` selects the first WO, namespace matching is literal, and `iter_workorders` silently skips malformed XML. Existing data contains one WO per file, so these are future-input robustness gaps rather than observed dropped records in this snapshot. Date helpers recognize string patterns rather than fully validating calendar values.

   Extend the current parser with explicit parse/reject counts, source checksums/ingestion metadata, namespace and multi-WO handling, calendar validation, and defined revision/deduplication behavior. Keep existing originals and nested structure. Audit action timestamps and existing classification fields before assigning every timing/removal decision to an LLM.

   Evidence: plan `:87`, `:259`, `:262`; `scripts/xml_to_ndjson.py:440`, `:544`; `scripts/wo_xml.py:66`, `:96`; handover `:86`.

4. **High: current matching does not establish component eligibility.** Same aircraft, earlier closing TAC and matching ATA are candidate filters. They do not show that the same physical component was installed when the earlier symptom occurred. The plan has no installation interval, intervening-removal exclusion, mandatory date ordering or reviewed episode definition. Taking the first surviving matched replacement after a precursor does not repair these omissions.

   Preserve removed and installed PN/serial separately; establish aircraft and component identity, installation windows and temporal validity before making linked-event claims. Use the removed component for removal-history questions; explicitly define the business meaning of installed-component rankings. Rename `is_supersession` to an observed `part_number_changed` flag unless an approved supersession map exists. Distinguish replacement counts from failure rates, which require outcome definitions and exposure denominators.

   Evidence: plan `:110`, `:113`, `:114`, `:305`, `:313`, `:448`, `:575`; handover `:90`, `:95`, `:146`. Local issue-TAC coverage and sparse aircraft histories keep onset timing/installation validity unresolved.

5. **High: offline and online information are different.** The plan embeds completed descriptions plus actions, then queries them with a later replacement's failure/rectification text. That can support retrospective case investigation, but it does not validate new-WO retrieval using only symptoms available at the time. The L3 prompt also requires a known later replacement, so it cannot simply become the online component gate.

   Maintain separate symptom and completed-case representations, timestamps/availability cutoffs, text versions and evidence links. Evaluate simulated new-WO queries without future actions or future labels in the indexed corpus. Completed historical actions may be shown as explicitly dated outcome evidence when available at the replay cutoff. Across aircraft, use dates/availability for historical eligibility; comparing one aircraft's cumulative TAC to another aircraft's TAC is invalid.

   Evidence: plan `:52`, `:175`, `:263`, `:384`, `:527`, `:567`, `:627`; handover `:148`, `:149`.

6. **High: descriptive matching is repeatedly mislabeled as causal or predictive.** The header correctly excludes a trained model, but L3 promises "causality", its output is called "verified", and sample count/CV become "OK confidence". None of these establishes causal precursors or calibrated prediction. Selected positive replacement matches also omit the event-free/censored population needed for prospective risk or time-to-event estimation.

   Call L3 outputs candidate relationships with supported/contradicted/uncertain review states. Keep all verdicts and their provenance. Label any supported quantiles as observed intervals among selected historical matches, with unique event/episode/aircraft support and selection limitations. LLM self-confidence, cosine similarity, `n >= 8`, and `CV <= 0.5` must not become a probability or confidence score. Prediction and prospective timing should return explicit unavailable/insufficient-evidence states until a separate, justified model/cohort stage passes.

   Evidence: plan `:8`, `:173`, `:507`, `:563`, `:650`, `:697`; handover `:101`, `:130`, `:146`, `:152`, `:179`.

7. **High: online BigQuery permissions and query controls are incomplete.** Checked-in application roles omit `bigquery.jobUser` and `bigquery.dataViewer`; granting those to the plan's new offline pipeline account does not authorize the existing runtime identity. This is a configuration finding; live IAM was not inspected. The current tool's `compute_project_id` selects the compute/billing project, not an allowlist of referenced tables. `WriteMode.BLOCKED` is valuable but does not itself bound reads or cost.

   Give the actual runtime principal job-creation access and appropriately scoped dataset/view read access. Keep offline write permissions separate. Prefer typed, parameterized tools over approved views, with a configured bytes-billed ceiling, result limits, timeout/error behavior and provenance. Distinguish population/event counts from returned row counts and expose truncation. Review whether forecast/anomaly/data-insights tools belong in this descriptive scope.

   Evidence: plan `:232`; `deployment/terraform/single-project/variables.tf:38`; `iam.tf:47`; BQ agent `:77`, `:83`, `:95`. Installed ADK `integrations/bigquery/query_tool.py` confirms the compute-project behavior.

8. **High: the current evaluation gate cannot validate the intended behavior.** A spot-check of retained candidates measures neither missed precursors nor new-WO relevance. Tuning and judging thresholds on the same approximately 50 pairs is development feedback, not held-out validation. Distribution shape is not a predictive go/no-go test.

   Add a deterministic query baseline and reviewed keyword-retrieval baseline. Separate development and held-out cases by time and related asset/episode; where the sample is too small, report that limitation. Include wrong family/component, ambiguous/missing data, no-result, permission/error and unsupported-prediction cases. Evaluate grounded numerical answers, source support, relevant-case retrieval, false matches/missed cases, abstention, IPC regression, routing and latency. Choose acceptance thresholds with evidence instead of treating draft constants as agreed requirements.

   Evidence: plan `:647`, `:665`; `tests/unit/test_dummy.py:21`; `tests/integration/test_agent.py:23`; `tests/eval/datasets/basic-dataset.json:4`; handover `:149`, `:151`, `:181`.

9. **Medium: SQL/API examples contain execution defects and missing failure handling.** Step 07 uses undefined `p.*` and a literal prompt placeholder. For Gemini with `flatten_json_output = TRUE`, parse `ml_generate_text_llm_result`; the shown `ml_generate_text_result` belongs to the non-flattened response. Persist `ml_generate_text_status`, parse/schema errors and rejected/uncertain outcomes. Step 04 must preserve embedding status, validate vector dimensions and handle empty/failed inputs. Version the model, text hash, prompt and run; reuse successful embeddings and retry failed rows within bounds.

   Step 02's `RANK() <= 3` can select more than three components on ties. Use a deterministic `ROW_NUMBER()` if exactly three are required, or document tie-inclusive selection. The vector-export sample selects `wo_uuid AS id` but then maps from `wo_uuid`; its writer and placeholder restrictions are also illustrative, not runnable implementation.

   Evidence: plan `:339`, `:393`, `:550`, `:556`, `:557`, `:616`. The generated-text issue was checked against Google's current function documentation, linked below.

10. **Medium: resource and build order need reconciliation.** Creating a second infrastructure stack and ingest path is unnecessary. The statement that Terraform owns only datasets contradicts existing Terraform-owned source tables and load jobs; preserve that ownership for current sources and define one owner for each new derived object. Rebuilds should produce validated run artifacts before advancing the serving version, rather than expose half-refreshed interdependent tables.

    Keep WO vector retrieval in BigQuery initially; its vector index is optional. Existing Vertex AI Search remains appropriate for the IPC branch. A separate Vertex Vector Search endpoint for WOs should follow measured latency/scale needs. Stage A also says to defer the connection until Step 08 although Step 04 needs it; provision the connection before embeddings if using remote models, and defer only unnecessary serving infrastructure. Add BigQuery Connection API/identity configuration when that phase is reached.

    Evidence: plan `:134`, `:138`, `:244`, `:380`, `:605`, `:735`; `deployment/terraform/single-project/analytics.tf:37`, `:79`, `:110`; handover `:140`.

## Revised delivery sequence

| Increment | Concrete deliverable | Acceptance evidence |
|---|---|---|
| 1. Source audit and canonical views | Extend current parser only where needed; document field coverage, source/version/aircraft/change keys and source completeness; add proposed `v_work_orders`, `v_component_changes`, `v_observed_swaps` over existing nested data. Keep FAA separate. | XML/normalized/view counts reconcile; synthetic multi-step/multi-action/multi-change fixtures do not multiply unrelated arrays; NG/MAX/unknown scope is explicit; reload/revision policy is documented. |
| 2. Useful BQ agent | Add bounded tools for coverage, observed-swap rankings, component history and related FAA reports to the existing BQ leaf. Define source and error contracts; prepare runtime IAM changes in the current Terraform module. | Reviewed SQL results match tool results and cited answers; no replacement count is called a failure rate; missing/out-of-scope data produces honest unavailable results; IPC still retrieves with citations. |
| 3. Historical-case retrieval | Keyword baseline, then symptom-oriented BigQuery embeddings/vector retrieval where it improves reviewed relevance. Preserve source IDs, excerpts, family/component filters and availability cutoffs. | Keyword/vector comparison on the same reviewed cases, correct filtering, no-match behavior, citation support and measured latency/cost. An external vector endpoint is not required. |
| 4. Retrospective relationship experiment | Select one family with usable episode support; preserve replacement-event identity; enforce date/installation eligibility where established; compare L0/L1, L2 and optional L3. | Independent support counts, reviewed uncertain/rejected cases, bounded call costs, development/held-out separation and no hindsight leakage in any prospective replay claim. Stop linked-event claims where installation evidence is missing. |
| 5. Descriptive interval summaries | Publish only reviewed, supported historical intervals with clear population, event grouping, cutoff and limitations. | Counts are not inflated by repeated parts or precursor WOs; quantiles are labeled descriptive; no calibrated confidence or remaining-life claim is generated. |
| 6. Conditional prognosis | Only if complete enough installation/outcome/follow-up data becomes available: define one target/horizon, build censoring-aware cohorts and compare a simple statistical baseline. | Time/asset-aware validation, calibration and useful alert metrics for risk; appropriate censoring-aware metrics for timing. Otherwise keep prediction unavailable and continue delivering retrieval. |

The next implementation ticket should cover increments 1-2: **finish the existing BigQuery specialist with canonical views, bounded tools, grounded answers and meaningful evaluation**. It should not depend on proving all L0-L3 stages or purchasing a second vector serving path.

Preserve the existing model, IPC configuration and single-turn workflow semantics. Deployment remains a separate action governed by `CLAUDE.md`; this review does not require deployment. Real AMOS event ingestion, scheduled rebuilds, full incremental processing and combined IPC/BQ answers can be separate increments once their interfaces are defined.

## Suggested BQ tool contract

Proposed names below are implementation suggestions, not existing functions:

- `get_data_coverage(aircraft_family, date_range)` returns observed source scope and gaps.
- `rank_observed_replacements(aircraft_family, date_range, limit)` returns change counts, distinct WO/episode support where defined, and the precise replacement predicate.
- `get_component_history(component_scope, aircraft_scope, date_range)` returns dated, traceable records without implying a proven installation chain.
- `find_similar_work_orders(symptom, aircraft_family, component_scope, as_of, limit)` adds historical retrieval in increment 3.
- `get_related_faa_reports(part_number, limit)` returns separate public-report evidence; normalized part-number matches are candidates, not evidence of a shared physical asset.

Each response should include `status`, applied scope/filters, source tables and record IDs, `data_cutoff`, actual support counts, returned-row count, truncation, and limitations. Distinguish no data, unsupported request and tool failure. Include historical actions as evidence, not as approved instructions for another aircraft. Keep prediction/timing status explicit when unavailable.

## Checks and remaining limits

Completed: three specialist reviews; direct repository/config/schema inspection; full local XML and normalized-row aggregate profiling; local reproduction of swap/top-component/L0-L1 logic; current Google documentation checks. No L2/L3 relevance scores or predictive performance were measured. Existing scaffolding tests were inspected rather than run because they do not validate this plan and some invoke live models.

Still requiring evidence during implementation: live table contents and runtime access, parser fidelity beyond aggregate coverage, stable identity across future exports, source revision availability, installation/follow-up completeness, domain-reviewed event/component definitions, approved embedding-model compatibility and measured retrieval quality. None prevents beginning deterministic analytics over the current snapshot with explicit limitations.

Google references checked on 2026-09-22:

- [ML.GENERATE_TEXT output contract](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/bigqueryml-syntax-generate-text#output)
- [BigQuery vector search and optional vector indexes](https://docs.cloud.google.com/bigquery/docs/vector-search-intro)

## Follow-up: knowledge-base provisioning for a fresh project

The user also reported that a colleague could not create the knowledge base from scratch. The exact colleague error was not supplied during the review; inspection found reproducible configuration and import-flow defects:

- Reusable defaults adopted the lab datastore and disabled ingestion. Fresh defaults now create `ipc-part-numbers` and import the supplied PDFs; `vars/env.tfvars` keeps the existing lab's explicit adoption and no-import settings.
- Import lacked a datastore-creation dependency and did not wait for the schema-update operation. Both dependencies are now enforced.
- Same-path PDF changes were not sufficient to refresh ingestion. Terraform tracks PDF and script content hashes, and the importer requests content refresh.
- Import completion could conceal partial/empty results. The script checks expected document counts, operation/error responses and bounded readiness/IAM retries.
- Fresh-project API/build identity prerequisites were incomplete. API management now includes Storage, BigQuery Connection and Compute; the existing build grant resolves the default Compute identity after API enablement.
- Incremental import preserves documents outside the manifest. Empty source inventories fail unless an empty datastore is explicitly requested; the original project's model, agent code and datastore settings are unchanged.

The [README fresh-project guide](README.md#recreating-the-knowledge-base-and-infrastructure-with-terraform) and [new-project variables example](deployment/terraform/single-project/vars/new-project.tfvars.example) document credentials, separate project state, inputs, plan/apply and verification. Terraform validation, mocked plan tests and offline importer tests verify the implementation locally. A real first apply and search in the colleague's project remain unverified; organization policy, billing, permissions and eventual indexing must be checked there.
