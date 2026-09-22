# Handover: parallel BigQuery and knowledge-base evidence

Prepared 2026-09-22. Baseline: `9363519` on
`feat/pm-agent-graph-scaffold` in `/Users/kulagas/caveman-agent`.

## Start here

The next implementation should make work-order analysis query BigQuery and the
IPC knowledge base concurrently, then produce one answer with traceable sources.
Common BigQuery operations should use predefined, parameterized SQL behind typed
Python tools. The model supplies variables, rather than writing SQL each time.

The existing ADK file-upload integration is complete. Extend it; do not rebuild
it or repeat the earlier parser/data audit. Read
[the next-step plan](adk-parallel-evidence-plan.md) for the intended behavior,
then use the task breakdown below to implement it incrementally.

The user requested an orchestrator with atomic tasks delegated to lower-cost
implementation agents. Use disjoint file ownership: Luna is suitable for bounded
SQL/tool work, Terra for adapters and contract work; the orchestrator owns the
graph integration, review and final acceptance. These are implementation-agent
choices, not changes to the application's Gemini model.

This handover documents next work; it does not start that implementation. The
previous deployment and merge were explicitly requested and completed. Follow
the next user's instructions for further implementation/release scope; no
Terraform apply or bulk data/embedding load was performed in this work.

## Repository and release state

| Item | Verified state |
|---|---|
| Current branch | `feat/pm-agent-graph-scaffold`, pushed and clean before this document |
| Upload branch | `feat/adk-workorder-upload`, merged back into its parent by fast-forward |
| Merge/head revision | `93635194a09999d22ff90e15b9b5437636d21c97` |
| Application implementation | `72016cd` |
| Deployed revision | `853c94725a42b19221a8d115dfe7bc8a1a23d9b3`; later commit `9363519` only records deployment/docs |
| Remote | `https://github.com/adrianpozueco/pma.git` |
| `main` | Was not merged or changed in this task |

Start a new feature branch from the current parent for the next implementation,
after checking for intervening user changes. Suggested name:
`feat/parallel-maintenance-evidence`.

Do not confuse these deliberately different names: `pm-agent` is the project,
`pm_agent` is the ADK app/package, and `pma-agent` is the deployed infrastructure
name. Do not rename them.

## What works now

```text
START -> prepare_workorder_upload -> XML/follow-up -> display_workorder_upload
                                 -> ordinary chat -> router -> IPC OR BigQuery
```

- ADK's actual file input sends `inlineData` with `displayName`, `mimeType` and
  base64 `data`. The graph parses those bytes through the shared AMOS parser.
  No separate multipart upload or BigQuery import is required.
- Uploaded XML is authoritative. A database row with the same WO number is not
  substituted for it. The synthetic demo works without a database lookup.
- Artifacts are scoped to app/user/session and pinned by filename, version and
  SHA-256. Version **0** is valid. External URLs and cross-session references
  are rejected; there is no filesystem-path fallback.
- Up to 10 XML files / 25 MiB combined are accepted. XML validation occurs before
  saving reusable artifacts; limits also cover referenced artifacts.
- Multiple files prompt for `filename="FILE.xml"`; multiple WOs prompt for
  `selected_wo_id=NUMBER`. Follow-ups reuse the saved artifact. Invalid follow-up
  choices do not poison the last valid analysis, and invalid new uploads clear
  prior upload context.
- Closed exports default to `historical_replay`, open exports to
  `new_work_order`, with current UTC as the default cutoff. Explicit
  `analysis_as_of` controls replay; ambiguous earlier dates prompt for a value.
- Findings and completed actions are separate. Earlier replay excludes later
  snapshot findings/actions. Issue, closing and supplied-current TAC retain
  distinct provenance.
- The upload response is deterministic and currently makes **no BigQuery or IPC
  calls**. It explicitly says so. `WorkOrderAnalysisService()` is constructed
  without a history provider on this chat path.
- Ordinary chat still uses the existing exclusive IPC/BigQuery router. Its
  BigQuery specialist still exposes generic SQL and forecasting tools; replacing
  that surface is outstanding work.

The separate `/workorders/analyze` API can instantiate the existing bounded
history provider when configured. Its capability must not be mistaken for a
connected chat evidence branch.

## Code map

