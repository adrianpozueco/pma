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

"""Entry node of the pm_agent graph: picks the specialist for a question."""

from google.adk.agents.context import Context
from google.adk.workflow import node

# Deterministic placeholder classifier. Keyword matching keeps the routing
# decision free and unit-testable while the graph is being built out; the
# intended upgrade is an LLM classifier node behind the same "ipc"/"bq"
# contract, so only this module changes when that lands.
BQ_KEYWORDS = (
    "how many",
    "how much",
    "count",
    "trend",
    "average",
    "total",
    "work order",
    "workorder",
    "swap",
    "sdr",
    "fleet",
    "aircraft",
)

# Anything the keywords do not claim goes to the manual, so the graph never
# needs a DEFAULT_ROUTE edge - a second edge onto an existing node fails
# graph validation as a duplicate.
DEFAULT_ROUTE_NAME = "ipc"


def _latest_message(ctx: Context) -> str:
    """Return the text of the most recent event that carries content."""
    for event in reversed(ctx.session.events):
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None)
        if not parts:
            continue
        return " ".join(part.text for part in parts if part.text)
    return ""


@node(name="router")
async def router(ctx: Context) -> str:
    """Set ctx.route to the specialist that should answer the question."""
    question = _latest_message(ctx).lower()
    matched = any(word in question for word in BQ_KEYWORDS)
    route = "bq" if matched else DEFAULT_ROUTE_NAME
    ctx.route = route
    return route
