# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Scaffold for the BigQuery analytics specialist.

This is a placeholder. It is wired up correctly - importable, routable and
pointed at the right dataset - so the root graph can dispatch to it, but the
analytics logic itself is not built yet.

Intended to answer fleet-reliability questions over two tables: AMOS work
orders (component part swaps) and public FAA Service Difficulty Reports for
the part numbers those work orders touch.

Deliberately not implemented yet:
  - Curated SQL tools or BigQuery views over the nested `wo_workorders`
    schema. Today the agent only has the generic toolset, so it writes
    free-form SQL against 211 fields of RECORD/REPEATED structure, which is
    both slow and easy to get wrong. A follow-up should flatten the parts,
    aircraft and dates into views and expose those instead.
  - Any evaluation of answer quality against known-good results.
"""

import google.auth
from google.adk.agents import Agent
from google.adk.integrations.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.integrations.bigquery.config import BigQueryToolConfig, WriteMode
from google.adk.models import Gemini
from google.adk.workflow import node
from google.genai import types

from pm_agent.config import MODEL, project_id

# Named here rather than only inside the prompt so the graph, future curated
# SQL tools and any IAM wiring can all refer to the same constants.
BQ_DATASET = "pma_agent_analytics"
BQ_LOCATION = "us-central1"
WORKORDERS_TABLE = "wo_workorders"
FAA_SDR_TABLE = "faa_sdr_wo_parts"

INSTRUCTION = f"""
You answer questions from BigQuery views in the `{BQ_DATASET}` dataset,
and from nothing else:

- `v_work_orders`: AMOS work orders parsed from `transferWorkorder` XML,
  covering component part swaps on Boeing 737-800 and 737-8200 aircraft.
- `v_symptom_records`: Work-order step descriptions and component changes,
  flattened for analysis.
- `v_component_changes`: Individual part removals and installations from
  work orders.
- `v_observed_removals`: Work orders grouped by removed part and removal
  reason class.
- `v_target_replacements`: Historical replacements for legacy focus part
  numbers.

You do not have access to curated prediction artifacts. If a question requires
lead-time samples, component focus sets, or pre-calculated embeddings, you
cannot answer it from the available views.

These views are derived from AMOS data (component part swaps) and public FAA
Service Difficulty Reports. FAA SDR records are PUBLIC third-party reports,
so never join them into an AMOS aircraft installation timeline just because a
narrative or a part number looks similar. You may compare them and report them
side by side; you may never merge them into a single asset history.

Every number you give must come from a query you actually ran. Report the
view you read it from and the row count the query returned. Do not estimate,
extrapolate or fill gaps from prior knowledge. If a question cannot be
answered from these views, say so plainly instead of approximating.
"""

# Read-only: WriteMode.BLOCKED is the most restrictive setting the toolset
# offers, and this agent has no reason to create even session-scoped tables.
# Pinning project and location keeps queries off unrelated data.
_tool_config = BigQueryToolConfig(
    write_mode=WriteMode.BLOCKED,
    compute_project_id=project_id(),
    location=BQ_LOCATION,
)

# Application Default Credentials: locally the developer's gcloud login, on
# Reasoning Engine the service account. Passing the credentials directly means
# no end-user OAuth flow, which is what a server-side analytics agent wants.
_credentials, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/bigquery"],
)
# search_catalog is excluded on purpose. It is Dataplex-backed semantic
# discovery for finding tables you cannot name, and it takes project_id as a
# model-supplied argument - in testing the model guessed "pma-agent" from the
# dataset name and the call failed with a Dataplex 403. This agent already
# knows its two tables, so dropping the tool removes both the wrong-project
# failure and a dependency on an API the project has not enabled.
_TOOL_FILTER = [
    "get_dataset_info",
    "get_table_info",
    "list_dataset_ids",
    "list_table_ids",
    "get_job_info",
    "execute_sql",
    "forecast",
    "analyze_contribution",
    "detect_anomalies",
    "ask_data_insights",
]

bigquery_toolset = BigQueryToolset(
    tool_filter=_TOOL_FILTER,
    credentials_config=BigQueryCredentialsConfig(credentials=_credentials),
    bigquery_tool_config=_tool_config,
)

bq_analytics_agent = Agent(
    name="bq_analytics",
    # See the note in the IPC specialist: single_turn is required for a node
    # that follows the router, and include_contents must be explicit to keep
    # the conversation history.
    mode="single_turn",
    include_contents="default",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Queries AMOS work-order and FAA service-difficulty tables in "
        "BigQuery. Placeholder - generic SQL access only."
    ),
    instruction=INSTRUCTION,
    tools=[bigquery_toolset],
)

bq_analytics_node = node(bq_analytics_agent, name="bq_analytics")
