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

import logging
from typing import Literal

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.models import Gemini
from google.adk.workflow import node
from google.genai import types
from pydantic import BaseModel, Field

from pm_agent.config import MODEL

logger = logging.getLogger(__name__)

# Route keys. These must match the routing map in pm_agent/agent.py.
IPC_ROUTE = "ipc"
BQ_ROUTE = "bq"

# Fallback only. A fixed keyword list cannot cover open-ended phrasing - it
# sent "which parts fail most often?" to the manual because no keyword hit -
# so classification is the model's job now and these words are just the
# safety net for when that call fails.
BQ_KEYWORDS = (
    "how many",
    "how much",
    "count",
    "trend",
    "average",
    "total",
    "most often",
    "per aircraft",
    "work order",
    "workorder",
    "swap",
    "sdr",
    "fleet",
)


class RouteDecision(BaseModel):
    """Structured output of the classifier agent."""

    route: Literal["ipc", "bq"] = Field(
        description=(
            "'ipc' for questions about part numbers, part descriptions or "
            "manual content. 'bq' for questions about counts, rates, trends "
            "or history across work orders and service difficulty reports."
        )
    )


CLASSIFIER_INSTRUCTION = """
Classify the user's question into exactly one route.

Answer "ipc" when the question is about what a part is or what its number is:
looking up a part number, a part description, or anything else that is read
out of an Illustrated Parts Catalogue page.

Answer "bq" when answering needs aggregation over maintenance history: counts,
rates, totals, trends, rankings such as "which parts fail most often", or any
comparison across aircraft, work orders or service difficulty reports.

If the question could go either way, prefer "bq" when it asks how often, how
many or which is worst, and "ipc" when it asks what something is or what it is
called.
"""

route_classifier = Agent(
    name="route_classifier",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description="Chooses which specialist should answer a question.",
    instruction=CLASSIFIER_INSTRUCTION,
    output_schema=RouteDecision,
)


def _latest_user_message(ctx: Context) -> str:
    """Return the text of the most recent genuine user turn.

    Only events authored by "user" count. Specialists running as workflow
    nodes append their own synthetic user-role events to the session, so
    matching on role alone would let the router classify its own plumbing.
    """
    for event in reversed(ctx.session.events):
        if event.author != "user":
            continue
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None)
        if not parts:
            continue
        text = " ".join(part.text for part in parts if part.text).strip()
        if text:
            return text
    return ""


def _keyword_route(question: str) -> str:
    """Fallback classification used when the classifier call fails."""
    lowered = question.lower()
    return BQ_ROUTE if any(w in lowered for w in BQ_KEYWORDS) else IPC_ROUTE


# rerun_on_resume is required because this node calls ctx.run_node: the
# check is on the CALLING node, since a dynamically scheduled child can be
# interrupted and the workflow re-runs its parent to collect the result.
@node(name="router", rerun_on_resume=True)
async def router(ctx: Context) -> str:
    """Pick the specialist and hand it the user's question.

    ctx.route selects the edge; the return value becomes the chosen node's
    node_input, which an LlmAgent node injects as its user turn. Returning the
    route string here is what made the specialist receive the bare word "ipc"
    instead of the question, so the question itself is returned instead.
    """
    question = _latest_user_message(ctx)
    if not question:
        ctx.route = IPC_ROUTE
        return ""

    try:
        decision = await ctx.run_node(
            route_classifier,
            node_input=question,
            use_sub_branch=True,
        )
        route = RouteDecision.model_validate(decision).route
    except Exception as exc:  # Routing must never break the turn.
        route = _keyword_route(question)
        logger.warning(
            "Route classification failed (%s), fell back to keywords: %s",
            type(exc).__name__,
            exc,
        )

    ctx.route = route
    return question
