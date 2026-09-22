# pm-agent

A predictive-maintenance agent for Boeing 737-800 / 737-8200 fleet questions.
A router classifies each question and hands the turn to one specialist: an IPC
manual retriever backed by Vertex AI Search, or a BigQuery analytics agent over
AMOS work orders and FAA Service Difficulty Reports.

Scaffolded with `agents-cli` version `1.6.1` (`agents-cli-manifest.yaml`), built
on the ADK 2.0 workflow graph API.

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
│   │   └── router.py                  # Entry node: LLM classifier sets ctx.route
│   ├── sub_agents/
│   │   ├── ipc_manual_retrieval/      # Vertex AI Search over the IPC datastore
│   │   └── bq_analytics/              # BigQuery specialist (placeholder)
│   └── app_utils/                     # a2a, services, reasoning_engine_adapter
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

`pm_agent/agent.py` builds a single-hop graph:

```
START -> router -+-> "ipc" -> ipc_manual_retrieval
                 +-> "bq"  -> bq_analytics
```

- **`router`** (`pm_agent/nodes/router.py`) is the only entry point. It runs a
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
- **Terraform** - only for the infrastructure work below (see the install note)

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

**`GOOGLE_CLOUD_LOCATION=global`** matters. The IPC datastore lives in `global`,
and a wrong location surfaces as a confusing model 404 rather than a location
error.

**`IPC_DATASTORE_ID`** is the Vertex AI Search datastore behind the IPC
specialist. It is not hardcoded in the agent because it is environment
specific. Terraform owns it as `var.knowledge_base_data_store_id`, and
`service.tf` passes it to the deployed agent, so `.env` is only for local runs.

The id is always an **input**, never generated. `data_store_id` is a required
field on `google_discovery_engine_data_store`, so Terraform creates the
datastore under exactly the id you give it. That means there is no
chicken-and-egg: you can write the value into `.env` before the first apply.

- **Creating a new datastore** (`adopt_existing_data_store = false`): choose any
  valid id, for example `ipc-part-numbers`, put it in both `vars/env.tfvars`
  and `.env`, and apply. They will match because you picked both.
- **Adopting the existing one** (`adopt_existing_data_store = true`, the default
  here): the id is whatever the console assigned, including its numeric suffix,
  such as `ipc-part-numbers_1789998929768`. Look it up once and use it in both
  places. Getting this wrong does not error - Terraform quietly creates a
  second, empty datastore beside the working one.

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

## Recreating the BigQuery deployment with Terraform

`deployment/terraform/single-project/analytics.tf` owns the BigQuery side:
one dataset, one GCS data bucket, two staged objects, two native tables and two
load jobs. The same root module also creates the service account, the
telemetry dataset and the Reasoning Engine, so an apply touches more than
BigQuery.

### 0. Install Terraform

`brew install terraform` does not work because of the BUSL relicense. Use the
HashiCorp tap:

```bash
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
terraform version   # 1.16.3 here
```

### 1. Configure

Edit `deployment/terraform/single-project/vars/env.tfvars`:

```hcl
project_name = "pma-agent"                        # DO NOT CHANGE - see the warning above
project_id   = "your-gcp-project-id"
region       = "us-central1"
```

> **Region must be `us-central1`.** An earlier attempt at `us-east1` was
> rejected by the org policy `constraints/gcp.resourceLocations`.
> `terraform plan` cannot see that policy, so the failure only appears at apply
> time, after resources have started being created.

State is local: there is no `backend` block, so `terraform.tfstate` lives in
`deployment/terraform/single-project/` and is gitignored. Whoever applies needs
that file.

### 2. Regenerate the data files

Terraform uploads whatever is on disk, so do this before planning:

```bash
uv run python scripts/xml_to_ndjson.py
uv run python scripts/build_faa_sdr_wo_parts.py
```

### 3. Plan and apply

`terraform apply -auto-approve` is blocked by tooling in this environment. Use
the two-step form:

```bash
cd deployment/terraform/single-project
terraform init
terraform plan -out=tfplan -var-file=vars/env.tfvars
terraform apply tfplan
```

`terraform apply <planfile>` does not take `-var-file`; the variables are baked
into the saved plan.

> **The first apply can fail** with a BigQuery connection service account that
> "does not exist". This is a propagation race (`telemetry.tf` has a 10s
> `time_sleep` that is sometimes not enough). Re-plan and re-apply; that is what
> worked.

