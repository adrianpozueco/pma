# pm-agent

A predictive-maintenance agent for Boeing 737-800 / 737-8200 fleet questions.
XML attachments in ADK chat go directly to the shared work-order parser and
analysis service. A router classifies ordinary questions and hands the turn to one specialist: an IPC
manual retriever backed by Vertex AI Search, or a BigQuery analytics agent over
AMOS work orders and FAA Service Difficulty Reports.

Scaffolded with `agents-cli` version `1.6.1` (`agents-cli-manifest.yaml`), built
on the ADK 2.0 workflow graph API.

The dedicated XML work-order API now parses uploads, resolves the three target
parts, and can retrieve dated AMOS/FAA evidence through bounded BigQuery queries.
Failure probabilities and replacement deadlines remain unavailable: the fixed
corpus did not establish valid failure labels and component follow-up. See the
[implementation validation report](docs/bigquery-implementation-validation.md)
and [atomic task board](docs/bigquery-implementation-tasks.md).

---

## Do not "tidy up" these names

Three similar-looking names are deliberately different. Changing any of them
renames or destroys already-deployed GCP resources.

| Name | Where it lives | What it is |
|------|----------------|------------|
| `pm-agent` | `agents-cli-manifest.yaml` `name:`, `pyproject.toml` `name` | The application / project name |
| `pm_agent` | `pm_agent/` package, `Workflow(name=...)`, `App(name=...)` | The Python package and the agent name in telemetry |
| `pma-agent` | Terraform `var.project_name` (`variables.tf`, `vars/env.tfvars`) | The naming base for **deployed GCP resources** |

`var.project_name = "pma-agent"` (note the `a`) feeds:

- the BigQuery analytics dataset `pma_agent_analytics` (`replace("${var.project_name}_analytics", "-", "_")` in `analytics.tf`)
- the BigQuery telemetry dataset `pma_agent_telemetry`
- the GCS buckets `<project_id>-pma-agent-data` and `<project_id>-pma-agent-logs`
- the service account `pma-agent-app@<project_id>.iam.gserviceaccount.com`
- the Reasoning Engine display name and the log sink name

The live dataset is `pma_agent_analytics` and `pm_agent/sub_agents/bq_analytics/agent.py`
hardcodes that exact string in `BQ_DATASET`. **Renaming `project_name` to match
the application name would destroy and recreate live infrastructure.** Leave it
alone.

---

## Project structure

```
pm-agent/
├── pm_agent/                          # Agent package
│   ├── agent.py                       # Workflow graph -> root_agent, app
│   ├── config.py                      # MODEL, project_id()
│   ├── fast_api_app.py                # FastAPI / A2A server entrypoint
│   ├── nodes/
│   │   ├── workorder_upload.py        # XML attachment preparation and response
│   │   └── router.py                  # Ordinary chat classifier sets ctx.route
│   ├── sub_agents/
│   │   ├── ipc_manual_retrieval/      # Vertex AI Search over the IPC datastore
│   │   └── bq_analytics/              # BigQuery specialist (placeholder)
│   ├── workorders/                    # XML analysis context and artifact adapter
│   └── app_utils/                     # a2a, services, reasoning_engine_adapter
├── amos_data/                         # Pure parser, cohorts, retrieval and embeddings
├── scripts/                           # Data preparation for BigQuery
│   ├── wo_xml.py                      # Shared XML parsing helpers
│   ├── xml_to_ndjson.py               # AMOS workorder XML -> NDJSON + BQ schema
│   └── build_faa_sdr_wo_parts.py      # FAA SDR filter -> CSV + BQ schema
├── data/
│   ├── xml/                           # 8259 AMOS transferWorkorder XML files
│   ├── faa-sdr/                       # SDR-2023/2024/2025.csv (raw FAA exports)
│   ├── ipc_part_numbers/              # IPC manual PDFs (source for the datastore)
│   └── processed/                     # Generated: uploaded by Terraform
├── deployment/terraform/
│   ├── shared/                        # BQ schemas (generated), completions.sql
│   └── single-project/                # The deployment root module
├── tests/                             # unit, integration, eval
├── CLAUDE.md                          # AI-assisted development guide
└── pyproject.toml
```

### The graph

`pm_agent/agent.py` checks for uploaded XML before classifying ordinary chat:

```
START -> prepare_workorder_upload -+-> "workorder" -> display_workorder_upload
                                  +-> "chat" -> router -+-> "ipc" -> ipc_manual_retrieval
                                                        +-> "bq"  -> bq_analytics
```

- **`prepare_workorder_upload`** parses current XML attachments or explicit
  follow-ups from a saved session artifact. It produces a deterministic answer
  from uploaded evidence; parallel BigQuery/IPC retrieval is a later step.
- **`router`** (`pm_agent/nodes/router.py`) handles ordinary chat. It runs a
  structured-output classifier sub-agent that returns `"ipc"` or `"bq"`, sets
  `ctx.route`, and returns the user's question as the chosen node's input. If
  the classifier call fails it falls back to a keyword list.
- **`ipc_manual_retrieval`** answers part-number and part-description questions
  from a Vertex AI Search datastore (`ipc-part-numbers_1789998929768`, location
  `global`), using `VertexAiSearchTool` with `max_results=10`.
- **`bq_analytics`** is a **placeholder**. It is importable, routable and
  pointed at the right dataset, but it only has the generic BigQuery toolset -
  no curated SQL tools or flattened views over the 211-field nested
  `wo_workorders` schema, and no eval coverage.

Specialists never call each other. Adding a domain means adding a leaf node and
a route key in both `agent.py` and `router.py`.

---

## Requirements

