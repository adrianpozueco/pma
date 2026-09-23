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

"""Configuration shared by the root graph and every specialist sub-agent."""

import os

import google.auth
from dotenv import load_dotenv

# Loaded here rather than only in fast_api_app, because config is imported by
# every entrypoint that reaches an agent - the server, adk web, pytest and a
# bare "import pm_agent". Without it, anything but the server would miss the
# settings below. load_dotenv never overrides a variable already in the
# environment, so a real deployment's values still win.
load_dotenv()

MODEL = "gemini-3.8-flash"


def project_id() -> str:
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


def ipc_datastore_id() -> str:
    """Resolve the Vertex AI Search datastore holding the IPC manuals.

    Kept out of the source because the id is environment-specific: it carries a
    console-generated numeric suffix, and a different project has a different
    one. Terraform owns the value (var.knowledge_base_data_store_id), passes it
    to the deployed agent through service.tf, and .env carries it locally.
    """
    value = os.getenv("IPC_DATASTORE_ID")
    if not value:
        raise RuntimeError(
            "IPC_DATASTORE_ID is not set. Copy .env.example to .env and fill "
            "in the Vertex AI Search datastore id, or set the variable in the "
            "environment. Terraform reports it as the "
            "knowledge_base_data_store_id output."
        )
    return value
