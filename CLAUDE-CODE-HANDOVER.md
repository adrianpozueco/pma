---
title: AMOS predictive maintenance - Claude Code implementation handover
date: 2026-09-21
type: resource
area: ai
status: active
tags: [research, handover, aviation, predictive-maintenance, rag, bigquery, google-cloud]
projects: ["[[airworthiness-directives]]", "[[internal-knowledge-rag]]"]
---

# Claude Code implementation handover

## 1. Mission and first action

You are taking over a Google Cloud course project for Sebastian Kulaga and a team described as three people. Build an evidence-based aircraft maintenance prototype around AMOS work orders, with public FAA reports as supplementary development data.

The intended workflow is: a new work order describes a symptom; the system retrieves compatible historical cases and, if the observed histories support it, estimates the risk or timing of a subsequent component replacement. Outputs support maintenance planning and remain recommendations.

**Start by inspecting the actual XML and CSV files, then implement a small, repeatable ingestion and retrieval baseline.** Do not spend the first iteration repeating the source research. Do not assume the XML schema or that work orders alone support time-to-replacement modelling.

The user confirmed on 2026-09-21 that your coding-agent environment has access to both XML and CSV inputs. Discover their paths from the supplied workspace/configuration. Do not ask the user to upload files again. If the XML path cannot be found in that context, request only the missing location while continuing with the CSV work.

## 2. Project location and present state

Known project root: `/Users/kulagas/work/boeing-maintenance-rag`. If your checkout is elsewhere, use that root instead; paths below are relative to the project root.

The root contains data, research, a completed project-description DOCX, and `scripts/verify.py`. At handover preparation, all **62 previously inventoried files** matched `catalog/files.csv`; no implementation beyond the integrity utility was present. No XML was found inside this particular project folder. The XML available to your environment may be stored separately. Adding this handover changes the inventory count.

The previous work did not create cloud resources, upload AMOS data, train a prediction model, deploy an agent or validate a live AMOS integration. Check for work added by others before implementing anything. Read applicable `AGENTS.md`, `CLAUDE.md` and existing project configuration, and preserve their work.

Read these files in order:

1. `README.md` and `data/README.md` for the package layout and actual supplied data.
2. `docs/bigquery-ml-agent-architecture-review.md` for the latest proposed architecture and checked Google capabilities.
3. `docs/amos-predictive-maintenance-data-reuse.md` for how FAA data differs from AMOS histories.
4. `docs/data-dictionary.md` and `catalog/schemas.json` for the actual CSV/JSONL fields.
5. `docs/research/bigquery-ml-sources-2026-09-17.csv` for the research sources.

The earlier `docs/course-plan.md` and `docs/project-description.md` describe the original public-data RAG scope and mention Vertex AI Search. The later BigQuery review is the more relevant recommendation for the AMOS proposal. Do not treat every historical architecture note as a simultaneous requirement. Originals and legacy extraction scripts are preserved under `archive/`.

## 3. Confirmed scope versus recommendations

| Confirmed context | Recommendations to validate against the data |
|---|---|
| This is a Google Cloud course project, inspired by Ryanair maintenance. | Begin with one component family on the 737-800. |
| Aircraft scope is Boeing 737-800 NG and 737-8200 MAX. No Airbus scope. | Separate NG and MAX data/model applicability; expand only when evidence supports it. |
| The team has AMOS XML, described as containing WOs, flight-cycle values and part in/out information. | Inspect the exact fields, observation periods and component links before choosing a prediction target. |
| Public FAA CSVs have already been collected and packaged. | Use FAA for extraction/retrieval development; AMOS should supply fleet-specific outcome histories. |
| A Google representative suggested BigQuery. | Use BigQuery for analytical tables, embeddings, vector retrieval and a first ML baseline. |
| The team proposed an offline Knowledge Builder and an online Decision Agent. | Implement a repeatable offline pipeline and a controlled online assistant, with statistical predictions separate from LLM explanations. |

No particular component, risk horizon, alert threshold, latency target or Google model version has been agreed. **The 200-cycle horizon in the research is illustrative, not a requirement.** The exact GCP project, region, available services and budget must come from existing team configuration or a focused clarification when needed. These are not prerequisites for local inspection, parser development or baseline retrieval.

## 4. What the CSV data actually is

The CSV files are **public FAA Service Difficulty Reports**, not AMOS XML converted to CSV. They describe reported defects/malfunctions, sometimes including investigations and maintenance actions. They are not complete airline maintenance logs, and a report is not necessarily a unique failure.