- **uv** - [install](https://docs.astral.sh/uv/getting-started/installation/)
- **agents-cli** - `uv tool install google-agents-cli`
- **Google Cloud SDK** - [install](https://cloud.google.com/sdk/docs/install)
- **Terraform >= 1.7** - only for the infrastructure work below (see the install note)
- **Bash, curl and jq** - used by Terraform's IPC document import script

---

## Setup and run

From a fresh clone to a working agent. Every step has a check, so a failure
shows up where it happened instead of three steps later.

### 1. Install the tooling

```bash
uv tool install google-agents-cli
```

Check: `uv --version` and `agents-cli --version` both print something.

### 2. Authenticate to Google Cloud

The agent talks to Vertex AI, Vertex AI Search and BigQuery through Application
Default Credentials.

```bash
gcloud auth login                        # the gcloud CLI itself
gcloud auth application-default login    # ADC, which the agent uses
gcloud config set project <your-project-id>
```

On the consent screen tick **every** permission box. If the
`cloud-platform` scope is not granted the command fails with:

```
ERROR: ... https://www.googleapis.com/auth/cloud-platform scope is required
but not consented. Please run the login command again and consent in the
login page.
```

If the browser handoff fails, use `gcloud auth application-default login --no-browser`.

Check:

```bash
gcloud auth application-default print-access-token | head -c 12
```

A token prefix means ADC is healthy. These credentials expire; see
Troubleshooting.

### 3. Configure `.env`

```bash
cp .env.example .env
```

```
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
IPC_DATASTORE_ID=your-datastore-id
```

`.env` is gitignored. It is read automatically by `pm_agent/config.py`, so
every entrypoint picks it up - the server, `adk web`, `pytest` and a bare
`import pm_agent`. `load_dotenv` never overrides a variable already set in the
environment, so a real deployment's values still win.

**`GOOGLE_CLOUD_LOCATION=global`** selects the model endpoint used by this app.
The IPC specialist separately builds a `locations/global` datastore path, so
keep Terraform's `knowledge_base_location = "global"` too.

**`IPC_DATASTORE_ID`** is the Vertex AI Search datastore behind the IPC
specialist. It is not hardcoded in the agent because it is environment
specific. Terraform owns it as `var.knowledge_base_data_store_id`, and
`service.tf` passes it to the deployed agent, so `.env` is only for local runs.

The id is always an **input**, never generated. `data_store_id` is a required
field on `google_discovery_engine_data_store`, so Terraform creates the
datastore under exactly the id you give it. That means there is no
chicken-and-egg: you can write the value into `.env` before the first apply.

- **Creating a new datastore** (the module defaults): choose a valid id, for
  example `ipc-part-numbers`, in your own `vars/my_env.tfvars` and `.env`.
  Follow [the fresh-project Terraform steps](#recreating-the-knowledge-base-and-infrastructure-with-terraform)
  to create it and import the manuals.
- **Adopting an existing datastore** (`adopt_existing_data_store = true`):
  the id is whatever the console assigned, including its numeric suffix,
  such as `ipc-part-numbers_1789998929768`. Look it up once and use it in both
  places. The checked-in `vars/env.tfvars` is the existing lab project's
  adoption configuration; it is not a template for a new project. With
  adoption enabled, an incorrect id causes an import failure.

To look up an existing id:

```bash
# from Terraform, if the module has been applied - this echoes the input,
# so it confirms the value rather than discovering it
cd deployment/terraform/single-project && terraform output knowledge_base_data_store_id

# from the API, which is the real source of truth for a console-created one
PROJECT=$(gcloud config get-value project)
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://discoveryengine.googleapis.com/v1/projects/${PROJECT}/locations/global/collections/default_collection/dataStores" \
  | jq -r '.dataStores[].name'
```

If it is unset the agent refuses to import, with a message naming the variable.

### 4. Install dependencies

```bash
agents-cli install
```

Check:

```bash
uv run python -c "from pm_agent.agent import root_agent, app; print(root_agent.name, '/', app.name)"
# pm_agent / pm_agent
```

That single command exercises the whole wiring: it resolves the project id,
reads `IPC_DATASTORE_ID`, builds both specialists and validates the graph.

### 5. Run it

```bash
agents-cli playground
```

Opens a local dev UI that reloads on save. `adk web` from the repo root works
too - it scans for directories containing an `agent.py` and will list
`pm_agent`.

To run the HTTP server directly instead:

```bash
uv run uvicorn pm_agent.fast_api_app:app --host 127.0.0.1 --port 8000
```

### 6. Verify end to end

Ask one question per branch and confirm each reaches the right specialist.

- **IPC branch:** `what is the part number for the convection oven`
  should return a part number with a `Citations` block.
- **BigQuery branch:** `how many work orders are there`
  should run BigQuery tools and cite the table it read.

Headless equivalent:

```bash
uv run python - <<'EOF'
import asyncio
from google.adk.runners import InMemoryRunner
from google.genai import types
from pm_agent.agent import app as adk_app

async def ask(q):
    r = InMemoryRunner(app=adk_app)
    s = await r.session_service.create_session(app_name=adk_app.name, user_id="u")
    async for ev in r.run_async(user_id="u", session_id=s.id,
        new_message=types.Content(role="user", parts=[types.Part(text=q)])):
        if ev.actions and getattr(ev.actions, "route", None):
            print("route:", ev.actions.route)
        if ev.content and ev.content.parts:
            for p in ev.content.parts:
                if p.text:
                    print(f"[{ev.author}] {p.text[:200]}")

asyncio.run(ask("what is the part number for the convection oven"))
EOF
```

A healthy IPC turn is about 4 events and under 30 seconds: the classifier, two
bookkeeping events from the graph, then the specialist's answer.

Then run the tests:

```bash
uv run pytest tests/unit tests/integration    # 7 passed
```

---

### Commands

| Command | Description |
|---------|-------------|
| `agents-cli install` | Install dependencies with uv |
| `agents-cli playground` | Local dev UI, auto-reloads on save |
| `agents-cli eval run` | Run and grade the eval dataset (`tests/eval/`) |
| `agents-cli deploy` | Deploy to Agent Runtime |
| `uv run pytest tests/unit tests/integration` | Unit and integration tests |
| `uvx ruff check pm_agent tests` | Lint (see gotcha below) |

### Testing gotchas

**`.env` is loaded automatically.** `pm_agent/config.py` calls `load_dotenv()`,
so `pytest`, `adk web` and a bare `import pm_agent` all pick it up, not just the
server. `load_dotenv` never overrides a variable already in the environment, so
a real deployment's values still win. Credentials are separate: run
`gcloud auth application-default login` if calls fail with a `RefreshError`.

**ruff is not installed in `.venv`.** It is declared in the `lint` optional
dependency group but not synced. Use `uvx`:

```bash
uvx ruff check pm_agent tests
```

### Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `RefreshError: Reauthentication is needed` | ADC expired. Re-run `gcloud auth application-default login`. Over HTTP this surfaces indirectly, as an SSE stream that ends early and a `ChunkedEncodingError` client-side, so check credentials before suspecting the code. |
| `cloud-platform scope is required but not consented` | The consent screen was accepted without ticking the permission boxes. Run the login again and tick all of them. |
| `RuntimeError: IPC_DATASTORE_ID is not set` | `.env` is missing or lacks the variable. See step 3. |
| `RuntimeError: No project id` | Neither `GOOGLE_CLOUD_PROJECT` nor ADC carries a project. Set it in `.env` or run `gcloud config set project`. |
| Model 404 | Usually `GOOGLE_CLOUD_LOCATION`, not the model name. The datastore is in `global`. |
| The dev UI shows stale behaviour, or an old agent name | An `agents-cli playground` server from an earlier session is still running. Check `.google-agents-cli/run_server.json` for its pid and port, kill it, and start a fresh one. |
| `vertex_ai_search` never turns green in the graph view | Expected. It is a model built-in grounding tool, so it produces no function call or response for the graph to highlight; retrieval happens server-side and comes back as `grounding_metadata`. Confirm grounding through the `Citations` block or `event.grounding_metadata.grounding_chunks`. |
| "System instructions were modified between consecutive turns" performance warning | Expected for a multi-agent graph. The router classifier and the specialist are different agents with different system instructions, and the dev UI compares consecutive model calls assuming one agent per session. |
| `test_adk_run_sse` fails intermittently in a full run but passes alone | A transient stream drop, not a defect. Re-run before investigating; the same test passes in isolation and via direct SSE. |
| Everything routes to one specialist | The router classifies with the model and falls back to a keyword list only when that call fails. A `Route classification failed` warning in the logs means the fallback is doing the routing; the log line names the underlying exception. |

---

## Data preparation

Two scripts turn the raw sources in `data/` into the two files Terraform
uploads. Both also **write the BigQuery table schemas** that `analytics.tf`
reads, so rerunning them can change Terraform's plan.

```bash
uv run python scripts/xml_to_ndjson.py
uv run python scripts/build_faa_sdr_wo_parts.py
```

| Script | Reads | Writes |
|--------|-------|--------|
| `scripts/xml_to_ndjson.py` | `data/xml/*.xml` | `data/processed/wo_workorders.ndjson.gz`<br>`deployment/terraform/shared/wo_workorders_schema.json` |
| `scripts/build_faa_sdr_wo_parts.py` | `data/faa-sdr/SDR-{2023,2024,2025}.csv` | `data/processed/faa_sdr_matching_wo_parts.csv`<br>`deployment/terraform/shared/faa_sdr_wo_parts_schema.json` |

Current output sizes: 8259 workorder rows (29 top-level fields, 211 including
nested), 299 matched FAA SDR rows (80 columns).

`xml_to_ndjson.py` prints a `SCHEMA DRIFT` section if the XML contains paths the
hand-written schema does not cover. Check that output before applying.

---

## Recreating the knowledge base and infrastructure with Terraform

Use this path to give a colleague their own IPC knowledge base in a new Google
Cloud project. Terraform enables Discovery Engine, creates a PDF staging
bucket and datastore, grants the importer and runtime permissions, and imports
the manuals with aircraft/ATA metadata. The defaults create a new datastore
and enable ingestion.

This is the **whole infrastructure module**: the same apply uploads the AMOS
and FAA datasets, creates BigQuery analytics and telemetry resources, and
provisions an Agent Runtime resource with placeholder code. It does not deploy
the current application. The project itself and its billing account must
already exist.

### 1. Prepare a separate checkout and credentials

Clone this repository into a new directory for the colleague's project. Keep
that checkout's `deployment/terraform/single-project/terraform.tfstate` with
that project: there is no remote backend configured. Do not copy the lab
project's state or change `project_id` in a checkout that manages it.
`TF_DATA_DIR` isolates provider files, **not Terraform state**. For shared
maintenance, arrange one shared state backend before multiple people apply.

Install Google Cloud SDK, Terraform >= 1.7, Bash, curl and jq. Python 3.11–3.13
and uv are needed for data regeneration and local agent runs. On macOS:


```bash
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
brew install jq
terraform version
```

The provisioning identity needs permission to enable services, manage project
IAM and service accounts, create buckets and their IAM grants, manage BigQuery
datasets/connections/jobs, manage Discovery Engine datastores/schemas/documents,
configure log sinks, and create Agent Runtime resources. Typical predefined
roles covering these operations are `roles/serviceusage.serviceUsageAdmin`,
`roles/serviceusage.serviceUsageConsumer`, `roles/resourcemanager.projectIamAdmin`,
`roles/iam.serviceAccountAdmin`, `roles/iam.serviceAccountUser`,
`roles/compute.viewer`, `roles/storage.admin`, `roles/bigquery.admin`, `roles/bigquery.connectionAdmin`,
`roles/discoveryengine.admin`, `roles/logging.configWriter` and
`roles/aiplatform.admin`; have your project administrator assign the equivalent
permissions under your organization's policy. Billing/project creation
permissions are separate and are not granted by this module.
Compute Engine is enabled so the module can resolve the default Compute
service account used by the existing build-permission grant.

Terraform uses ADC; the document importer uses the active gcloud CLI identity.
Authenticate both with the intended provisioning identity:

```bash
export PMA_PROJECT_ID=your-gcp-project-id
gcloud auth login
gcloud config set project "$PMA_PROJECT_ID"
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PMA_PROJECT_ID"
gcloud auth list --filter=status:ACTIVE --format='value(account)'
```

### 2. Set the project variables and inspect the input files

From the repository root:

```bash
cp deployment/terraform/single-project/vars/new-project.tfvars.example \
  deployment/terraform/single-project/vars/my_env.tfvars
git ls-files data/ipc_part_numbers
git ls-files data/processed/wo_workorders.ndjson.gz \
  data/processed/faa_sdr_matching_wo_parts.csv
```

Edit `vars/my_env.tfvars` to set `project_id` to the same project as
`PMA_PROJECT_ID`. This filename is gitignored. Leave `project_name = "pma-agent"`
because the current BigQuery specialist expects `pma_agent_analytics`.
`us-central1` is the tested infrastructure region for the lab; another
organization may allow different regions. Check its resource-location policy
before applying. Keep the knowledge-base location `global` for the current app.

The fresh-project settings are:

```hcl
knowledge_base_data_store_id     = "ipc-part-numbers"
knowledge_base_location          = "global"
create_knowledge_base_data_store = true
adopt_existing_data_store        = false
ingest_ipc_documents             = true
```

The repository tracks these three source PDFs, so a normal clone includes them:

| Source file under `data/ipc_part_numbers/` | Indexed aircraft type |
|---|---|
| `B737-8/25-31___124.pdf` | `737-800` |
| `M73-82/25-32___042.pdf` | `737-8200` |
| `M73-82/73-11___042.pdf` | `737-8200` |

The AMOS XML, FAA CSVs and two prepared BigQuery load files are also tracked.
Terraform uploads those local inputs to the selected project. To supply
different manuals, set `ipc_source_dir` to their absolute directory and retain
the `<AMOS type>/<ATA chapter>___<revision>.pdf` layout. The import requires a
nonempty PDF inventory. Document titles include the filename revision; a full
manual document number is not available from these filenames.

Use the prepared BigQuery files as supplied, or regenerate them after changing
their raw inputs. Regeneration also rewrites the shared table schemas:

```bash
uv sync
uv run python scripts/xml_to_ndjson.py
uv run python scripts/build_faa_sdr_wo_parts.py
```

### Curated dataset modes (synthetic vs real-data pipeline)

Terraform always creates the curated dataset as `pma_agent_curated` when
`project_name = "pma-agent"` and `create_curated_tables = true`.

You can populate curated artifacts in two mutually exclusive ways:

- Synthetic mode (Plan B): load prepared NDJSON files from `data/processed/`.
- Real-data mode: run the curated SQL pipeline against the analytics tables
  (replacement events, focus components, reference set, embeddings, precursor
  filtering, semantic scoring, LLM adjudication, lead-time samples).

Set only one mode per apply:

```hcl
# Synthetic mode (Plan B)
create_curated_tables          = true
load_curated_data              = true
run_curated_real_data_pipeline = false

# Real-data mode
create_curated_tables          = true
load_curated_data              = false
run_curated_real_data_pipeline = true
```

If real-data mode is enabled, you can tune:

```hcl
curated_sim_threshold      = 0.80
curated_k_precursors       = 50
curated_embedding_endpoint = "text-embedding-005"
curated_llm_endpoint       = "gemini-3.8.flash"
```

Sample commands from repository root:

```bash
# Synthetic mode (Plan B)
terraform -chdir=deployment/terraform/single-project plan \
  -var-file=vars/my_env.tfvars \
  -var='run_curated_real_data_pipeline=false' \
  -var='load_curated_data=true' \
  -out=.terraform/plan-synthetic.tfplan

# Real-data SQL pipeline mode
terraform -chdir=deployment/terraform/single-project plan \
  -var-file=vars/my_env.tfvars \
  -var='run_curated_real_data_pipeline=true' \
  -var='load_curated_data=false' \
  -out=.terraform/plan-real-data.tfplan
```

Real-data pipeline execution notes:

- The real-data curated steps run through `terraform_data` + `local-exec` with
  `bq query`, so the machine running `terraform apply` must have valid `gcloud`
  and `bq` authentication.
- Each step reruns when its own SQL changes (or when the shared input digest
  changes). `depends_on` preserves order but does not force downstream reruns.
- To force all curated real-data steps to run again, use `-replace` on all
  eight `terraform_data` resources:

```bash
terraform -chdir=deployment/terraform/single-project apply \
  -var-file=vars/my_env.tfvars \
  -replace='terraform_data.curated_replacement_events[0]' \
  -replace='terraform_data.curated_focus_components[0]' \
  -replace='terraform_data.curated_reference_set[0]' \
  -replace='terraform_data.curated_workorder_embeddings[0]' \
  -replace='terraform_data.curated_candidate_precursors[0]' \
  -replace='terraform_data.curated_semantic_scoring[0]' \
  -replace='terraform_data.curated_llm_adjudication[0]' \
  -replace='terraform_data.curated_lead_time_samples[0]'
```

### 3. Plan and apply

```bash
cd deployment/terraform/single-project
terraform init
terraform validate
terraform plan -var-file=vars/my_env.tfvars -out=.terraform/new-project.tfplan
terraform show .terraform/new-project.tfplan
terraform apply .terraform/new-project.tfplan
```

Review the saved plan before applying: it should create the datastore in your
project and enable document import, with no attempt to import the lab datastore
or destroy another project's resources. Applying the saved plan uses its
captured variable values; `terraform apply <planfile>` does not take `-var-file`.

The import script patches filterable metadata fields, imports PDFs with stable
document IDs using `INCREMENTAL` reconciliation, and waits for the operation.
It checks reported import failures and the expected document count. Search
indexing can still take additional time after import completes. Incremental
imports update matching IDs and add new ones; removing a PDF locally does not
remove its previously indexed document.

### 4. Verify the knowledge base and connect the local app

```bash
terraform output knowledge_base_data_store_name
terraform output knowledge_base_document_count
terraform output knowledge_base_import_metadata_uri
gcloud storage cat "$(terraform output -raw knowledge_base_import_metadata_uri)" \
  | jq -s 'map({id, aircraft_type: .structData.aircraft_type, uri: .content.uri})'

export PMA_DATASTORE_NAME="$(terraform output -raw knowledge_base_data_store_name)"
export PMA_IPC_DATASTORE_ID="$(terraform output -raw knowledge_base_data_store_id)"
curl --silent --show-error --fail-with-body \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: ${PMA_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1/${PMA_DATASTORE_NAME}/branches/default_branch/documents?pageSize=100" \
  | jq '{documents: [.documents[]? | {id, aircraft_type: .structData.aircraft_type}], nextPageToken}'
```

With the supplied inventory, expect three source records and three documents
with the two aircraft types shown above. `knowledge_base_document_count` is the
local manifest count, not a live API assertion. Follow `nextPageToken` for a
larger inventory. An imported document alone does not prove that a search query
will retrieve the right passage.

```bash
cd ../../..
cp .env.example .env
```

In `.env`, set `GOOGLE_CLOUD_PROJECT` to `PMA_PROJECT_ID`,
`GOOGLE_CLOUD_LOCATION=global`, and `IPC_DATASTORE_ID` to the
`PMA_IPC_DATASTORE_ID` output, which is `ipc-part-numbers` for the example.
Keep `GOOGLE_GENAI_USE_VERTEXAI=true`. Install dependencies with `agents-cli install`
and use [the IPC end-to-end check](#6-verify-end-to-end) after indexing is ready.
The local ADC identity needs `roles/aiplatform.user` and
`roles/discoveryengine.viewer` for this check. Terraform grants the Discovery
Engine viewer role to the application service account and Vertex AI service
agent; it does not grant roles to your personal ADC identity.

The accompanying BigQuery tables can be checked independently:

```bash
bq show --format=prettyjson "${PMA_PROJECT_ID}:pma_agent_analytics.wo_workorders" | jq .numRows
bq show --format=prettyjson "${PMA_PROJECT_ID}:pma_agent_analytics.faa_sdr_wo_parts" | jq .numRows
```

Expect 8,259 and 299 rows for the supplied files. Application deployment remains
the separate [Deployment](#deployment) step; Terraform's placeholder Agent
Runtime resource is not evidence that the current agent has been deployed.

### Adopting an existing datastore

For the existing lab, retain `vars/env.tfvars` and its original state. It sets
the exact console-created datastore ID, `adopt_existing_data_store = true` and
`ingest_ipc_documents = false`. For a different existing datastore, create your
own variable file with its exact ID and those same adoption flags. Use that
file in the plan command and review the import and any changes before applying.
If other resources already exist but are absent from your state, import them
at their Terraform addresses as well; a fresh state does not adopt them
automatically.

Keep ingestion disabled while adopting console-imported documents: their IDs
may differ from this module's stable IDs and an incremental import could create
duplicates. Adoption and changing document ownership are separate operations.
The datastore has `prevent_destroy`; investigate any replacement plan instead
of removing that guard to make the plan pass.

### Validate changes without provisioning cloud resources

From the repository root, with dependencies installed:

```bash
terraform -chdir=deployment/terraform/single-project validate
terraform -chdir=deployment/terraform/single-project test -filter=tests/knowledge_base.tftest.hcl
uv run pytest tests/unit/test_ipc_ingestion.py -q
```

The Terraform tests use mocked providers and plan-only runs for fresh creation,
existing-datastore adoption, unmanaged datastores and empty inventories. The
importer tests run the real Bash script with mocked gcloud/curl responses to
check schema/import ordering, retries, refreshed PDF content and partial
failures. They make no cloud calls; they do not replace verification after a
real apply in the target project.

### Re-running after a data change

`google_bigquery_job` resources are **immutable**: a BigQuery job cannot be
edited once it exists. `analytics.tf` works around this by keying the job id on
the file's md5:

```hcl
job_id = "load-wo-workorders-${substr(filemd5(local.wo_workorders_local_path), 0, 12)}"
```

Consequences for a re-run:

- **Identical file bytes** produce the same job id. Regenerating gzip files
  can change their metadata and hash even when the workorder rows are unchanged.
- **Changed data** produces a new md5, a new job id and therefore a genuinely
  new load job. Both loads use `write_disposition = "WRITE_TRUNCATE"`, so the
  table is replaced, not appended to.
- Terraform replaces the old job resource with the new one in state. BigQuery
  jobs cannot be deleted through the API, so the previous load stays in the
  project's job history.
- Tables have `deletion_protection = false`, so a schema change to
  `deployment/terraform/shared/*_schema.json` will recreate the table.

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Error 409: already exists` | Do not retry creation. `terraform import <address> <id>` the existing resource into state (per `CLAUDE.md`). |
| Apply fails on the BigQuery connection service account | Propagation race. Re-plan and re-apply. |
| Resource rejected by `constraints/gcp.resourceLocations` | Use a region allowed by the target project's organization policy. The lab uses `us-central1`; the policy can differ in a colleague's project. |
| `apply -auto-approve` blocked | Use `plan -out=tfplan` then `apply tfplan`. |
| Model 404 at runtime | Wrong `GOOGLE_CLOUD_LOCATION` (use `global`), not a wrong model name. |

### Resource names

The module uses these names with `project_name = "pma-agent"`. Verify their
presence in your own state and project; this table is not a live deployment
check.

| Resource | Name |
|----------|------|
| BigQuery dataset (analytics) | `pma_agent_analytics` |
| BigQuery dataset (curated) | `pma_agent_curated` |
| BigQuery tables | `wo_workorders`, `faa_sdr_wo_parts` |
| Curated tables (synthetic mode) | `wo_embeddings`, `fct_lead_time_samples`, `dim_focus_components`, `dim_reference_set` |
| Curated tables (real-data mode) | `fct_replacement_events`, `dim_focus_components`, `dim_reference_set`, `wo_embeddings`, `replacement_anchor_embeddings`, `candidate_precursors`, `scored_precursors`, `adjudicated_precursors`, `fct_lead_time_samples` |
| BigQuery dataset (telemetry) | `pma_agent_telemetry` |
| Telemetry tables / view | `completions`, `aiplatform_googleapis_com_reasoning_engine_stdout`, `completions_view` |
| GCS buckets | `<project_id>-pma-agent-data`, `<project_id>-pma-agent-logs`, `<project_id>-pma-agent-kb` |
| IPC datastore | Your `knowledge_base_data_store_id` input |
| Service account | `pma-agent-app@<project_id>.iam.gserviceaccount.com` |
| BigQuery connection | `pma-agent-genai-telemetry` |
| Log sink | `pma-agent-genai-logs` |
| Reasoning Engine | display name `pma-agent`, region `us-central1` |

Terraform creates the Reasoning Engine with a placeholder source archive
(`deployment/terraform/shared/dummy_source.b64`) and `lifecycle.ignore_changes`
on `spec[0].source_code_spec`, `spec[0].container_spec` and
`spec[0].deployment_spec`, so `agents-cli deploy` can overwrite the code without
Terraform reverting it.

---

## Creating it manually, without Terraform

Both paths below produce the same result as the Terraform module. Use them for
a quick throwaway project, or to understand what the module does. Terraform
remains the source of truth; if you build by hand and later want the module to
manage it, `terraform import` the resources rather than applying over them.

Set these once:

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION=us-central1
export PREFIX=pma-agent          # matches Terraform's var.project_name
```

### BigQuery by hand

Enable the APIs and create the dataset. The dataset must be in the same region
as the buckets; `us-east1` is rejected by the org policy.

```bash
gcloud services enable bigquery.googleapis.com storage.googleapis.com

bq --location=${REGION} mk --dataset \
  --description "AMOS workorders and matched FAA Service Difficulty Reports" \
  ${PROJECT_ID}:${PREFIX//-/_}_analytics
```

Generate the data and the schemas. Both scripts write a schema JSON next to the
data, and those schema files are what you load with:

```bash
uv run python scripts/xml_to_ndjson.py
uv run python scripts/build_faa_sdr_wo_parts.py
```

Stage the files in GCS. Loading straight from a local path also works, but the
bucket is what Terraform does and it keeps the two paths comparable:

```bash
gcloud storage buckets create gs://${PROJECT_ID}-${PREFIX}-data \
  --location=${REGION} --uniform-bucket-level-access

gcloud storage cp data/processed/wo_workorders.ndjson.gz \
  gs://${PROJECT_ID}-${PREFIX}-data/workorders/wo_workorders.ndjson.gz
gcloud storage cp data/processed/faa_sdr_matching_wo_parts.csv \
  gs://${PROJECT_ID}-${PREFIX}-data/faa-sdr/faa_sdr_matching_wo_parts.csv
```

Load both tables. `bq load` creates the table from the schema file, so there is
no separate create step. These flags mirror the `google_bigquery_job` resources
exactly - explicit schema, no autodetect, truncate on reload:

```bash
DATASET=${PROJECT_ID}:${PREFIX//-/_}_analytics

bq --location=${REGION} load \
  --source_format=NEWLINE_DELIMITED_JSON \
  --replace \
  ${DATASET}.wo_workorders \
  gs://${PROJECT_ID}-${PREFIX}-data/workorders/wo_workorders.ndjson.gz \
  deployment/terraform/shared/wo_workorders_schema.json

bq --location=${REGION} load \
  --source_format=CSV \
  --skip_leading_rows=1 \
  --allow_quoted_newlines \
  --replace \
  ${DATASET}.faa_sdr_wo_parts \
  gs://${PROJECT_ID}-${PREFIX}-data/faa-sdr/faa_sdr_matching_wo_parts.csv \
  deployment/terraform/shared/faa_sdr_wo_parts_schema.json
```

`--replace` is `WRITE_TRUNCATE`: rerunning replaces the table contents rather
than appending. Do not drop it, or a second run doubles every row.

Verify:

```bash
bq show --format=prettyjson ${DATASET}.wo_workorders    | grep numRows   # 8259
bq show --format=prettyjson ${DATASET}.faa_sdr_wo_parts | grep numRows   # 299
```

`wo_workorders.ndjson.gz` is gzipped. `bq load` decompresses it, but a gzipped
JSON load is single-threaded and slower than an uncompressed one - expect it to
take a while rather than assuming it has hung.

### BigQuery by clicking through the console

Console labels move around; these were checked against the Google Cloud docs,
but if one has been renamed, match the intent rather than the exact wording.

**Create the dataset**

1. Console → **BigQuery**.
2. In the left pane click **Explorer**, expand your project.
3. On the project row click the three-dot menu → **Create dataset**.
4. **Dataset ID**: `pma_agent_analytics` (underscores; BigQuery does not accept hyphens).
5. **Location type**: **Region**, and pick **us-central1**. This must match the
   bucket's region, and it cannot be changed later.
6. **Create dataset**.

**Upload the data**

1. Run the two scripts under Data preparation first - the console cannot
   generate `wo_workorders.ndjson.gz` or the schema files for you.
2. Console → **Cloud Storage** → **Buckets** → **Create**.
   Name it `<project-id>-pma-agent-data`, **Region** `us-central1`, leave
   uniform bucket-level access on.
3. Upload `data/processed/wo_workorders.ndjson.gz` into a `workorders/` folder
   and `data/processed/faa_sdr_matching_wo_parts.csv` into a `faa-sdr/` folder.
   The folder names are only a convention, but they keep the console and the
   Terraform layout comparable.

**Create the workorders table**

1. In **Explorer**, click your dataset, then **Create table** in the
   **Dataset info** panel.
2. **Create table from**: **Google Cloud Storage**. Browse to
   `workorders/wo_workorders.ndjson.gz`. Only one URI is accepted here, though
   wildcards work.
3. **File format**: **JSONL (Newline delimited JSON)**.
4. **Destination** → **Table**: `wo_workorders`. **Table type** stays
   **Native table**.
5. **Schema**: leave **Auto detect** unticked. Click **Edit as text** and paste
   the entire contents of
   `deployment/terraform/shared/wo_workorders_schema.json`. Autodetect will
   guess wrongly on a 211-field nested schema, so this step is not optional.
6. **Advanced options** → **Write preference**. The default is
   **Write if empty**; switch it to the overwrite option if you are reloading a
   table that already has rows.
7. **Create table**.

**Create the FAA SDR table**

Same flow, with:

- file `faa-sdr/faa_sdr_matching_wo_parts.csv`, **File format**: **CSV**
- **Table**: `faa_sdr_wo_parts`
- **Edit as text** → paste `deployment/terraform/shared/faa_sdr_wo_parts_schema.json`
- **Advanced options** → **Header rows to skip**: `1` (default is `0`)
- **Advanced options** → **Quoted newlines**: tick **Allow quoted newlines**
  (default is off). The FAA narrative fields contain newlines inside quotes, so
  without this the load fails with a row-count mismatch.

Check the row counts on each table's **Details** tab: 8259 and 299.

### Knowledge base by hand

The datastore holds the three IPC PDFs and must keep the two aircraft types
separable, so the interesting part is the metadata, not the upload.

**Console route.** Vertex AI Search (Agent Builder) → Data Stores → Create.
Choose Cloud Storage as the source, **Unstructured documents**, and location
`global`. The console assigns an id with a numeric suffix, which is why the
existing datastore is called `ipc-part-numbers_1789998929768`. Note the id down;
it goes in `.env` and in `vars/env.tfvars`.

The console route with "folder of PDFs" gives you no aircraft-type metadata, so
retrieval cannot filter NG from MAX. Prefer the API route below, or import the
metadata JSONL afterwards.

**API route**, which is what the Terraform path does. First stage the PDFs,
mirroring the on-disk aircraft-type folders:

```bash
gcloud services enable discoveryengine.googleapis.com

gcloud storage buckets create gs://${PROJECT_ID}-${PREFIX}-kb \
  --location=${REGION} --uniform-bucket-level-access

gcloud storage cp -r data/ipc_part_numbers/* \
  gs://${PROJECT_ID}-${PREFIX}-kb/ipc-manuals/
```

Create the datastore. `dataStoreId` is yours to choose - nothing generates it:

```bash
export DATA_STORE_ID=ipc-part-numbers
export TOKEN=$(gcloud auth print-access-token)

curl -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1/projects/${PROJECT_ID}/locations/global/collections/default_collection/dataStores?dataStoreId=${DATA_STORE_ID}" \
  -d '{
        "displayName": "IPC part numbers",
        "industryVertical": "GENERIC",
        "solutionTypes": ["SOLUTION_TYPE_SEARCH"],
        "contentConfig": "CONTENT_REQUIRED"
      }'
```

Those four fields match the `google_discovery_engine_data_store` resource.
`industryVertical`, `contentConfig` and the location are all ForceNew in
Terraform, so if you later adopt this datastore they must agree or the plan
becomes a destroy and create.

Write a metadata JSONL, one line per PDF. This is what carries the aircraft
type into the index, so retrieval can filter on it:

```jsonl
{"id":"ipc-737-800-25-31-124","structData":{"aircraft_type":"737-800","amos_aircraft_type":"B737-8","ata_chapter":"25-31","manual_type":"AIPC","revision":"124","title":"Chapter 25-31, 737-800, AIPC, 124"},"content":{"mimeType":"application/pdf","uri":"gs://PROJECT-pma-agent-kb/ipc-manuals/B737-8/25-31___124.pdf"}}
```

The aircraft strings must match `VARIANT_BY_TYPE` in `scripts/wo_xml.py`
(`B737-8` -> `737-800`, `M73-82` -> `737-8200`) so the knowledge base and the
BigQuery tables describe aircraft the same way.

Upload it and import:

```bash
gcloud storage cp ipc_documents.jsonl \
  gs://${PROJECT_ID}-${PREFIX}-kb/ipc-manuals/metadata/ipc_documents.jsonl

curl -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1/projects/${PROJECT_ID}/locations/global/collections/default_collection/dataStores/${DATA_STORE_ID}/branches/default_branch/documents:import" \
  -d "{
        \"gcsSource\": {
          \"inputUris\": [\"gs://${PROJECT_ID}-${PREFIX}-kb/ipc-manuals/metadata/ipc_documents.jsonl\"],
          \"dataSchema\": \"document\"
        },
        \"reconciliationMode\": \"INCREMENTAL\"
      }"
```

> **`reconciliationMode`.** `INCREMENTAL` adds and updates documents, which is
> the safe choice by hand. `FULL` makes the datastore exactly match the JSONL
> and **deletes anything not listed** - including documents a console import
> added under different ids. The checked-in
> `deployment/terraform/single-project/ingest_ipc_documents.sh` defaults to
> `INCREMENTAL` too. Its optional `RECONCILIATION_MODE=FULL` override is only
> for an intentional replacement of a datastore's complete document inventory.

Import is a long-running operation. Poll it:

```bash
curl -s -H "Authorization: Bearer ${TOKEN}" \
  "https://discoveryengine.googleapis.com/v1/<operation-name-from-the-response>"
```

Finally put the id in `.env` as `IPC_DATASTORE_ID`, and grant the app service
account `roles/discoveryengine.viewer` if the agent will run as a service
account rather than your own login.

### Knowledge base by clicking through the console

The product is called **AI Applications** in the console navigation (formerly
Agent Builder; Vertex AI Search is being rebranded again, to Agent Search, so
expect the naming to keep moving).

1. Console → **AI Applications** → **Data Stores**.
2. **Create data store**.
3. **Source**: **Cloud Storage**.
4. Under "Select a folder or file you want to import", choose **Folder** and
   browse to your uploaded `ipc-manuals/` prefix, or paste the `gs://` path.
5. Choose what kind of data you are importing. This is the step that decides
   whether aircraft-type metadata survives:
   - plain unstructured documents - the PDFs are indexed with ids hashed from
     their Cloud Storage URI, and **no `aircraft_type` metadata**
   - unstructured documents **with metadata**, which reads a JSONL where each
     line carries `id`, `structData` and a `content.uri` - this is the
     `dataSchema: "document"` form shown in the API route above
6. **Continue**, then pick the region. Use **global**, which is where the
   existing datastore lives and what `pm_agent` expects.
7. Name the data store. **The console generates the id from the name and
   appends a numeric suffix**, which is where `ipc-part-numbers_1789998929768`
   came from. You cannot choose it here - that is the one thing the API and
   Terraform routes give you that the console does not.
8. Optionally expand **Document processing options** for parsing and chunking.
   The OCR and layout parsers cost extra.
9. **Create**. Watch the data store's **Data** page; the **Activity** tab moves
   from **In progress** to **Import completed**. Minutes to hours depending on
   volume.
10. Copy the generated id into `.env` as `IPC_DATASTORE_ID`, and into
    `vars/env.tfvars` as `knowledge_base_data_store_id` if Terraform will adopt
    it.

Two things the docs call out that are worth knowing before you start:

> **Console creation of a Cloud Storage data store can fail.** This is a known
> issue. The documented workarounds are to use the API instead, or to create a
> fresh Cloud Storage bucket first and import from that.

> **Cloud Storage permissions do not carry over.** After import, anyone with
> sufficient AI Applications permissions can read the documents regardless of
> their access to the source bucket.

If you want metadata-driven filtering and a predictable id, the API route is
less clicking and fewer surprises.

---

## Known gaps

Discovery Engine API enablement and `roles/discoveryengine.viewer` grants are
managed in Terraform. The remaining implementation/deployment gaps are:

- **Runtime BigQuery permissions are not yet verified.** Terraform now declares
  project-scoped `roles/bigquery.jobUser` and analytics-dataset-scoped
  `roles/bigquery.dataViewer` for the app service account. This implementation
  has not applied them or tested the deployed principal.
- **`GOOGLE_CLOUD_PROJECT` is not set on the Reasoning Engine.** `service.tf`
  sets `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI` and the telemetry
  variables, but not the project; the `service.tf` comment says Agent Runtime
  reserves it and rejects it in `deployment_spec.env`. `pm_agent/config.py`
  works around this by falling back to Application Default Credentials.
- **Ordinary BigQuery chat is unchanged.** The dedicated work-order route uses
  the new bounded history provider and canonical artifact tables. Production
  corpus loading, reviewed retrieval relevance, model training, and replacement
  policy validation remain separate outstanding work.

## XML work-order analysis

Start the existing application after installing dependencies:

```sh
uv sync
uv run uvicorn pm_agent.fast_api_app:app --host 127.0.0.1 --port 8080
```

### Upload in ADK chat

Open <http://127.0.0.1:8080/dev-ui/?app=pm_agent>, select `pm_agent`, use the
**+ / Upload local file** control and attach
[`demo_nozzle_upload.xml`](tests/fixtures/workorders/demo_nozzle_upload.xml).
Send **“Analyse the attached work order.”**

This synthetic completed work order is `DEMO-XML-ONLY-2085`, with part
`2085M31G03`, marker `COPPER-FINCH-41`, serial off `DEMO-OFF-41` and serial on
`DEMO-ON-42`. The answer must show those uploaded facts. No BigQuery row or
database import is needed. The ADK event details also expose the source filename,
saved artifact version and SHA-256 upload hash.

The real completed example is
[`TRANSFER_WORKORDER_1789487041751.xml`](data/xml/TRANSFER_WORKORDER_1789487041751.xml)
(WO `105177647`). Structured component positions are reported as recorded;
serial pairs are not assigned to narrative nozzle numbers without supporting data.

Closed uploads default to `historical_replay`; open uploads default to
`new_work_order`. The cutoff defaults to the current UTC time. Supply explicit
options in the message or in a follow-up:

```text
Analyse the attached work order. analysis_as_of=2026-09-01T09:15:00Z
```

For the synthetic closed export, that earlier cutoff excludes the later snapshot
findings and completed actions. Multiple files prompt for `filename="FILE.xml"`;
multiple work orders prompt for `selected_wo_id=NUMBER`. Follow-ups reuse the
exact saved version, including version 0. Other supported options are `mode`,
`target_part_number`, `current_aircraft_tac`, `current_tac_source` and
`current_tac_observed_at`. Current TAC requires its own source and observation
time; closing TAC is not substituted for it.

Chat accepts up to 10 XML files totalling 25 MiB. Artifacts stay within the
current ADK app/user/session. Storage uses the existing artifact service: GCS
when `LOGS_BUCKET_NAME` is set, otherwise memory until the server restarts.
Attachment analysis currently reports uploaded evidence only. It explicitly
states that BigQuery and the knowledge base were not queried and that failure
probability, remaining life and replacement deadlines are unavailable.

### Dedicated HTTP API

This synthetic fixture exercises the dedicated API route. Metadata belongs in query
parameters for both raw XML and multipart uploads:

```sh
curl --fail-with-body \
  'http://127.0.0.1:8080/workorders/analyze?mode=new_work_order&analysis_as_of=2026-09-01T10:00:00Z&current_aircraft_tac=120&current_tac_source=operator_feed&current_tac_observed_at=2026-09-01T09:55:00Z' \
  -H 'Content-Type: application/xml' \
  --data-binary @tests/fixtures/workorders/open_nozzle.xml
```

For multipart, replace the last two arguments with
`-F 'file=@tests/fixtures/workorders/open_nozzle.xml;type=application/xml'`.
The request body limit is 25 MiB including multipart framing. An envelope with
multiple WOs requires `selected_wo_id`; a closed export requires
`mode=historical_replay`. Replay excludes text unavailable at the analysis time.
If needed, supply `target_part_number` to resolve an alias-only candidate.

The response includes parsed context, input-counter provenance, targets,
historical cases, retrieval status and local IPC catalogue references. With no
history configuration, parsing still works and retrieval reports `not_configured`.
Prediction, timing and replacement recommendation fields remain explicitly null
with their reasons. Current aircraft TAC is not component age.

Enable history only after the matching physical tables and corpus are loaded:

| Environment variable | Value |
|---|---|
| `PM_HISTORY_PROJECT` | Existing Google Cloud project; falls back to application config/ADC |
| `PM_HISTORY_DATASET` | `pma_agent_analytics` |
| `PM_HISTORY_CORPUS` | Exact `reference_corpus_version` from preparation report |
| `PM_HISTORY_METHOD` | `keyword` (default), `vector`, or `hybrid` |
| `PM_EMBEDDING_MODEL` / `PM_EMBEDDING_VERSION` | `gemini-embedding-001` / `001` |
| `PM_EMBEDDING_DIMENSION` / `PM_EMBEDDING_LOCATION` | `3072` / `global` |

Semantic methods require embeddings in the identical model/preparation/corpus
configuration. Query values are parameterized; history queries stay within the
analytics dataset and use byte, time and result limits. The upload is never
inserted into the historical index.

Reproduce local preparation and the failed training gate:

```sh
uv run python scripts/audit_prediction_cohort.py
uv run python scripts/prepare_retrieval_documents.py \
  --output /tmp/retrieval_documents.jsonl --report /tmp/retrieval_coverage.json
uv run python scripts/embed_retrieval_documents.py --documents /tmp/retrieval_documents.jsonl
uv run python scripts/run_conditional_failure_model.py
```

The last command exits `3` while the fixed-corpus gate fails. Preparation defaults
to ten eligible local documents and embedding defaults to dry-run. See
[embedding and table-loading commands](docs/bigquery-embeddings.md) and the
[reviewed retrieval evaluator](docs/bigquery-retrieval-evaluation.md).

Run deterministic checks with
`uv run pytest tests/unit tests/integration/test_workorder_upload.py tests/integration/test_adk_workorder_upload.py`.
The ADK tests exercise real Workflow, Runner, session and artifact services;
model calls are disabled for uploads. Reproduce the two attachment evaluations
against the running local server with:

```sh
agents-cli eval generate --dataset tests/eval/datasets/adk-workorder-upload.json \
  --url http://127.0.0.1:8080 --app-name pm_agent --output /tmp/adk-upload-traces
agents-cli eval grade --traces /tmp/adk-upload-traces \
  --config tests/eval/upload_eval_config.yaml --output /tmp/adk-upload-scores
```

These use deterministic checks of the parsed response and replay exclusions.
With Google credentials, `uv run python scripts/verify_history_bigquery.py`
checks keyword/vector/hybrid SQL using synthetic session temporary tables and a
100 MB per-script billing cap. It creates no permanent table or deployment.

---

## Deployment

Agent code is deployed separately from infrastructure:

```bash
PMA_PROJECT_ID=your-project-id
agents-cli deploy --project "$PMA_PROJECT_ID" --region us-central1 \
  --service-name pma-agent --update-only \
  --service-account "pma-agent-app@${PMA_PROJECT_ID}.iam.gserviceaccount.com"
```

The explicit service name updates the Terraform-created `pma-agent` runtime;
`--update-only` prevents accidentally creating a separate `pm-agent` runtime.
**Requires explicit human approval** per `CLAUDE.md`. The `Dockerfile` serves
`pm_agent.fast_api_app:app` on port 8080 and includes the shared `amos_data`
parser. Raw source data and local evaluation artifacts are excluded from the
deployment archive.

The upload slice was deployed on 2026-09-22 from revision `853c947` to runtime
`projects/98892663275/locations/us-central1/reasoningEngines/9071133107117096960`.
Live checks verified uploaded XML facts, artifact version 0 and a replay
follow-up using the saved artifact. `deployment_metadata.json` records the
runtime and deployment timestamp. This does not establish BigQuery history
permissions or complete the planned parallel evidence workflow.

## Observability

Telemetry exports to Cloud Trace and to BigQuery. `telemetry.tf` routes GenAI
inference logs through a log sink into `pma_agent_telemetry`, uploads
prompt/response content to `gs://<project_id>-pma-agent-logs/completions`, and
joins the two in the `completions_view` view. Content capture is off by default
(`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT`).

Traces report the agent as `pm_agent` (`gen_ai.agent.name`) but
`OTEL_SERVICE_NAME` is `pma-agent`, and `var.telemetry_logs_filter` filters on
`labels.service_name="pma-agent"`.

## A2A

This agent supports the [A2A Protocol](https://a2a-protocol.org/)
(`is_a2a: true` in the manifest). Use the
[A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test
interoperability.
