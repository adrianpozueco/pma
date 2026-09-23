#!/usr/bin/env bash
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
#
# Imports the staged IPC manual PDFs into the Vertex AI Search datastore, and
# marks the structData fields the IPC agent filters on as indexable.
#
# There is no Terraform resource for Discovery Engine documents, so this is
# called from a local-exec provisioner in knowledge_base.tf. It is also safe to
# run by hand:
#
#   PROJECT_ID=... LOCATION=global DATA_STORE_ID=ipc-part-numbers_1789998929768 \
#   METADATA_URI=gs://.../ipc-manuals/metadata/ipc_documents.jsonl \
#   bash deployment/terraform/single-project/ingest_ipc_documents.sh
#
# Requires: gcloud (authenticated), curl, jq.

set +x # Never expose access tokens, even when invoked through bash -x.
set -euo pipefail
umask 077

: "${PROJECT_ID:?PROJECT_ID is required}"
: "${LOCATION:?LOCATION is required}"
: "${DATA_STORE_ID:?DATA_STORE_ID is required}"
: "${METADATA_URI:?METADATA_URI is required}"

# INCREMENTAL updates our stable document IDs without deleting other documents.
# FULL is an explicit opt-in for a datastore whose entire corpus we own.
RECONCILIATION_MODE="${RECONCILIATION_MODE:-INCREMENTAL}"
API_MAX_ATTEMPTS="${API_MAX_ATTEMPTS:-10}"
API_RETRY_SECONDS="${API_RETRY_SECONDS:-15}"
OPERATION_MAX_POLLS="${OPERATION_MAX_POLLS:-120}"
OPERATION_POLL_SECONDS="${OPERATION_POLL_SECONDS:-15}"
IMPORT_MAX_ATTEMPTS="${IMPORT_MAX_ATTEMPTS:-3}"
EXPECTED_DOCUMENT_COUNT="${EXPECTED_DOCUMENT_COUNT:-}"

fail() { echo "$*" >&2; exit 1; }
[[ "${PROJECT_ID}" =~ ^([a-z][a-z0-9-]*|[0-9]+)$ ]] || fail "Invalid PROJECT_ID"
[[ "${DATA_STORE_ID}" =~ ^[A-Za-z0-9_-]+$ ]] || fail "Invalid DATA_STORE_ID"
[[ "${LOCATION}" =~ ^(global|us|eu)$ ]] || fail "LOCATION must be global, us or eu"
[[ "${METADATA_URI}" =~ ^gs://[^/]+/.+ ]] || fail "METADATA_URI must identify a GCS object"
[[ "${RECONCILIATION_MODE}" =~ ^(INCREMENTAL|FULL)$ ]] || fail "Invalid RECONCILIATION_MODE"
for setting in API_MAX_ATTEMPTS OPERATION_MAX_POLLS IMPORT_MAX_ATTEMPTS; do
  [[ "${!setting}" =~ ^[1-9][0-9]*$ ]] || fail "${setting} must be positive"
done
for setting in API_RETRY_SECONDS OPERATION_POLL_SECONDS; do
  [[ "${!setting}" =~ ^(0|[1-9][0-9]*)$ ]] || fail "${setting} must be nonnegative"
done
[[ -z "${EXPECTED_DOCUMENT_COUNT}" || "${EXPECTED_DOCUMENT_COUNT}" =~ ^[1-9][0-9]*$ ]] || fail "EXPECTED_DOCUMENT_COUNT must be positive"
for dependency in gcloud curl jq; do
  command -v "${dependency}" >/dev/null || fail "Required command missing: ${dependency}"
done
IPC_TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${IPC_TMP_DIR}"' EXIT

# Global datastores are served from the unprefixed host; us and eu are not.
if [[ "${LOCATION}" == "global" ]]; then
  HOST="discoveryengine.googleapis.com"
else
  HOST="${LOCATION}-discoveryengine.googleapis.com"
fi

DATA_STORE="projects/${PROJECT_ID}/locations/${LOCATION}/collections/default_collection/dataStores/${DATA_STORE_ID}"
# Resource names may use the numeric project number instead of the project ID.
RESOURCE_PREFIX="projects/(${PROJECT_ID}|[0-9]+)/locations/${LOCATION}/collections/default_collection/dataStores/${DATA_STORE_ID}"

api_request() {
  local method="$1" url="$2" payload="${3:-}" token code attempt
  # Keep the array nonempty: macOS Bash 3.2 treats an empty array as unset
  # under nounset, even with the quoted [@] expansion.
  local -a body_args=(--request "${method}")
  if [[ -n "${payload}" ]]; then
    printf '%s' "${payload}" >"${IPC_TMP_DIR}/request.json"
    body_args+=(--data-binary "@${IPC_TMP_DIR}/request.json")
  fi
  for ((attempt=1; attempt<=API_MAX_ATTEMPTS; attempt++)); do
    # Refresh on every request/poll. gcloud caches credentials itself; a long
    # import must not keep using the token acquired before it started.
    token="$(gcloud auth print-access-token 2>/dev/null)" || fail "Could not obtain Google credentials"
    [[ -n "${token}" && "${token}" != *$'\n'* && "${token}" != *$'\r'* ]] || fail "Invalid access token"
    printf 'Authorization: Bearer %s\nX-Goog-User-Project: %s\nContent-Type: application/json\n' \
      "${token}" "${PROJECT_ID}" >"${IPC_TMP_DIR}/headers"
    # A private header file keeps the bearer token out of curl's process args.
    # Do not echo API bodies: errors can include internal document content.
    if code="$(curl --silent --show-error --connect-timeout 10 --max-time 60 \
      --header "@${IPC_TMP_DIR}/headers" \
      --output "${IPC_TMP_DIR}/response.json" --write-out '%{http_code}' \
      "${body_args[@]}" "${url}" 2>"${IPC_TMP_DIR}/curl-error")"; then
      if [[ "${code}" =~ ^2[0-9][0-9]$ ]]; then
        jq -e 'type == "object"' "${IPC_TMP_DIR}/response.json" >/dev/null 2>&1 \
          || fail "API returned an invalid JSON response"
        cat "${IPC_TMP_DIR}/response.json"
        return
      fi
      case "${code}" in
        401|403|404|409|429|500|502|503|504) ;;
        *) fail "Discovery Engine ${method} failed (HTTP ${code})" ;;
      esac
    else
      code="transport error"
    fi
    # Freshly created datastores and service-agent IAM grants are eventually
    # consistent. Bound retries so permanent permission errors still fail.
    if ((attempt == API_MAX_ATTEMPTS)); then
      fail "Discovery Engine ${method} failed after ${attempt} attempts (${code})"
    fi
    echo "Discovery Engine ${method} not ready (${code}); retry ${attempt}/${API_MAX_ATTEMPTS}" >&2
    sleep "${API_RETRY_SECONDS}"
  done
}