| File | Role / reuse point |
|---|---|
| [pm_agent/agent.py](../pm_agent/agent.py) | Current graph and routing edges |
| [nodes/workorder_upload.py](../pm_agent/nodes/workorder_upload.py) | Upload preparation and final visible response |
| [workorders/chat.py](../pm_agent/workorders/chat.py) | Attachment detection, state/options, artifact pinning, recorded evidence, Markdown renderer |
| [workorders/artifacts.py](../pm_agent/workorders/artifacts.py) | Authorized session artifact loader |
| [workorders/service.py](../pm_agent/workorders/service.py) | `AnalysisInput`, target/context resolution, as-of masking, unavailable prediction/policy contracts |
| [amos_data/parser.py](../amos_data/parser.py) | Credential-independent shared XML parser |
| [amos_data/retrieval.py](../amos_data/retrieval.py) | `HistoryQuery`, local provider, eligibility and ranking contracts |
| [bq_analytics/history.py](../pm_agent/sub_agents/bq_analytics/history.py) | Existing `BQHistoryProvider`, bounded parameterized history queries |
| [bq_analytics/agent.py](../pm_agent/sub_agents/bq_analytics/agent.py) | Current generic SQL specialist/tool filter to replace incrementally |
| [ipc_manual_retrieval/agent.py](../pm_agent/sub_agents/ipc_manual_retrieval/agent.py) | Existing `VertexAiSearchTool`, datastore and short-query instructions |
| [analytics_views/](../deployment/terraform/shared/analytics_views/) | Six reviewed parent-child SQL projections; definitions are not proof of live views |
| [app_utils/services.py](../pm_agent/app_utils/services.py) | Shared session/artifact services used by ADK, A2A and runtime adapter |
| [fast_api_app.py](../pm_agent/fast_api_app.py) | ADK UI/API, XML HTTP endpoint and serving adapters |

The proposed unified request/source-result contracts are **not implemented**.
Existing `AnalysisInput`, `HistoryQuery` and upload dictionaries are reusable
pieces. In the plan, P3's upload milestone is delivered; it needs adaptation to
P1, not another upload implementation. P1, P2, P4 and P5 remain outstanding;
P6 currently covers uploads only.

## Atomic next tasks

Proposed new paths below are ownership boundaries, not existing files. Freeze
the interfaces in N1 before parallel implementers start.

| ID | Deliverable and suggested owner | Dependency | Acceptance |
|---|---|---|---|
| N1 | Typed evidence request and per-source results; Terra, new `workorders/evidence.py` | None | JSON-serializable context, upload provenance, cutoff, candidates, counters, exclusions; explicit success/no-match/unavailable/error/timeout status |
| N2 | Bounded shared SQL runner plus WO/header/action/change tools; Luna, new `bq_analytics/queries.py` and package `sql/` | N1 | Fixed templates and real BQ parameters, exact nested-row counts and serial pairs, allowlisted identifiers, budgets/timeouts/cancellation |
| N3 | Part history/coverage and existing symptom/FAA adapters; Terra, separate adapter module | N1, N2 runner interface | Reuse `BQHistoryProvider`; own-WO/text-hash/future exclusions, case deduplication, explicit corpus/method availability and counting units |
| N4 | IPC evidence adapter; Terra, new `ipc_manual_retrieval/evidence.py` | N1 | Real retrieved excerpts/citations; retained document/page/revision metadata where available; explicit empty/error results |
| N5 | Adapt upload preparation and integrate parallel branches, join and one final answer; orchestrator | N1–N4 | Both evidence branches overlap, use the same request, emit one answer, and handle one-source failure without stale data |
| N6 | Regression eval, real UI demonstration, runtime-principal checks and documentation; orchestrator with bounded test tasks | N5 | All three PNs, unknown synthetic WO, replay/self exclusion, source failure, citations, abstention, ordinary-chat/ADK/A2A regressions |

N2 and N4 can run independently after N1. N3 may start once the runner interface
is stable; do not let N2 and N3 edit the same file simultaneously. Keep the graph
under one owner's control.

### Contract and query requirements

The shared request should carry: original question, artifact filename/version/
hash, selected WO, mode/cutoff, resolved PN candidates, aircraft/family/position,
available symptoms, counters with provenance, and excluded IDs/text hashes.
Missing current TAC must not prevent descriptive evidence retrieval.

Each branch should return source status, records/excerpts, source identifiers,
citations, executed parameters, counts/units, truncation, and safe error details.
Distinguish unavailable/permission-denied/timeout from a successful zero-row
query. Scope intermediate results to the invocation so one branch cannot reuse
an earlier turn's evidence or overwrite the other branch's result.

Implement the planned tools: `get_workorder`, `get_workorder_actions`,
`get_component_changes`, `get_part_history`, `get_part_coverage`,
`find_historical_symptoms`, and `find_faa_reports`. Runtime tools should call
Python functions; optional CLI scripts can wrap them. Do not spawn shell SQL
scripts per request. Package SQL resources in the application image/wheel.

