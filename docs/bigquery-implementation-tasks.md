# BigQuery work-order implementation tasks

Source: `BIGQUERY-AGENT-plan.md` (2026-09-22). User authorized implementation
with lower-cost implementation agents; the orchestrator owns integration and
acceptance. Existing Gemini application model and ordinary IPC/ADK/A2A routes
remain in scope for regression checks. Cloud deployment is a separate action.

## Task boundaries and acceptance

| ID | Owner/model | Deliverable | Dependencies | Acceptance |
|---|---|---|---|---|
| B1 | parser / GPT-5.6 Luna | Pure shared AMOS parser, legacy ingestion adapters and wheel packaging | None | Multiple WOs, namespace/date handling, input limits, XML safety, identity diagnostics; existing schema preserved |
| C1 | analytics_views / GPT-5.6 Luna | Canonical WO/step/change/removal logical views and runtime read IAM | None | Parent-child unnest only, stable change keys, outgoing PN attribution, no-change WOs preserved; Terraform validation |
| C2 | analytics_views / GPT-5.6 Luna | Full FAA import alongside existing subset and retrieval artifact schemas | None | Both PN roles, raw counters/quality, versioned source; no FAA-to-AMOS asset links |
| D1 | cohort_audit / GPT-5.6 Terra | All-PN interval feasibility audit and frozen temporal/episode split | Existing fixed corpus | Actual support by PN, no invented negatives/failure labels, duplicate/episode purges, explicit failed gates |
| B2 | upload_service / GPT-5.6 Terra | Typed analysis context, target resolution and XML upload/artifact adapters | B1 | Actual upload for each target, explicit selection, new/replay provenance, honest prediction/policy status |
| C3/F1 | cohort_audit / GPT-5.6 Terra; orchestrator integration | Bounded history executor and keyword/vector/hybrid retrieval | C1/C2 | Parameterized values, allowlisted identifiers, query limits, as-of/source filtering, deterministic ranking/deduplication |
| F2 | embedding_pipeline / GPT-5.6 Terra | Canonical documents and resumable versioned embedding batch | D1/C2 | Exclude final-test/unknown availability, shared vector config, cache/dimensions/error coverage |
| G1 | orchestrator with implementation agents | Integration, upload fixtures, reproducible commands and validation report | B2/C3/F2 | Deterministic tests, real upload path, existing IPC regression coverage, precise live-validation gaps |
| D2/E | conditional | Model fitting and replacement policy | Valid D1 labels/follow-up, agreed horizon and reviewed policy | Held-out target-specific metrics; remain unavailable if prerequisites fail |

Agents own disjoint files; follow-on work starts when its dependencies expose a
stable interface. No task may populate failure probabilities or cycle deadlines
from retrieval scores, aircraft closing-counter summaries, or unreviewed labels.

## Shared interfaces

- `amos_data.parser.parse_workorders(xml_bytes, source_name=None,
  artifact_version=None)` returns a `ParseResult` with `workorders` (existing
  nested batch-schema dictionaries), `diagnostics`, and `upload_hash`.
- `amos_data` stays importable without agent/model initialization or credentials.
- Serving receives explicit `new_work_order` or `historical_replay` mode and
  analysis timestamp; user counters retain their source and observation time.
- Forecast/recommendation fields use null values and a reason whenever their
  distinct model, evidence, input or policy gates fail.
- BigQuery remains in configured project, `pma_agent_analytics`, `us-central1`.
  Offline artifact writers and read-only serving have separate responsibilities.

## Validation record

Implementation and review are complete for B1, B2, C1, C2, C3/F1 and F2. D1's
automated fixed-corpus audit is complete and fails the failure-risk gate. D2/E
remain unsupported by the fixed dataset and unimplemented beyond a guarded
training template and explicit unavailable response fields. Engineer-adjudicated
triage/removal labels and their model experiments are still absent.

G1 includes the upload fixtures, integration checks, reviewed-label evaluation
harness and run instructions. Cloud loading/deployment, runtime-principal IAM
verification and independently reviewed retrieval comparisons are outstanding.
The live IPC regression run was partial (one passed case, one timeout).

The team used Luna for parser/SQL work and Terra for audit, upload, retrieval,
embedding and evaluator work. When Luna authentication failed during follow-on
tasks, Terra agents and the orchestrator completed those tasks. The orchestrator
reviewed the shared interfaces, corrected integration defects and owned final
acceptance; the application's Gemini model was preserved.

See [validation evidence and remaining gates](bigquery-implementation-validation.md)
for exact counts and checks. A passing deterministic test validates software
contracts; it does not establish predictive validity, reviewed retrieval
relevance, deployed runtime IAM, or a live model's performance.
