"""Exercise the real ingestion shell script with fully offline CLI responses."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "deployment/terraform/single-project/ingest_ipc_documents.sh"
)
STORE = "projects/test-project/locations/global/collections/default_collection/dataStores/test-store"
SCHEMA_NAME = f"{STORE}/schemas/default_schema"
SCHEMA_OPERATION = f"{STORE}/operations/schema-update"
IMPORT_OPERATION = f"{STORE}/branches/0/operations/import-documents"
SECRET = "never-print-this-token"
SCHEMA = {
    "type": "object",
    "required": ["existing"],
    "properties": {
        "existing": {"type": "string", "retrievable": True},
        "aircraft_type": {"type": "string", "description": "Keep this description"},
    },
}


def schema_response(encoding="structSchema"):
    return {
        "name": SCHEMA_NAME,
        encoding: json.dumps(SCHEMA) if encoding == "jsonSchema" else SCHEMA,
    }


def import_done(**overrides):
    return {
        "name": IMPORT_OPERATION,
        "done": True,
        "metadata": {"successCount": "3"},
        "response": {},
        **overrides,
    }


def response(method, suffix, body, status=200):
    return {"method": method, "suffix": suffix, "body": body, "status": status}


def happy_responses(encoding="structSchema"):
    return [
        response("GET", "/schemas/default_schema", schema_response(encoding)),
        response("PATCH", "/schemas/default_schema", {"name": SCHEMA_OPERATION}),
        response(
            "GET", "/operations/schema-update", {"name": SCHEMA_OPERATION}
        ),
        response(
            "GET",
            "/operations/schema-update",
            {"name": SCHEMA_OPERATION, "done": True, "response": schema_response()},
        ),
        response(
            "POST", "/documents:import", {"name": IMPORT_OPERATION}
        ),
        response("GET", "/operations/import-documents", import_done()),
    ]


@pytest.fixture
def run_ingestion(tmp_path):
    """Mock binaries, not script internals; unexpected network requests fail."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock = f"#!{sys.executable}\n" + r'''
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["IPC_MOCK_ROOT"])
command = Path(sys.argv[0]).name
if command == "gcloud":
    assert sys.argv[1:] == ["auth", "print-access-token"]
    count_path = root / "tokens"
    count = int(count_path.read_text()) + 1 if count_path.exists() else 1
    count_path.write_text(str(count))
    print("never-print-this-token-" + str(count))
elif command == "curl":
    args = sys.argv[1:]
    def option(name):
        return args[args.index(name) + 1]
    calls_path = root / "calls.json"
    calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
    replies = json.loads((root / "replies.json").read_text())
    if len(calls) >= len(replies):
        sys.exit(90)
    reply = replies[len(calls)]
    method, url = option("--request"), args[-1]
    assert method == reply["method"], (method, reply)
    assert url.endswith(reply["suffix"]), (url, reply)
    header_arg = option("--header")
    assert header_arg.startswith("@")
    headers_path = Path(header_arg[1:])
    assert headers_path.stat().st_mode & 0o077 == 0
    headers = headers_path.read_text()
    assert "X-Goog-User-Project: test-project" in headers
    assert not any("never-print-this-token" in arg for arg in args)
    call = {"method": method, "url": url}
    call["auth_index"] = int(headers.splitlines()[0].rsplit("-", 1)[1])
    if "--data-binary" in args:
        body_arg = option("--data-binary")
        assert body_arg.startswith("@")
        call["payload"] = json.loads(Path(body_arg[1:]).read_text())
    calls.append(call)
    calls_path.write_text(json.dumps(calls))
    body = reply["body"]
    Path(option("--output")).write_text(body if isinstance(body, str) else json.dumps(body))
    print(reply["status"], end="")
elif command == "sleep":
    pass
else:
    sys.exit(91)