Use parameters such as `@workorder_number`, `@part_number`, `@as_of`, `@limit`.
Identifiers come from code-owned configuration/allowlists. Reuse reviewed nested
projections against existing tables first: the canonical views, full FAA table,
document corpus and embeddings have not been deployed/loaded by this work.
Remove generic `execute_sql` and forecasting tools from the normal evidence
workflow as predefined tool coverage replaces it. Unsupported intents should
remain explicit, rather than silently falling back to generated SQL.

### Graph and citation requirements

Use ADK fan-out/join after request preparation. Check the installed ADK 2.8.0
`JoinNode`/Workflow API before writing edges; skill examples may differ from the
installed version. Verify concurrent execution with deterministic synchronization
in tests, not merely by observing a shorter elapsed time.

Function-node events have `author="pm_agent"`; their identity is in
`event.node_info.path`. Routing is `event.actions.route`. Working conditional
edge syntax is `(source, node, {"route": destination})` or
`(node, {"route": destination})`, not `(source, destination, "route")`.

The final node must emit `Event(content=...)` so the ADK UI shows the answer.
Preparation can retain structured output for tracing; the current final node
intentionally avoids repeating the full JSON payload underneath its answer.

`VertexAiSearchTool` is built-in grounding: inspect `grounding_metadata` and
retrieved chunks rather than relying on visible function-call events. Retain
real metadata; the current specialist's title-based citation prompt is not
sufficient evidence of an exact page or revision. Distinguish catalogue
presence from applicability to the particular aircraft.

## Data and prediction limits to preserve

- Application model stays `gemini-3.8-flash` in `pm_agent/config.py`.
- Targets: `2085M31G03` (fuel nozzle), `62197-301-001` (water boiler),
  `8201-11-0000-01` (oven). Normalized keys remove punctuation; display PNs retain
  their formatting. Compare `part_key` when testing normalization.
- Count work orders separately from component changes. Preserve parent-child
  relationships when unnesting arrays; avoid cross-products.
- FAA reports stay separate from AMOS aircraft/serial installation histories.
  Submission/difficulty dates alone do not prove historical publication
  availability. Preserve the provider's availability checks.
- Closing aircraft TAC is not current TAC, component age or time to failure.
  Completed actions are historical evidence, not pre-event symptom features.
- The fixed corpus did not establish valid failure labels/component follow-up.
  Failure probability, remaining life and replacement deadlines remain
  unavailable. Retrieval similarity, historical intervals, IPC quantities and
  model prose cannot fill those fields.
- Full semantic history is unavailable until the matching corpus/embeddings are
  loaded. Never hide this by silently switching retrieval methods.

Older [implementation validation](bigquery-implementation-validation.md) and
[task-board](bigquery-implementation-tasks.md) documents describe the earlier
108-test, undeployed snapshot. Their parser/audit findings remain useful, but
their statements that the application was not deployed or full serving tests
were not run are superseded by this handover. Runtime BigQuery permissions,
full IPC relevance evaluation and corpus loading still remain unverified.

## Fixtures and acceptance demonstration

Use [demo_nozzle_upload.xml](../tests/fixtures/workorders/demo_nozzle_upload.xml)
with the actual ADK file control and the prompt:

> Analyse the attached work order. Check earlier BigQuery cases and the IPC
> knowledge base, and distinguish their evidence from the uploaded record.

Expected uploaded facts: WO `DEMO-XML-ONLY-2085`, marker `COPPER-FINCH-41`,
serial off `DEMO-OFF-41`, serial on `DEMO-ON-42`. Its SHA-256 is
`6a43e1f4631492bd18a4bbd3e60ea51273aa20917b9896a0001ceb041c5bbab7`.
It is deliberately synthetic; do not insert it into the historical corpus.

Follow up with `analysis_as_of=2026-09-01T09:15:00Z`. The completed snapshot's
later marker/actions/serials must be excluded from replay evidence. Both
retrieval branches must obey the same cutoff and appropriate self exclusions.

The real nozzle example is
[TRANSFER_WORKORDER_1789487041751.xml](../data/xml/TRANSFER_WORKORDER_1789487041751.xml),
WO `105177647`. It records four swaps. All four structured positions are `#2`;
do not map their serial pairs to narrative nozzle positions 1/2/7/8. Do not edit
the original data to make it match the narrative. Also repeat for boiler and oven
fixtures, no-match cases, one-branch timeout and missing current TAC.

Completion requires a trace proving uploaded bytes were parsed, overlapping
BigQuery/IPC execution, one answer with real source provenance, explicit partial
failures, and unchanged prediction/policy abstention. Describing the uploaded
file alone no longer meets the next milestone.

## Verification and local commands

Last completed checks: **136 tests passed** across `tests/unit` and
`tests/integration`; two upload evals scored **1.0/1.0**; Ruff passed. Actual
browser upload and deployed upload/artifact/replay checks passed. Deployed A2A
chat also answered successfully. This does not establish BigQuery query access
under the deployed principal or the future parallel graph's behavior.

