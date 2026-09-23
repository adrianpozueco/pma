# Handover: parallel evidence delivered, semantic work-order search next

Prepared 2026-09-23. Branch `feat/parallel-maintenance-evidence`, cut from
`9363519` on `feat/pm-agent-graph-scaffold`. Not pushed, not deployed.

Supersedes the "Atomic next tasks" section of
[next-agent-handover.md](next-agent-handover.md): N1-N5 are implemented. That
document's data limits, naming rules and deployment reference still apply.

## What was delivered

An uploaded XML work order now fans out to BigQuery and the IPC knowledge base
concurrently, joins both branches and returns one answer that keeps each
source's evidence distinct. Common BigQuery operations run as predefined
parameterized SQL behind typed Python tools; the model supplies variables and
does not write SQL for them.

| Commit | Task | Deliverable |
|---|---|---|
| `be336be` | N1 | `workorders/evidence.py`: `EvidenceRequest`, `SourceResult`, `SourceStatus`, merge/join helpers |
| `a392823` | N2 | `bq_analytics/queries.py` + `sql/`: bounded runner, `get_workorder`, `get_workorder_actions`, `get_component_changes` |
| `792c9ed` | N3 | `bq_analytics/adapters.py`: `get_part_history`, `get_part_coverage`, `find_historical_symptoms`, `find_faa_reports` |
| `1ee5453` | N4 | `ipc_manual_retrieval/evidence.py`: structured IPC adapter |
| `78a048a` | N5 | `nodes/evidence_branches.py` + graph fan-out, join and single composed answer |
| `065d866` | fix | BigQuery scalar serialization; branch error boundary closed |

Implementation was split across parallel lower-cost agents with disjoint file
ownership; the orchestrator owned the graph, review and acceptance. That worked:
no merge conflicts, and each agent's report was verified against disk and a
re-run of the suite before its commit.

## Graph now

```text
START -> prepare_workorder_upload
           "workorder_prompt"   -> display_workorder_upload        (selection/replay/error prompts)
           "workorder_evidence" -> bq_evidence + ipc_evidence      (concurrent)
                                     -> join_evidence -> compose_evidence_answer
           "chat"               -> router -> ipc | bq              (unchanged)
```

Prompts keep their own route, so a selection or cutoff question never triggers
a retrieval call. Ordinary chat still uses the existing exclusive router.

### Installed ADK 2.8.0 facts, verified in site-packages

- Fan-out is real concurrency: `_schedule_ready_nodes` starts each next node
  with `asyncio.create_task` and awaits `asyncio.wait(..., FIRST_COMPLETED)`.
- `JoinNode` sets `_requires_all_predecessors`; it fires only once every
  predecessor is `COMPLETED` and passes `{predecessor_node_name: output}`.
- Conditional edges are `(source, node, {"route": destination})`.
- Only `Event(content=...)` is visible in the ADK UI.

## Verified live

A real upload through the ADK dev UI with
`tests/fixtures/workorders/demo_nozzle_upload.xml` returned one answer in four
labelled parts. Both branches executed:

- `bigquery.get_part_history`: 20 rows across 2 distinct work orders (live).
- IPC: one document, `D638A002-RYR-0055`, Chapter 73-11, tagged
  `family_catalogue (unconfirmed_by_catalog)`.
- Work-order lookups returned `NO_MATCH`, correct for a synthetic WO absent
  from BigQuery, while its uploaded facts survived intact.
- Closing TAC was reported as not the current counter; failure probability,
  remaining life and replacement deadline were refused.

Tests: **241 passed** (`tests/unit tests/integration`), up from a 136-test
baseline. Ruff clean on every owned path; about 11 pre-existing errors remain
in unrelated test files.

## Known gaps

1. **`get_part_coverage` is uncallable.** It requires a mandatory `range_start`
   with no corresponding field on `EvidenceRequest`, so `bq_evidence` does not
   call it. Either add a coverage window to the request or rework the
   signature. This is the one outright defect.
2. **Symptom and FAA retrieval report `unavailable:no_history_provider_configured`.**
   Not a bug: `default_history_provider()` returns `None` unless
   `PM_HISTORY_CORPUS` is set, and no `PM_HISTORY_*` values exist in `.env`.
   The document corpus and embeddings were never loaded, so setting the
   variable without loading data would convert an honest "unavailable" into a
   misleading empty result.
3. **IPC "revision" metadata is unobtainable.** The installed
   `google-genai` `GroundingChunkRetrievedContext` has no revision field at
   all, and `page_number` is documented as unsupported on Vertex AI. The plan
   and the earlier handover both promise document/page/revision; only document
   name, title, uri and text can ever be returned. Correct those documents.
4. **`AircraftContext.position` is always `None`** - `analyze_xml`'s
   `parsed_context` does not expose position info. Left unguessed.
5. **Exclusion text hashing diverges slightly** from `service._history()`,
   which hashes the joined query text that `analyze_chat_upload` does not
   expose. Each symptom is hashed individually instead.
6. **N4's Vertex error-to-status mapping is unexercised.** The
   `PERMISSION_DENIED` / `UNAVAILABLE` / `TIMEOUT` classification was written
   against the SDK's exception types, never against the live datastore. It is
   isolated in `_default_search` and easy to correct.
