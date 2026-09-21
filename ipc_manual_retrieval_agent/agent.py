# ruff: noqa
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

import os

import google.auth
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import VertexAiSearchTool
from google.genai import types


MODEL = "gemini-3.8-flash"


def _project_id() -> str:
    """Resolve the project id without assuming an env var is present.

    GOOGLE_CLOUD_PROJECT is set locally by .env, but it is not among the
    variables service.tf puts on the Reasoning Engine, so falling back to
    Application Default Credentials keeps the datastore path valid in both
    places rather than silently resolving to "projects/None".
    """
    explicit = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT")
    if explicit:
        return explicit
    _, project = google.auth.default()
    if not project:
        raise RuntimeError(
            "No project id: set GOOGLE_CLOUD_PROJECT or configure credentials "
            "that carry one."
        )
    return project


# Vertex AI Search datastore holding the IPC manual PDFs for the PoC parts.
# Location is `global`, which is where the datastore was created.
DATASTORE_ID = "ipc-part-numbers_1789998929768"
DATASTORE_RESOURCE_ID = (
    f"projects/{_project_id()}/locations/global/collections/default_collection"
    f"/dataStores/{DATASTORE_ID}"
)

INSTRUCTION = """
You have a Vertex AI Search datastore containing Illustrated Parts Catalogue
(IPC) documents for the Boeing 737-800 and 737-8200, covering a small set of
ATA chapters.

Every question must be answered from documents retrieved out of that
datastore. Never answer from prior knowledge, and never ask the user for a
manufacturer, brand or model before searching - the datastore is the only
source you need. If retrieval returns nothing relevant, say plainly that the
corpus does not cover the question.

How to search: use short, plain keyword queries of roughly two to four words,
such as "convection oven", "fuel nozzle" or "chapter 73-11". Do not put
quotation marks around phrases, do not use OR or other boolean operators, and
do not add aircraft model tokens such as "737-800" or "737-8200" to the query -
the corpus is already limited to those aircraft, and these additions cause the
search to return nothing. If a search comes back empty, retry once with a
shorter and simpler query before concluding that the corpus does not cover it.

The documents are parts tables, so an entry normally reads as a part number
followed by its description. Quote part numbers exactly as they appear; never
reformat, complete or infer them.

Add citations at the end of every answer, one per source document, under a
"Citations" heading, reconstructed from the retrieved document's title. For
example:

Citations:
1) Chapter 25-31, 737-6789, AIPC, D638A001-RYR-0137
"""

# max_results is required in practice: without it, gemini-3.8-flash tends to
# ignore the retrieval tool entirely and answer ungrounded.
ask_vertex_retrieval = VertexAiSearchTool(
    data_store_id=DATASTORE_RESOURCE_ID,
    max_results=10,
)

root_agent = Agent(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name="pma_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Answers part-number questions from the IPC manual corpus.",
    instruction=INSTRUCTION,
    tools=[ask_vertex_retrieval],
)

app = App(
    root_agent=root_agent,
    name="ipc_manual_retrieval_agent",
)