```sh
uv sync
UV_CACHE_DIR=/tmp/caveman-uv-cache uv run --offline --no-sync \
  pytest tests/unit tests/integration -q

# Requires a running local server on 8080; use the explicit URL.
UV_CACHE_DIR=/tmp/caveman-uv-cache agents-cli eval run \
  --dataset tests/eval/datasets/adk-workorder-upload.json \
  --config tests/eval/upload_eval_config.yaml \
  --url http://127.0.0.1:8080 --app-name pm_agent \
  --output /tmp/adk-upload-next-eval --concurrency 1
```

Read the applicable Google Agents workflow/code/eval skills before implementation
and evaluation. Pure parser/graph contracts belong in deterministic tests;
LLM response quality and grounding belong in eval. Check actual scores, not just
CLI exit status. Existing upload evals explicitly expect no BigQuery/IPC lookup;
when adding parallel retrieval, update their assertions for the new intended
behavior while retaining independent upload/provenance/replay coverage.

Session-specific tooling notes:

- `/tmp/caveman-uv-cache` works as `UV_CACHE_DIR`; dependencies were synced.
- At handover, servers listen on 8080 (started for this task) and 18080 (older
  user server). Re-check processes before restarting; do not stop the older
  server merely to satisfy CLI auto-discovery.
- ADK UI: `http://127.0.0.1:8080/dev-ui/?app=pm_agent`.
- `agents-cli eval run` without `--url` hit a persistent/in-memory session
  conflict with the existing CLI server. Explicit `--url` works. Run from the
  project directory; the CLI requires its manifest even for URL-based eval.
- The eval CLI warns that `/apps/pm_agent/app-info` returns 400 and omits agent
  metadata. The deterministic upload metrics passed; richer trajectory/grounding
  evals should address or account for that omission.
- Docker's local daemon was stopped. Agent Runtime successfully built the image
  remotely. `gh` was unavailable; Git fetch/push worked.
- Localhost/cloud access can require sandbox escalation. Reuse approved scoped
  commands; do not print access tokens or secret environment values.

Temporary evidence may disappear; the checked-in tests/docs are the durable
record. Files currently available: `/tmp/caveman-predeploy-tests.log`,
`/tmp/caveman-upload-predeploy-eval/results_20260922_163912.json`,
`/tmp/caveman-upload-ui.png`, `/tmp/caveman-upload-browser.py`,
`/tmp/caveman-runtime-smoke.py`, `/tmp/caveman-runtime-smoke-result.json`, and
`/tmp/caveman-runtime-chat-smoke.log`. The browser helper uses Chrome DevTools;
the isolated headless Chrome session was stopped after verification.

## Deployment reference

| Setting | Value at handover |
|---|---|
| Project | `qwiklabs-asl-04-1726946cb8ab` (number `98892663275`) |
| Runtime / BigQuery region | `us-central1` |
| Runtime | `projects/98892663275/locations/us-central1/reasoningEngines/9071133107117096960` |
| Service account | `pma-agent-app@qwiklabs-asl-04-1726946cb8ab.iam.gserviceaccount.com` |
| Resource limits | 4 CPU / 8 GiB, preserved during update |
| BigQuery dataset | `pma_agent_analytics` |
| Existing source tables | `wo_workorders`, `faa_sdr_wo_parts` |
| IPC datastore / location | `ipc-part-numbers_1789998929768` / `global` |
| Artifact bucket | `qwiklabs-asl-04-1726946cb8ab-pma-agent-logs` |
| Deployed version | `AGENT_VERSION=853c947`; `git-sha` label records the full commit |

Use [deployment_metadata.json](../deployment_metadata.json), not old `None`
values in historical reports. The live upload smoke test used managed session
`6543846016527892480`, user `upload-deploy-check-ecbc9fa87c`, and
`deployment-check.xml` version 0. Its synthetic artifact was left available.

For a future authorized release, read the deployment skill and preserve the
existing runtime by specifying `--service-name pma-agent --update-only` and its
service account. Bare `agents-cli deploy` derives `pm-agent` from the manifest
and can create a different runtime. The CLI propagates `.env` values, strips the
reserved `GOOGLE_CLOUD_PROJECT`, and preserves other live environment settings
on update; inspect the concrete deployment configuration before executing it.

`Dockerfile` now copies both `pm_agent` and `amos_data`. `.gcloudignore` excludes
raw `data/` and local `artifacts/`; maintain those packaging fixes. A code deploy
does not apply the planned views/IAM or load documents/embeddings. Verify the
runtime principal separately, and keep such infrastructure/data changes distinct
from the graph implementation and its acceptance results.