validate_schema() {
  local name
  name="$(jq -er '.name | select(type == "string")' <<<"$1" 2>/dev/null)" || fail "Schema response has no name"
  [[ "${name}" =~ ^${RESOURCE_PREFIX}/schemas/default_schema$ ]] || fail "Schema response names a different datastore"
  jq -e '
    if (.structSchema | type) == "object" then .structSchema
    elif (.jsonSchema | type) == "string" then .jsonSchema | fromjson
    else error("Missing schema") end
    | type == "object" and ((.properties // {}) | type == "object")
  ' <<<"$1" >/dev/null 2>&1 || fail "Schema response has no valid schema object"
}

wait_operation() {
  local status="$1" label="$2" operation_name returned_name poll
  operation_name="$(jq -er '.name | select(type == "string")' <<<"${status}" 2>/dev/null)" || fail "${label} did not return an operation name"
  [[ "${operation_name}" =~ ^${RESOURCE_PREFIX}/((schemas/default_schema|branches/(default_branch|0))/)?operations/[A-Za-z0-9_-]+$ ]] \
    || fail "${label} returned an invalid operation name"
  echo "Waiting for ${label}: ${operation_name}" >&2
  for ((poll=0; poll<=OPERATION_MAX_POLLS; poll++)); do
    returned_name="$(jq -r '.name // empty' <<<"${status}")"
    [[ "${returned_name}" == "${operation_name}" ]] || fail "${label} returned a different operation"
    jq -e '(has("done") | not) or (.done | type == "boolean")' <<<"${status}" >/dev/null \
      || fail "${label} returned invalid operation status"
    if jq -e 'has("error")' <<<"${status}" >/dev/null; then
      # GCS access by the newly created Discovery Engine service agent can
      # fail asynchronously while its bucket IAM grant propagates.
      if [[ "${label}" == "Document import" ]] && \
        jq -e '.error.code == 7 or .error.code == 16' <<<"${status}" >/dev/null; then
        return 75
      fi
      fail "${label} operation failed: ${operation_name}"
    fi
    if [[ "$(jq -r '.done // false' <<<"${status}")" == "true" ]]; then
      jq -e '.response | type == "object"' <<<"${status}" >/dev/null \
        || fail "${label} completed without a response"
      printf '%s' "${status}"
      return
    fi
    ((poll < OPERATION_MAX_POLLS)) || fail "${label} did not finish after ${OPERATION_MAX_POLLS} polls: ${operation_name}"
    sleep "${OPERATION_POLL_SECONDS}"
    status="$(api_request GET "https://${HOST}/v1/${operation_name}")" || return "$?"
  done
}

# --------------------------------------------------------------------
# 1. Make the aircraft-type metadata filterable
# --------------------------------------------------------------------
#
# structData alone is retrievable but not filterable: a field has to be marked
# indexable in the datastore schema before `aircraft_type: ANY("737-8200")`
# will match anything. This is a read-modify-write of default_schema rather
# than a blind PATCH, so auto-detected fields are not clobbered.
#
# Deliberately not a google_discovery_engine_schema resource: json_schema is
# ForceNew in hashicorp/google 7.28.0, so any later edit would delete and
# recreate the schema of a datastore that already holds documents.

echo "Patching ${DATA_STORE_ID} default_schema for filterable metadata..."

CURRENT_SCHEMA="$(api_request GET "https://${HOST}/v1/${DATA_STORE}/schemas/default_schema")"
validate_schema "${CURRENT_SCHEMA}"

MERGED_SCHEMA="$(jq -c '
  (if (.structSchema | type) == "object" then .structSchema else .jsonSchema | fromjson end)
  | .properties //= {}
  | .properties.aircraft_type      = ((.properties.aircraft_type // {}) + {"type":"string","indexable":true,"searchable":true,"retrievable":true,"dynamicFacetable":true})
  | .properties.amos_aircraft_type = ((.properties.amos_aircraft_type // {}) + {"type":"string","indexable":true,"retrievable":true})
  | .properties.ata_chapter        = ((.properties.ata_chapter // {}) + {"type":"string","indexable":true,"searchable":true,"retrievable":true,"dynamicFacetable":true})
  | .properties.revision           = ((.properties.revision // {}) + {"type":"string","indexable":true,"retrievable":true})
  | .properties.manual_type        = ((.properties.manual_type // {}) + {"type":"string","indexable":true,"retrievable":true})
  | .properties.title              = ((.properties.title // {}) + {"type":"string","keyPropertyMapping":"title","retrievable":true})
  | {structSchema: .}
' <<<"${CURRENT_SCHEMA}")"

SCHEMA_OPERATION="$(api_request PATCH "https://${HOST}/v1/${DATA_STORE}/schemas/default_schema" "${MERGED_SCHEMA}")"
SCHEMA_STATUS="$(wait_operation "${SCHEMA_OPERATION}" "Schema update")"
validate_schema "$(jq -c '.response' <<<"${SCHEMA_STATUS}")"

# --------------------------------------------------------------------
# 2. Import the documents
# --------------------------------------------------------------------
#
# dataSchema "document" is the gcsSource schema whose lines carry both a GCS
# uri and structData. INCREMENTAL upserts stable IDs. It does not remove older
# revisions or console-imported IDs; use FULL only for deliberate reconciliation
# of a datastore whose complete contents are owned by this metadata file.

echo "Importing documents from ${METADATA_URI} into ${DATA_STORE_ID}..."

IMPORT_PAYLOAD="$(jq -n --arg uri "${METADATA_URI}" --arg mode "${RECONCILIATION_MODE}" '{
  gcsSource: {inputUris: [$uri], dataSchema: "document"},
  reconciliationMode: $mode,
  forceRefreshContent: true
}')"
for ((import_attempt=1; import_attempt<=IMPORT_MAX_ATTEMPTS; import_attempt++)); do
  OPERATION="$(api_request POST "https://${HOST}/v1/${DATA_STORE}/branches/default_branch/documents:import" "${IMPORT_PAYLOAD}")"
  if STATUS="$(wait_operation "${OPERATION}" "Document import")"; then
    # done=true can still mean partial failure. Proto JSON encodes int64
    # counters as strings and omits zero values. Require positive success.
    COUNTS="$(jq -er '
      [(.metadata.successCount // 0), (.metadata.failureCount // 0)]
      | map(tonumber)
      | select(all(.[]; . >= 0 and . == floor))
      | @tsv
    ' <<<"${STATUS}" 2>/dev/null)" || fail "Import returned invalid document counts"
    read -r SUCCESS_COUNT FAILURE_COUNT <<<"${COUNTS}"
    ERROR_SAMPLES="$(jq -er '(.response.errorSamples // []) | if type == "array" then length else error("Invalid error samples") end' <<<"${STATUS}" 2>/dev/null)" \
      || fail "Import returned invalid error samples"
    if [[ "${FAILURE_COUNT}" == "0" && "${ERROR_SAMPLES}" == "0" ]]; then
      [[ "${SUCCESS_COUNT}" != "0" ]] || fail "Import completed without importing any documents"
      [[ -z "${EXPECTED_DOCUMENT_COUNT}" || "${SUCCESS_COUNT}" == "${EXPECTED_DOCUMENT_COUNT}" ]] \
        || fail "Import incomplete: expected ${EXPECTED_DOCUMENT_COUNT} documents, imported ${SUCCESS_COUNT}"
      echo "Import finished: ${SUCCESS_COUNT} documents imported (${RECONCILIATION_MODE})"
      exit 0
    fi
    # Only permission failures are retried here. Malformed documents and other
    # partial failures need correction, not repeated submissions of bad data.
    jq -e '.response.errorSamples | length > 0 and all(.[]; .code == 7 or .code == 16)' <<<"${STATUS}" >/dev/null \
      || fail "Import incomplete: ${SUCCESS_COUNT} succeeded, ${FAILURE_COUNT} failed, ${ERROR_SAMPLES} error samples"
  else
    operation_exit="$?"
    [[ "${operation_exit}" == "75" ]] || exit "${operation_exit}"
  fi
  ((import_attempt < IMPORT_MAX_ATTEMPTS)) || fail "Import permission checks failed after ${IMPORT_MAX_ATTEMPTS} attempts"
  echo "Document import permissions not ready; retry ${import_attempt}/${IMPORT_MAX_ATTEMPTS}" >&2
  sleep "${API_RETRY_SECONDS}"
done