| Relative path | Records | Role |
|---|---:|---|
| `data/raw/faa-sdr/SDR-2023.csv` | 62,688 | Original annual export, multiple aircraft types. |
| `data/raw/faa-sdr/SDR-2024.csv` | 66,079 | Original annual export. |
| `data/raw/faa-sdr/SDR-2025.csv` | 67,637 | Original annual export. |
| `data/derived/faa-sdr/boeing_reports_2023_2025.csv` | 31,677 | Selected Boeing subset of the three raw files. |
| `data/derived/faa-sdr/finding_action_records.jsonl` | 31,677 | Same selected reports with heuristic finding/action splits. |
| `data/derived/faa-sdr/reference_bearing_reports.jsonl` | 23,865 | Overlapping reports with extracted manual references. |
| `data/derived/faa-sdr/candidate_reference_pairs.jsonl` | 22,082 | Filtered finding/reference candidates, not expert gold labels. |

The three annual files total **196,404 reports**. The 31,677 are a subset, not additional reports. Do not ingest the overlapping derived representations as independent incidents.

Raw annual files have 76 columns; the selected CSV adds `_year` for 77. Important fields include `Discrepancy`, `DifficultyDate`, `SubmissionDate`, `AircraftModel`, `RegistryNNumber`, `AircraftTotalCycles`, `JASCCode`, `PartName`, `PartNumber`, `PartSerialNumber` and `OperatorControlNumber`. Consult the dictionary for exact definitions and types. Read identifiers as strings and validate dates/numeric values explicitly.

Known issues to retain in the implementation:

- The selected Boeing file is predominantly NG, but contains **14 explicit `7378200` records**. Broader model mapping still needs validation. The raw files also contain **4,060 bare `7378` records**; do not assume all are MAX8200.
- **31,628 selected rows have a positive numeric aircraft cycle value**, but only **1,650 have a nonblank part serial number**. Populated counters do not establish linked component histories.
- **19,463 of the 22,082 candidate pairs contain only three-part section references**. Manual-reference strings do not include the actual Boeing manual contents.
- The 22,082 candidate pairs contain **20,352 distinct finding strings**. Additional near-duplicates and related reports can remain.
- **44.6%** of the selected records are ATA 53; **78.5%** carry an inspection/maintenance stage code. This is a skewed sample, not fleet incidence or complete ground-maintenance coverage.
- `data/reference/faa-sdr/jasc_codes.csv` contains 547 parsed rows, including extraction errors/headers. `HowDiscoveredCode` descriptions remain unresolved.
- FAA narratives often combine symptoms, actions, tests and supplements. Heuristic splitting does not prove those parts correspond to distinct events separated in time.

Source details are in `docs/audits/claude-sdr-data-audit-2026-09-10.json`. Broader handbook, image and prediction datasets remain catalogued leads unless explicitly marked downloaded. Do not claim they are part of the local corpus or block the first WO prototype on collecting them.

## 5. First deliverable: inspect and map the XML

Create an XML profile and field mapping before making assumptions about the AMOS export. Record file count, size, namespaces, root/repeating elements, WO count and date coverage. Inspect representative records and edge cases. Avoid copying raw internal narratives or identity mappings into public fixtures, logs or documentation.

For each relevant concept, document the actual XPath/element, sample type, coverage, meaning, transformation and unresolved questions:

- WO ID, revision/version and links between related WOs.
- Creation, event, update and closure dates; export/ingestion time separately.
- Original symptom text versus later action/rectification text. Determine whether the export preserves versions or only the final state.
- Aircraft identity and model, with reviewed NG/MAX classification.
- Aircraft cycle values and which event each counter belongs to. Resolve what the team's TAC field actually means.
- Part in/out, part number, physical-component identity, installation position and movement reason.
- Confirmed replacement timing and counter, planned/unscheduled status and component installation interval.
- End of reliable observation and evidence that an installation remains in service when no replacement is recorded.

Use consistent pseudonymous aircraft/component identifiers where needed. The same asset must retain the same key across records. Keep any reversible mapping separate from the learning/indexing data.

Produce a readiness assessment for three separate capabilities: **historical-case retrieval**, **risk within a cycle horizon**, and **time-to-replacement distributions**. Missing prediction evidence should not prevent a useful retrieval implementation.

