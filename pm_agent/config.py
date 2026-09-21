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
