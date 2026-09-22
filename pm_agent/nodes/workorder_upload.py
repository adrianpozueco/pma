"""Route XML attachments through the shared parser before ordinary chat."""

from typing import Any

from google.adk.agents.context import Context
from google.adk.events import Event
from google.genai import types

from pm_agent.workorders.chat import analyze_chat_upload, render_chat_upload


async def prepare_workorder_upload(ctx: Context) -> Event:
    result = await analyze_chat_upload(ctx)
    if result is None:
        return Event(output={}, route="chat")
    return Event(output=result, route="workorder")


def display_workorder_upload(node_input: dict[str, Any]) -> Event:
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=render_chat_upload(node_input))],
        ),
    )
