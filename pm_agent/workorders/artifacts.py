"""Session-scoped artifact loading for work-order analysis."""

from __future__ import annotations

import inspect
from typing import Any

from amos_data.parser import MAX_XML_BYTES

from .service import WorkOrderAnalysisError

_XML_MIME_TYPES = {"application/xml", "text/xml", "application/octet-stream"}


def validate_artifact_filename(filename: str) -> str:
    """Allow only a single artifact filename, never a user URI or filesystem path."""
    if (
        not filename
        or filename.startswith("user:")
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
    ):
        raise WorkOrderAnalysisError(
            "artifact filename must be a current-session filename",
            code="invalid_artifact_reference",
        )
    return filename


async def load_current_session_artifact(
    tool_context: Any, filename: str, version: int | str | None = None
) -> bytes:
    """Load through ADK's authorized ToolContext only; no path fallback exists."""
    filename = validate_artifact_filename(filename)
    loader = getattr(tool_context, "load_artifact", None)
    if loader is None:
        raise WorkOrderAnalysisError(
            "ToolContext does not support artifact loading", code="artifact_unavailable"
        )
    try:
        value = (
            loader(filename=filename, version=version)
            if version is not None
            else loader(filename=filename)
        )
    except TypeError:
        value = loader(filename, version) if version is not None else loader(filename)
    if inspect.isawaitable(value):
        value = await value
    inline_data = getattr(value, "inline_data", None)
    mime_type = (
        getattr(inline_data, "mime_type", None)
        if inline_data is not None
        else getattr(value, "mime_type", None)
    )
    if mime_type and mime_type.lower().split(";", 1)[0].strip() not in _XML_MIME_TYPES:
        raise WorkOrderAnalysisError(
            "artifact is not an XML document", code="invalid_artifact_mime"
        )
    data = (
        getattr(inline_data, "data", None)
        if inline_data is not None
        else getattr(value, "data", value)
    )
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise WorkOrderAnalysisError(
            "artifact did not contain XML bytes", code="invalid_artifact"
        )
    payload = bytes(data)
    if len(payload) > MAX_XML_BYTES:
        raise WorkOrderAnalysisError(
            "artifact exceeds XML upload limit",
            code="oversized_input",
            details={"max_bytes": MAX_XML_BYTES},
        )
    return payload


async def analyze_current_session_artifact(
    tool_context: Any,
    filename: str,
    request: Any,
    service: Any,
    *,
    version: int | str | None = None,
) -> dict[str, Any]:
    """Load an authorized current-session XML artifact through the shared service."""
    payload = await load_current_session_artifact(tool_context, filename, version)
    return service.analyze_xml(
        payload,
        request,
        source_name=filename,
        artifact_version=str(version) if version is not None else None,
    )
