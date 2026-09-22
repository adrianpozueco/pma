# BigQuery work-order implementation validation

Date: 2026-09-22. Source plan: [`BIGQUERY-AGENT-plan.md`](../BIGQUERY-AGENT-plan.md).

The XML evidence path is implemented and tested. The requested predictive
objective remains unmet: this fixed corpus has no established failure-risk
training cohort or validated replacement policy. No failure probability, cycle
forecast or replacement deadline is available. No infrastructure was applied and
no application was deployed by this implementation.

## Implemented behavior

- Shared, credential-independent AMOS parsing with legacy ingestion adapters,
  XML safety/size/depth limits, multiple-WO selection and provenance.
- Raw/multipart `/workorders/analyze` and session-scoped ADK artifact adapter;
  explicit new/replay modes, three-PN target resolution, separate issue/closing/
  supplied-current counters, stale/regressed-counter handling and as-of masking.
- Six canonical SQL views, separate full FAA table/schema, physical document and
  embedding schemas, and runtime job/read IAM declarations in the existing stack.
- Frozen all-PN candidate audit and temporal/episode split; guarded BigQuery ML
  template that refuses this corpus instead of treating removals as failures.
- Versioned text preparation, held-out/duplicate exclusion, bounded embedding
  batches, resumable validated-vector cache and explicit staging/MERGE writers.
- Parameterized keyword, exact-vector and hybrid retrieval with corpus/model
  pinning, availability and self-record filters, case-level ranking/deduplication,
  source-aware citations, budgets, cancellation and explicit query failures.
- Offline evaluator for independently reviewed relevance labels and precomputed
  semantic vectors, including per-PN results and AMOS/FAA source comparison.

Ordinary IPC/BigQuery chat and the existing Gemini model remain unchanged. The
upload API returns structured evidence and local IPC catalogue references;
applicability remains unverified. It does not claim a live manual lookup or
attach unreviewed installation/removal links as timing evidence.

## Fixed-corpus findings

The shared parser reconciled **8,259 XML files / 8,259 WOs** exactly to the
checked-in NDJSON. The uncompressed source SHA-256 is
`8de019b32907e9da954e812e0b9d8c68d46b8ba514655b0afe634066b9cda359`.
Source snapshots and the user's original planning/audit files were preserved.

The full FAA importer produced **196,404 rows** locally. The existing 299-row
FAA subset remains separate. No full generated CSV was added to the repository.

| Scope | Candidate on/off intervals | Unflagged candidates | Related episode groups |
|---|---:|---:|---:|
| All PNs | 1,535 | 1,371 | 1,017 |
| Nozzle `2085M31G03` | 5 | 5 | 1 |
| Boiler `62197-301-001` | 343 | 294 | 223 |
| Oven `8201-11-0000-01` | 142 | 129 | 94 |

The oven total includes three A320 candidates. These are observed candidate
intervals, not verified uninterrupted installations, symptom lead times or
failures. Each target has **zero reviewed usable failure labels, valid negatives,
complete component origins and training-eligible rows**. The frozen train cutoff
is 2026-02-09 and development cutoff is 2026-05-11; newer target episodes are held
out. See [model readiness](bigquery-model-readiness.md) and the
[v2 audit](audits/bigquery-feasibility-v2-2026-09-22.json).

Current preparation yields **197,527 canonical documents**: **6,378 eligible
AMOS** documents and **191,149 context-only FAA** narratives. It excludes 375
final-test WOs, 1,372 purged WOs, 144 duplicate/ineligible WOs, 5,260 repeated text
units and one protected held-out/duplicate chunk hash. These exclusion counts
have different units and are not a single WO total. FAA submission/difficulty
dates are not publication proof, so those narratives are not embedded or served
without a separately verified snapshot timestamp.

Reference corpus: `f2-087c13b95df4d5cc9023`; preparation: `f2-normalize-v1`;
split freeze: `3ab7ac6095c1a4b96a9339a4bc25be60c9a11e4253b3a94302e886d6d2b3ceae`.
Default embedding contract: `gemini-embedding-001`, version `001`, dimension
3072, `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY` tasks.

## Verification

| Check | Result and limit |
|---|---|
| Deterministic Python suite | 108 passed: `tests/unit` plus `tests/integration/test_workorder_upload.py`; nine third-party warnings |
| Parser reconciliation | Every parsed record matched the checked-in source representation |
| Frozen audit rerun | Audit, split manifest and per-WO index verified unchanged; training command correctly exits 3 |
| Preparation / embedding dry-run | Full preparation coverage computed; ten eligible sample documents emitted; embedding dry-run passed |
| Six SQL templates | Real BigQuery dry runs passed; AMOS queried against the existing table, FAA against a typed empty fixture because the new FAA table is not deployed |
| Live keyword/vector/hybrid SQL | All three passed against synthetic session temporary tables; included family, future, held-out, self-WO, text-hash, corpus and availability exclusions, plus AMOS/FAA source scope |
| Live embedding API | One synthetic query returned a valid nonzero 3072-dimensional vector in 2.242 seconds; no bulk embedding performed |
| Terraform | Module validation and the mocked analytics plan test passed (1/1); no apply or deployed IAM check |
| Packaging | Wheel built successfully; shared parser/retrieval/embedding and work-order modules confirmed present |
| Lint / whitespace | Ruff passed for new/modified implementation modules; `git diff --check` passed |
| Existing IPC live eval | Boiler identity/no-deadline case: 5/5. Nozzle quantity-not-cycle-limit case: timeout. This is a partial run, not a regression pass |

Ruff formatting was limited to implementation files. Whole-module Terraform
format checking still reports pre-existing formatting in `telemetry.tf`, which
was left unchanged. The full legacy live ADK/A2A integration suite was not rerun;
the upload integration tests and existing deterministic route tests do not
establish deployed end-to-end behavior.

The IPC trace is `artifacts/traces/traces_20260922_131925.json`; its local graded
result is `/tmp/caveman-ipc-eval/results_20260922_132251.json`. The eval CLI's zero
exit code did not mean every requested case completed.

## Remaining gates

1. Load approved document/embedding artifacts and deploy the declared views/IAM
   and application, then verify the actual runtime principal. The offline writer
   has contract tests but has not loaded a live production table.
2. Independently review retrieval relevance and no-match cases, then compare
   keyword/vector/hybrid and AMOS-only/combined retrieval on the frozen target
   holdout. No precision/recall, broad latency/cost or approximate-index claims
   are supported yet. FAA availability and aircraft mappings require review.
3. Resolve the IPC timeout and complete live IPC/ADK/A2A regression validation.
4. Engineer-adjudicated labels are needed for any separate triage or recorded-
   removal experiment. They cannot create missing component follow-up. The
   current fixed dataset does not establish that failure prediction is
   estimable; no further AMOS acquisition is assumed. Risk/timing models and a
   reviewed replacement policy remain unavailable.

Run commands are in the [README](../README.md),
[embedding instructions](bigquery-embeddings.md), and
[reviewed retrieval evaluator](bigquery-retrieval-evaluation.md). Team ownership
and acceptance boundaries are recorded in the
[atomic task board](bigquery-implementation-tasks.md).
