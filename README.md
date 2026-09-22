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

## Local development

Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
```

```
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
IPC_DATASTORE_ID=your-datastore-id
```

`IPC_DATASTORE_ID` is the Vertex AI Search datastore behind the IPC specialist.
It is environment specific - the id carries a console-generated numeric suffix -
so it is not hardcoded in the agent. Terraform owns it as
`var.knowledge_base_data_store_id` and reports it as the
`knowledge_base_data_store_id` output; `service.tf` passes it to the deployed
agent. If it is unset, importing the agent fails with a message saying so.

`GOOGLE_CLOUD_LOCATION=global` matters: the IPC datastore lives in `global`, and
a wrong location surfaces as a model 404. `pm_agent/config.py` falls back to
Application Default Credentials when `GOOGLE_CLOUD_PROJECT` is unset, so make
sure `gcloud auth application-default login` has been run.

Install and run:

```bash
agents-cli install
agents-cli playground
```

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