Absence of a later WO is not by itself proof that a component survived a specified number of cycles. You may need aircraft exposure or installation inventory beyond the WO XML to establish follow-up. Flag this explicitly rather than manufacturing negative labels.

## 6. Proposed architecture

```text
AMOS XML -> Python parser -> typed JSON/Parquet -> BigQuery history tables
                                                |
                           offline cohorts, candidate links, embeddings, models
                                                |
new WO or local replay -> online service -> retrieve cases + obtain model score
                                                |
                                Gemini explanation with cited evidence
```

Keep FAA and AMOS records in distinct source namespaces/tables. Never join a public FAA event into an AMOS aircraft installation timeline because the narrative or part number looks similar.

Suggested analytical tables, adapted to the real XML: `work_order_versions`, `component_movements`, `component_installations`, `prediction_cohort`, `precursor_candidates`, `work_order_embeddings` and `predictions`.

The team's L0-L3 stages were not fully specified. A proposed implementation is:

| Stage | Responsibility |
|---|---|
| L0 | Parse and validate sources, versions, dates and counters. |
| L1 | Build deterministic installation/event eligibility and observation windows. |
| L2 | Retrieve semantically similar symptom candidates within compatible component/aircraft classes. |
| L3 | Bounded LLM evidence review with supported/contradicted/uncertain outcomes, followed by evaluation against engineer-reviewed examples. |

Statistical model training is a further explicit step. Embeddings and LLM review alone do not establish replacement probabilities or causal precursors. Record versioned evidence and review status instead of automatically calling generated links “trusted.”

Google documentation checked on 2026-09-17 established:

- XML needs parsing; native BigQuery load formats include JSON, Parquet, Avro, CSV and ORC. Nested `STRUCT` and repeated `ARRAY` fields can preserve multiple part movements.
- `AI.GENERATE_EMBEDDING` and `VECTOR_SEARCH` support semantic retrieval. Verify eligibility filtering and index behaviour on the actual table/index configuration.
- `AI.GENERATE` can produce structured extraction/review outputs; those outputs still need validation.
- BigQuery ML supports `LOGISTIC_REG` and `BOOSTED_TREE_CLASSIFIER`, plus prediction/evaluation functions. These algorithms train on your labels; they are not pretrained Boeing maintenance models.
- TimesFM through `AI.FORECAST` supports aggregate time-series forecasting, such as weekly replacement demand. It is not a direct model of an individual WO's remaining component life.
- The reviewed native model interfaces did not document a Cox/AFT survival model. Python survival modelling or a carefully implemented discrete-time hazard classifier are possible extensions.
- BigQuery vector search is sufficient for the initial WO retrieval path; a separate Vertex AI Search datastore is not required.

Recheck current availability, model IDs, region/edition requirements and function syntax when implementing. Reuse any existing approved project/model configuration. Do not copy model names blindly from archived research. If adopting Google ADK/agents-cli, use the available workflow, scaffold and code skills in that environment; preserve existing code and distinguish deterministic unit tests from end-to-end agent evaluations. ADK adoption itself is not a confirmed project requirement.

## 7. Prediction rules and evaluation boundaries

1. Define the target event, component installation, prediction timestamp and horizon before labels. Replacement is not automatically failure; record removal reason and decide how scheduled removals or other competing events are handled.
2. A horizon-positive label needs an observed qualifying event within the horizon. A negative needs reliable event-free follow-up through the horizon. Incomplete follow-up is censored/unknown, not automatically negative.
3. Future replacement information can be used to construct training labels. Features, indexed symptom text and online context must contain only what was available at prediction time. Later WO closure text is a common leakage path.
4. Split by time and keep related asset/installations and duplicate episodes appropriately grouped. Training labels must have been available by the simulated training cutoff. Default random splits are insufficient.
5. Do not count several WOs leading to one removal as several independent replacement events. Define episode grouping and how same-WO/zero-cycle replacements are handled.
6. Use a simple logistic model and keyword-retrieval baseline before more complex models. Test whether embeddings and LLM steps actually improve the intended metrics.
7. Start with risk over an agreed horizon if the data supports it. A full timing distribution requires a censoring-aware approach. A p50 is a median; p90 is a percentile; neither is a confidence score. Unestimable quantiles stay null with a reason.
8. The statistical model produces numerical risk/timing results. The LLM explains the supplied outputs and retrieved evidence. It must not invent probabilities, lead times or manual procedures.

## 8. Implementation sequence and expected artifacts