### 4. Verify

```bash
terraform output   # analytics_dataset_id, analytics_data_bucket_name, ...

bq show --format=prettyjson <project_id>:pma_agent_analytics.wo_workorders    | grep numRows
bq show --format=prettyjson <project_id>:pma_agent_analytics.faa_sdr_wo_parts | grep numRows
```

Expect 8259 and 299 rows respectively, matching the local files. Both tables
are in location `us-central1`.

```bash
gsutil ls gs://<project_id>-pma-agent-data/**
# workorders/wo_workorders.ndjson.gz
# faa-sdr/faa_sdr_matching_wo_parts.csv
```

### Re-running after a data change

`google_bigquery_job` resources are **immutable**: a BigQuery job cannot be
edited once it exists. `analytics.tf` works around this by keying the job id on
the file's md5:

```hcl
job_id = "load-wo-workorders-${substr(filemd5(local.wo_workorders_local_path), 0, 12)}"
```

Consequences for a re-run:

- **Identical data** produces the same job id, so Terraform reports no changes
  and re-running the scripts and applying is a no-op.
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
| Resource rejected by `constraints/gcp.resourceLocations` | Region is not `us-central1`. `plan` cannot catch this. |
| `apply -auto-approve` blocked | Use `plan -out=tfplan` then `apply tfplan`. |
| Model 404 at runtime | Wrong `GOOGLE_CLOUD_LOCATION` (use `global`), not a wrong model name. |

### What is deployed

From `terraform.tfstate` in `deployment/terraform/single-project/`, against
project `qwiklabs-asl-04-1726946cb8ab`:

| Resource | Name |
|----------|------|
| BigQuery dataset (analytics) | `pma_agent_analytics` |
| BigQuery tables | `wo_workorders`, `faa_sdr_wo_parts` |
| BigQuery dataset (telemetry) | `pma_agent_telemetry` |
| Telemetry tables / view | `completions`, `aiplatform_googleapis_com_reasoning_engine_stdout`, `completions_view` |
| GCS buckets | `<project_id>-pma-agent-data`, `<project_id>-pma-agent-logs` |
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
> `deployment/terraform/single-project/ingest_ipc_documents.sh` uses `FULL`
> deliberately, because there the JSONL is generated from the repo and is meant
> to be the source of truth. Know which one you want.

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

These are real and currently unfixed.

- **`discoveryengine.googleapis.com` is not managed in Terraform.** It is
  enabled in the project, but it is absent from `local.services` in `apis.tf`.
  A fresh project built from this module would not have it, and the IPC
  specialist would fail.
- **The app service account cannot read the Vertex AI Search datastore.**
  `pma-agent-app@` holds exactly the five roles in `var.app_sa_roles`
  (`aiplatform.user`, `logging.logWriter`, `cloudtrace.agent`, `storage.admin`,
  `serviceusage.serviceUsageConsumer`). It has no
  `roles/discoveryengine.viewer`.
- **The app service account has no BigQuery role.** Same five roles, so no
  `roles/bigquery.jobUser` or `roles/bigquery.dataViewer`. The `bq_analytics`
  specialist uses Application Default Credentials, which is the developer's
  gcloud login locally but this service account on Agent Runtime, where it
  would not be able to run a query.
- **`GOOGLE_CLOUD_PROJECT` is not set on the Reasoning Engine.** `service.tf`
  sets `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI` and the telemetry
  variables, but not the project; the `service.tf` comment says Agent Runtime
  reserves it and rejects it in `deployment_spec.env`. `pm_agent/config.py`
  works around this by falling back to Application Default Credentials.
- **The deployed Reasoning Engine predates the restructure.** Its Terraform
  state `update_time` is earlier than the `pm_agent` restructure commit, and
  `deployment_metadata.json` still records `"remote_agent_runtime_id": "None"`.
  The running agent is not the code in this repo until someone redeploys.
- **`bq_analytics` is a placeholder.** See the docstring in
  `pm_agent/sub_agents/bq_analytics/agent.py` for what is deliberately not
  built: curated SQL tools or views over the nested schema, and any evaluation
  of answer quality.

---

## Deployment

Agent code is deployed separately from infrastructure:

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```

**Requires explicit human approval** per `CLAUDE.md`. The `Dockerfile` serves
`pm_agent.fast_api_app:app` on port 8080 for container targets.

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
