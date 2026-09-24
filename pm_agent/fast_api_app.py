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

import contextlib
import os
from collections.abc import AsyncIterator

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from starlette.concurrency import run_in_threadpool

from amos_data.parser import MAX_XML_BYTES
from pm_agent.app_utils import services
from pm_agent.app_utils.a2a import attach_a2a_routes
from pm_agent.app_utils.reasoning_engine_adapter import (
    attach_reasoning_engine_routes,
)
from pm_agent.prediction.service import default_predictor
from pm_agent.workorders import (
    AnalysisInput,
    WorkOrderAnalysisError,
    WorkOrderAnalysisService,
)
from pm_agent.workorders.service import default_history_provider

load_dotenv()
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)
otel_to_cloud = os.environ.get(
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", ""
).lower() in ("true", "1")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from pm_agent.agent import app as adk_app
    from pm_agent.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
app.title = "pm-agent"
app.description = "API for interacting with the Agent pm-agent"

# Proxy routes so the Vertex AI Console Playground (reasoning_engine SDK) can
# talk to this agent alongside the native adk_api routes.
attach_reasoning_engine_routes(app)


class _UploadTooLarge(Exception):
    pass


def _contains_upload_too_large(error: BaseException) -> bool:
    if isinstance(error, _UploadTooLarge):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_upload_too_large(item) for item in error.exceptions)
    return False


@app.middleware("http")
async def limit_workorder_upload_body(request: Request, call_next):
    """Apply the XML byte bound before multipart parsing buffers file bytes."""
    if request.url.path != "/workorders/analyze":
        return await call_next(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_XML_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": {"code": "oversized_input", "max_bytes": MAX_XML_BYTES}},
        )
    receive = request._receive
    total = 0

    async def limited_receive():
        nonlocal total
        message = await receive()
        if message.get("type") == "http.request":
            total += len(message.get("body", b""))
            if total > MAX_XML_BYTES:
                raise _UploadTooLarge()
        return message

    request._receive = limited_receive
    try:
        return await call_next(request)
    except BaseException as exc:
        if not _contains_upload_too_large(exc):
            raise
        return JSONResponse(
            status_code=413,
            content={"detail": {"code": "oversized_input", "max_bytes": MAX_XML_BYTES}},
        )


async def _bounded_request_bytes(request: Request) -> bytes:
    """Read raw XML incrementally; request bodies are never logged or retained."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_XML_BYTES:
        raise WorkOrderAnalysisError(
            "XML input exceeds the upload limit",
            code="oversized_input",
            details={"max_bytes": MAX_XML_BYTES},
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_XML_BYTES:
            raise WorkOrderAnalysisError(
                "XML input exceeds the upload limit",
                code="oversized_input",
                details={"max_bytes": MAX_XML_BYTES},
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _bounded_upload_bytes(request: Request) -> tuple[bytes, str | None]:
    """Accept the single XML file in a multipart form with the same byte limit."""
    form = await request.form(max_files=1, max_fields=20, max_part_size=MAX_XML_BYTES)
    upload = form.get("file") or form.get("xml_file")
    if upload is None or not hasattr(upload, "read"):
        raise WorkOrderAnalysisError(
            "multipart request needs a file or xml_file field",
            code="missing_xml_upload",
        )
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := await upload.read(64 * 1024):
            total += len(chunk)
            if total > MAX_XML_BYTES:
                raise WorkOrderAnalysisError(
                    "XML input exceeds the upload limit",
                    code="oversized_input",
                    details={"max_bytes": MAX_XML_BYTES},
                )
            chunks.append(chunk)
        return b"".join(chunks), getattr(upload, "filename", None)
    finally:
        await upload.close()


@app.post("/workorders/analyze")
async def analyze_workorder_upload(
    request: Request,
    mode: str = Query(...),
    analysis_as_of: str = Query(...),
    selected_wo_id: str | None = Query(None),
    current_aircraft_tac: int | None = Query(None),
    current_tac_source: str | None = Query(None),
    current_tac_observed_at: str | None = Query(None),
    target_part_number: str | None = Query(None),
):
    """Analyze raw XML or one multipart XML upload without invoking the chat model."""
    try:
        if request.headers.get("content-type", "").lower().startswith("multipart/"):
            xml_bytes, source_name = await _bounded_upload_bytes(request)
        else:
            xml_bytes, source_name = await _bounded_request_bytes(request), None
        analysis_input = AnalysisInput(
            mode=mode,
            analysis_as_of=analysis_as_of,
            selected_wo_id=selected_wo_id,
            current_aircraft_tac=current_aircraft_tac,
            current_tac_source=current_tac_source,
            current_tac_observed_at=current_tac_observed_at,
            target_part_number=target_part_number,
        )
        # Construction is intentionally here: credential-free imports and tests
        # do not resolve BigQuery configuration unless the endpoint is used.
        try:
            history_provider = default_history_provider()
        except Exception as exc:
            # Runtime configuration/credentials must not turn an XML upload
            # into an unstructured server error or expose provider details.
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "history_provider_unavailable",
                    "message": "Configured history retrieval is unavailable.",
                },
            ) from exc
        service = WorkOrderAnalysisService(
            history_provider, predictor=default_predictor()
        )
        result = await run_in_threadpool(
            service.analyze_xml, xml_bytes, analysis_input, source_name=source_name
        )
        return result
    except WorkOrderAnalysisError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        ) from exc


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