**Milestone 1: data audit and deterministic ingestion.** Produce `docs/amos-data-profile.md`, a machine-readable field mapping and quality summary, and a parser with typed normalized outputs under a separate prepared-data directory. Keep originals unchanged. Test XML namespaces, repeating elements, malformed records, missing fields, duplicate/revised WOs and counter/date interpretation. Use sanitized or clearly synthetic fixtures for code tests.

**Milestone 2: useful retrieval baseline.** Index canonical FAA reports and accessible AMOS symptom records with source namespaces and metadata filters. Start locally with a deterministic keyword baseline, then add BigQuery/vector retrieval when configured. Return traceable original record IDs and excerpts. Do not use the candidate manual-reference pairs as a falsely independent expert evaluation set.

**Milestone 3: linked AMOS cohort and first model.** If the audit supports it, construct installation-based event/follow-up cohorts, choose a narrow target and train/evaluate a horizon-risk baseline. If timing is required and support exists, add an explicit survival method. Otherwise document the precise missing evidence and continue delivering retrieval; do not substitute synthetic outcomes while presenting them as observed results.

**Milestone 4: online assistant and replay.** Build a small service accepting a new WO or a query, retrieving eligible cases and obtaining any supported model estimate. Use a replay of records in event order for the course demonstration. A file-arrival event can contain many WOs; process each eligible WO/revision once. Real AMOS event/API integration remains unverified.

**Milestone 5: measured evaluation and course handoff.** Produce an evaluation report, runnable commands and a clear account of what is implemented, measured and still limited. A later cloud deployment should follow the team's actual environment configuration and authorization; this handover does not itself create a cloud project or authorize publication of internal XML.

Suggested response contract, to adapt during implementation:

```text
request_id / wo_id / wo_version
aircraft_family / component_scope
answer
evidence[]: source, record_id, source_version, excerpt, location
prediction: status, target, horizon_cycles, risk, model_version
timing: p50_cycles, p90_cycles, estimation_limit, method
limitations[] / data_cutoff / processed_at
```

Prediction and timing fields must support unavailable/out-of-scope/insufficient-evidence states. An embedding distance is not a risk probability. A component support count is not a confidence score. Keep explanatory answers understandable to engineers rather than exposing pipeline terminology as the main user experience.

Evaluate deterministic parser/cohort/API logic with focused tests. Evaluate retrieval and generated explanations with reviewed questions and evidence: relevant-source retrieval, citation support, wrong-aircraft/component matches and insufficient-evidence handling. For risk, report calibration, precision/recall at useful alert thresholds and false alerts per eligible WO/exposure unit. For timing, use censoring-aware metrics and state follow-up coverage. Measure end-to-end latency separately. Do not invent target scores or report sample smoke tests as validated performance.

## 9. Working conventions and completion checklist

- Continue useful local implementation without repeatedly asking for permission for routine reversible work. Ask only for consequential unknowns that cannot be resolved from the data/configuration, such as an unavailable XML location or an undefined target event.
- Do not overwrite raw datasets or run legacy `archive/collection/scripts/` as the current pipeline. They assume the old layout and overwrite derived exports. Prefer new documented transformations with input/output configuration.
- The existing manifest describes supplied snapshots. Use `python3 scripts/verify.py` from the project root to verify them. Maintain provenance when adding or intentionally modifying documented files; do not update hashes merely to hide unintended changes.
- Use ordinary hyphens, not long dashes, in new prose, as requested by the user.
- Keep existing report counts, source dates and data-quality limitations accurate. Recount changed data and explain scope changes.
- No current complete AMM/FIM/SRM corpus has been obtained. Historical recorded actions must not be presented as approved instructions for another aircraft.
- Keep operational write-back out of the prototype: outputs are recommendations. Preserve stable pseudonyms and source traceability, and avoid leaking raw internal records into logs, fixtures or public repositories.
- Finish each implementation slice with what changed, exact run commands, checks/results and remaining evidence gaps. Preserve a working local baseline even if cloud configuration or prognostic data is incomplete.

## 10. Suggested first instruction to the coding agent

> Read this handover and the linked project research. Inspect the XML and CSV files available in your environment and preserve the originals. First produce the XML schema/coverage audit and implement a tested parser into typed normalized records. Then build a minimal similar-case retrieval baseline with source citations and NG/MAX/component filtering. Assess whether the AMOS histories support horizon-risk or time-to-replacement labels before training a predictor. Use BigQuery for the cloud analytical path when project configuration is available, and document decisions and measured results as you proceed.