7. **`tests/integration/test_server_e2e.py` is inconclusive.** Runs failed with
   `NameResolutionError` for `oauth2.googleapis.com` inside the spawned server,
   and a rerun gave a different failure count. The host resolves that name
   fine. Re-run from an environment with reliable network before trusting it.
8. **Eval datasets are stale.** The two upload evals still assert that no
   BigQuery or IPC lookup occurs. The live run above disproves that. Update
   them before relying on any eval score.

## What the serialization bug taught

240 tests passed over a hole: every BigQuery client in them was a fake
returning strings, so no test ever produced a `datetime`. The first live query
raised `TypeError: datetime is not JSON-serializable evidence data`. Worse, the
branch error boundary called `to_dict()` outside its own `try`, so the failure
escaped and aborted the join instead of degrading one source.

Both are fixed, and the contract now coerces `datetime`/`date`/`time` to
ISO-8601, `Decimal` to an exact string rather than a rounded float, and `bytes`
to hex, while still rejecting genuinely unknown types. The general lesson
stands: mocked backends cannot establish that a live backend works. Treat a
live smoke test as a required step, not a nicety.

## Next steps

Rough order of value.

1. **Update the two upload evals** to the behaviour that now exists, retaining
   independent upload, provenance and replay coverage. Until this is done, eval
   scores assert a contract the code deliberately no longer honours.
2. **Correct the IPC citation claims** in
   [adk-parallel-evidence-plan.md](adk-parallel-evidence-plan.md) and
   [next-agent-handover.md](next-agent-handover.md) to match what the SDK can
   return.
3. **Fix `get_part_coverage`** so the fifth query becomes callable.
4. **Prepare for the incoming BigQuery views.** Table identity is already
   injectable (`QueryRunner.run(table_placeholders=...)`, gated by
   `_ALLOWED_TABLES`), but the current templates inline the parent-child UNNEST
   chains that the canonical views subsume, so each template needs a rewrite
   rather than a rename. Build a parity harness that runs both shapes against
   the same WO/PN and asserts identical rows, serial pairs and the three
   counting units, so the switchover is a config flip with a green diff.

### 5. Semantic search over work-order embeddings (new request, not designed)

The next requirement is to embed an uploaded XML's action and description text
at query time and match it against an embeddings column in the new BigQuery
views. Design was started and deliberately stopped before implementation.

What already exists and should be reused rather than rebuilt:
`BQHistoryProvider._vector_sql` runs BigQuery `VECTOR_SEARCH` with COSINE
distance, as-of filtering and per-record dedup, and enforces a full
vector-space compatibility contract - a query embedding is only compared
against stored vectors matching on `model_id`, `model_version`, `dimension`,
`embedding_task`, `text_preparation_version` and `corpus_version`. A mismatch
raises instead of returning plausible nonsense. `VertexEmbeddingClient` and
`EmbeddingConfig` already produce conforming query embeddings.

**The view schema is not yet decided, which is the cheapest moment to state
requirements.** Ask the team building the views for:

- the six provenance columns above, so compatibility stays checkable in SQL
  rather than assumed;
- `embedding_status`, so partially embedded rows can be excluded;
- an as-of/availability column, so replay honours the same cutoff the rest of
  the evidence path obeys;
- a stable join key back to the work order and to the exact text that was
  embedded, so a hit can cite what it matched.

**Open design question, unanswered:** what one row represents - one vector per
action/description (keeps findings separate from completed actions and lets a
hit cite exact text), one vector per work order (simplest, but blends
pre-event symptoms with post-event completed actions, which the data limits
forbid), or per-text with chunking (best recall, adds chunk-to-record dedup and
makes chunking parameters part of the compatibility contract). Resolve this
before any implementation.

## Verification commands

```sh
uv sync
UV_CACHE_DIR=/tmp/caveman-uv-cache uv run --offline --no-sync \
  pytest tests/unit tests/integration -q
UV_CACHE_DIR=/tmp/caveman-uv-cache uv run --extra lint ruff check pm_agent

# ADK dev UI; attach tests/fixtures/workorders/demo_nozzle_upload.xml
UV_CACHE_DIR=/tmp/caveman-uv-cache agents-cli playground
# http://127.0.0.1:8080/dev-ui/?app=pm_agent
```

Confirm parallelism from the trace, not from prose: look for sibling
`bq_evidence` and `ipc_evidence` spans in `event.node_info.path` overlapping
before `join_evidence`. Restart the playground after code changes; a running
server keeps the code it started with.

## Constraints that still hold

- Application model stays `gemini-3.8-flash` in `pm_agent/config.py`.
- Do not rename `pm-agent` (project), `pm_agent` (app/package) or `pma-agent`
  (deployed infrastructure).
- Generic `execute_sql` and forecasting tools were deliberately left in
  `bq_analytics/agent.py`: ordinary chat still depends on that specialist, so
  removing them is a separate decision, not part of this milestone.
- No Terraform apply, no deployment and no corpus/embedding load was performed.
- `frontend/`, `docs/ryanair-ui-plan.md` and `docs/claude-frontend-handover.md`
  belong to separate work and were left uncommitted on purpose.
