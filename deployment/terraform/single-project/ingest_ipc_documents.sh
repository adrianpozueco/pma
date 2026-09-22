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

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
: "${LOCATION:?LOCATION is required}"
: "${DATA_STORE_ID:?DATA_STORE_ID is required}"
: "${METADATA_URI:?METADATA_URI is required}"

# Global datastores are served from the unprefixed host; us and eu are not.
if [[ "${LOCATION}" == "global" ]]; then
  HOST="discoveryengine.googleapis.com"
else
  HOST="${LOCATION}-discoveryengine.googleapis.com"
fi

DATA_STORE="projects/${PROJECT_ID}/locations/${LOCATION}/collections/default_collection/dataStores/${DATA_STORE_ID}"
TOKEN="$(gcloud auth print-access-token)"

auth_curl() {
  curl --silent --show-error --fail-with-body \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-Goog-User-Project: ${PROJECT_ID}" \
    -H "Content-Type: application/json" \
    "$@"
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

CURRENT_SCHEMA="$(auth_curl "https://${HOST}/v1/${DATA_STORE}/schemas/default_schema")"

MERGED_SCHEMA="$(jq -c '
  (.structSchema // {"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"})
  | .properties //= {}
  | .properties.aircraft_type      = {"type":"string","indexable":true,"searchable":true,"retrievable":true,"dynamicFacetable":true}
  | .properties.amos_aircraft_type = {"type":"string","indexable":true,"retrievable":true}
  | .properties.ata_chapter        = {"type":"string","indexable":true,"searchable":true,"retrievable":true,"dynamicFacetable":true}
  | .properties.revision           = {"type":"string","indexable":true,"retrievable":true}
  | .properties.manual_type        = {"type":"string","indexable":true,"retrievable":true}
  | .properties.title              = {"type":"string","keyPropertyMapping":"title","retrievable":true}
  | {structSchema: .}
' <<<"${CURRENT_SCHEMA}")"

auth_curl -X PATCH \
  "https://${HOST}/v1/${DATA_STORE}/schemas/default_schema" \
  -d "${MERGED_SCHEMA}" >/dev/null

# --------------------------------------------------------------------
# 2. Import the documents
# --------------------------------------------------------------------
#
# dataSchema "document" is the gcsSource schema whose lines carry both a GCS
# uri and structData. reconciliationMode FULL makes the datastore's contents
# exactly the JSONL: documents not listed are removed. That is the intent here
# (the JSONL is generated from the repo, so it is the source of truth), but it
# is also why this script must not be pointed at a datastore whose documents
# were loaded some other way.

echo "Importing documents from ${METADATA_URI} into ${DATA_STORE_ID}..."

OPERATION="$(auth_curl -X POST \
  "https://${HOST}/v1/${DATA_STORE}/branches/default_branch/documents:import" \
  -d "$(jq -n --arg uri "${METADATA_URI}" '{
        gcsSource: {inputUris: [$uri], dataSchema: "document"},
        reconciliationMode: "FULL"
      }')")"

OPERATION_NAME="$(jq -r '.name' <<<"${OPERATION}")"
echo "Import started: ${OPERATION_NAME}"

# The import is long running. Poll rather than return immediately, so a
# terraform apply that claims success has actually ingested something.
for _ in $(seq 1 120); do
  STATUS="$(auth_curl "https://${HOST}/v1/${OPERATION_NAME}")"
  if [[ "$(jq -r '.done // false' <<<"${STATUS}")" == "true" ]]; then
    if jq -e '.error' >/dev/null <<<"${STATUS}"; then
      echo "Import failed: $(jq -c '.error' <<<"${STATUS}")" >&2
      exit 1
    fi
    echo "Import finished: $(jq -c '.metadata // {}' <<<"${STATUS}")"
    exit 0
  fi
  sleep 15
done

echo "Import did not finish within 30 minutes; check ${OPERATION_NAME}" >&2
exit 1