'''
    for name in ("gcloud", "curl", "sleep"):
        command = bin_dir / name
        command.write_text(mock)
        command.chmod(0o700)

    def run(replies, **overrides):
        (tmp_path / "replies.json").write_text(json.dumps(replies))
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "IPC_MOCK_ROOT": str(tmp_path),
            "PROJECT_ID": "test-project",
            "LOCATION": "global",
            "DATA_STORE_ID": "test-store",
            "METADATA_URI": "gs://manual-bucket/ipc-manuals/metadata.jsonl",
            "API_MAX_ATTEMPTS": "3",
            "API_RETRY_SECONDS": "0",
            "OPERATION_MAX_POLLS": "3",
            "OPERATION_POLL_SECONDS": "0",
            "IMPORT_MAX_ATTEMPTS": "3",
            "EXPECTED_DOCUMENT_COUNT": "3",
            "RECONCILIATION_MODE": "INCREMENTAL",
            **overrides,
        }
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert SECRET not in result.stdout + result.stderr
        calls_path = tmp_path / "calls.json"
        calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
        return result, calls

    return run


@pytest.mark.parametrize("encoding", ["structSchema", "jsonSchema"])
def test_waits_for_schema_and_preserves_fields(run_ingestion, encoding):
    result, calls = run_ingestion(happy_responses(encoding))
    assert result.returncode == 0, result.stderr
    assert "3 documents imported (INCREMENTAL)" in result.stdout
    patched = calls[1]["payload"]["structSchema"]
    assert patched["required"] == ["existing"]
    assert patched["properties"]["existing"] == SCHEMA["properties"]["existing"]
    assert patched["properties"]["aircraft_type"]["description"] == "Keep this description"
    assert patched["properties"]["aircraft_type"]["indexable"] is True
    assert [call["method"] for call in calls] == ["GET", "PATCH", "GET", "GET", "POST", "GET"]
    assert [call["auth_index"] for call in calls] == list(range(1, 7))
    assert calls[4]["payload"]["reconciliationMode"] == "INCREMENTAL"
    assert calls[4]["payload"]["forceRefreshContent"] is True


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 503])
def test_retries_api_and_iam_readiness(run_ingestion, status):
    replies = [response("GET", "/schemas/default_schema", {"error": {}}, status)]
    result, calls = run_ingestion(replies + happy_responses())
    assert result.returncode == 0, result.stderr
    assert len(calls) == 7
    assert "retry 1/3" in result.stderr


def test_permanent_permissions_failure_is_bounded(run_ingestion):
    replies = [response("GET", "/schemas/default_schema", {"error": {}}, 403)] * 3
    result, calls = run_ingestion(replies)
    assert result.returncode != 0
    assert len(calls) == 3
    assert "failed after 3 attempts" in result.stderr


@pytest.mark.parametrize("body", [
    {},
    {"name": SCHEMA_NAME, "jsonSchema": "not json"},
    {"name": SCHEMA_NAME, "structSchema": {"properties": []}},
    {"name": SCHEMA_NAME + "/wrong", "structSchema": SCHEMA},
    "not a json response",
])
def test_invalid_schema_never_starts_a_patch(run_ingestion, body):
    result, calls = run_ingestion([response("GET", "/schemas/default_schema", body)])
    assert result.returncode != 0
    assert len(calls) == 1


@pytest.mark.parametrize("operation", [
    {},
    {"name": "https://attacker.example/token"},
    {"name": f"{STORE}/operations/../../other"},
    {"name": SCHEMA_OPERATION, "done": "true"},
    {"name": SCHEMA_OPERATION, "done": True, "error": {"code": 13}},
])
def test_bad_schema_operation_never_imports(run_ingestion, operation):
    replies = happy_responses()[:2]
    replies[1]["body"] = operation
    result, calls = run_ingestion(replies)
    assert result.returncode != 0
    assert [call["method"] for call in calls] == ["GET", "PATCH"]


@pytest.mark.parametrize("finished,error", [
    (import_done(metadata={"successCount": "2", "failureCount": "1"}), "Import incomplete"),
    (import_done(response={"errorSamples": [{"code": 3}]}), "Import incomplete"),
    (import_done(metadata={}), "without importing any documents"),
    (import_done(metadata={"successCount": "2"}), "expected 3 documents"),
    (import_done(metadata={"successCount": "NaN"}), "invalid document counts"),
    (import_done(error={"code": 13}), "failed"),
])
def test_finished_import_must_have_complete_success(run_ingestion, finished, error):
    replies = happy_responses()
    replies[-1]["body"] = finished
    result, _ = run_ingestion(replies)
    assert result.returncode != 0
    assert error in result.stderr


def test_operation_timeout_is_bounded(run_ingestion):
    replies = happy_responses()[:3]
    replies.append(replies[-1])
    result, calls = run_ingestion(replies, OPERATION_MAX_POLLS="2")
    assert result.returncode != 0
    assert "did not finish after 2 polls" in result.stderr
    assert len(calls) == 4


def test_full_reconciliation_requires_explicit_override(run_ingestion):
    result, calls = run_ingestion(happy_responses(), RECONCILIATION_MODE="FULL")
    assert result.returncode == 0, result.stderr
    assert calls[4]["payload"]["reconciliationMode"] == "FULL"


@pytest.mark.parametrize("location", ["us", "eu"])
def test_regional_endpoints_and_numeric_project_operation_names(run_ingestion, location):
    replies = json.loads(
        json.dumps(happy_responses())
        .replace("/locations/global/", f"/locations/{location}/")
        .replace("projects/test-project/", "projects/123456/")
    )
    result, calls = run_ingestion(replies, LOCATION=location)
    assert result.returncode == 0, result.stderr
    assert all(
        call["url"].startswith(f"https://{location}-discoveryengine.googleapis.com/")
        for call in calls
    )


@pytest.mark.parametrize("permission_failure", [
    import_done(error={"code": 7}),
    import_done(
        metadata={"failureCount": "3"},
        response={"errorSamples": [{"code": 7}]},
    ),
])
def test_retries_service_agent_permission_failures_inside_import(run_ingestion, permission_failure):
    replies = happy_responses()
    replies[-1]["body"] = permission_failure
    replies.extend(happy_responses()[-2:])
    result, calls = run_ingestion(replies)
    assert result.returncode == 0, result.stderr
    assert sum(call["method"] == "POST" for call in calls) == 2
    assert "Document import permissions not ready; retry 1/3" in result.stderr


def test_async_permission_retries_are_bounded(run_ingestion):
    replies = happy_responses()
    replies[-1]["body"] = import_done(error={"code": 7})
    replies.extend(replies[-2:] * 2)
    result, calls = run_ingestion(replies)
    assert result.returncode != 0
    assert "Import permission checks failed after 3 attempts" in result.stderr
    assert sum(call["method"] == "POST" for call in calls) == 3
