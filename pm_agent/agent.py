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

"""Root graph for pm_agent.

    START -> router -+-> "ipc" -> ipc_manual_retrieval
                     +-> "bq"  -> bq_analytics

The router is the only entry point; it classifies the question and hands the
whole turn to exactly one specialist, which terminates the run. Specialists do
not call each other, so adding a domain means adding a leaf and a route key.
"""

from google.adk.apps import App
from google.adk.workflow import START, Workflow

from pm_agent.nodes.router import router
from pm_agent.sub_agents.bq_analytics.agent import bq_analytics_node
from pm_agent.sub_agents.ipc_manual_retrieval.agent import (
    ipc_manual_retrieval_node,
)

root_agent = Workflow(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name="pm_agent",
    description=(
        "Routes maintenance questions to an IPC manual specialist or a "
        "BigQuery analytics specialist."
    ),
    edges=[
        (
            START,
            router,
            {"ipc": ipc_manual_retrieval_node, "bq": bq_analytics_node},
        ),
    ],
)

app = App(
    root_agent=root_agent,
    name="pm_agent",
)
