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

# ====================================================================
# IPC manual knowledge base (Vertex AI Search / Discovery Engine)
# ====================================================================
#
# One datastore, not one per aircraft type. The two aircraft are kept apart by
# a `structData` field on every document rather than by separate corpora:
#
#   - ADK's VertexAiSearchTool binds to exactly one data_store_id, so two
#     datastores would mean two tools (or a search engine fronting both) and a
#     routing decision taken before retrieval.
#   - structData gives a runtime filter, e.g. filter = 'aircraft_type: ANY("737-8200")',
#     which the agent can apply, relax or omit per question. A second datastore
#     is a deploy-time split that cannot be relaxed, so "which of the two types
#     uses this part" stops being answerable in one call.
#   - The citation format the IPC agent is instructed to emit already needs the
#     ATA chapter and aircraft type as retrievable metadata. Once that metadata
#     exists, a second datastore buys nothing.
#
# Note the two location axes are unrelated: the datastore lives in `global`
# (Discovery Engine only offers global/us/eu), the staging bucket in var.region
# because `constraints/gcp.resourceLocations` rejects anything else.

locals {
  ipc_source_dir = "${local.repo_root}/data/ipc_part_numbers"

  # AMOS type codes are not ICAO codes. Mirrors VARIANT_BY_TYPE in
  # scripts/wo_xml.py so a filter value in the knowledge base is the same
  # string as an aircraft type in the BigQuery workorder tables. Keep the two
  # in step; a divergence silently splits the two halves of an answer.
  aircraft_variant_by_amos_type = {
    "B737-8" = "737-800"
    "M73-82" = "737-8200"
    "B737-7" = "737-700"
    "32S"    = "A320"
  }

  # Discovered from disk rather than listed, so dropping another chapter PDF
  # into data/ipc_part_numbers/<AMOS type>/ is enough to stage it. The layout
  # is <AMOS type>/<ATA chapter>___<revision>.pdf, e.g. M73-82/73-11___042.pdf.
  ipc_pdf_rel_paths = fileset(local.ipc_source_dir, "*/*.pdf")

  ipc_documents = {
    for rel in local.ipc_pdf_rel_paths : rel => {
      amos_type     = dirname(rel)
      aircraft_type = local.aircraft_variant_by_amos_type[dirname(rel)]
      ata_chapter   = split("___", basename(rel))[0]
      revision      = trimsuffix(split("___", basename(rel))[1], ".pdf")

      local_path = "${local.ipc_source_dir}/${rel}"
      # Mirrors the on-disk aircraft-type folders, so the GCS prefix alone
      # still separates the two types even before any metadata is read.
      object_name = "ipc-manuals/${rel}"
    }
  }

  # Document ids are ours, not console-generated, and they are what makes an
  # import repeatable: re-importing the same id updates that document instead
  # of adding a duplicate. Stable across revisions of this config as long as
  # the filename does not change.
  ipc_document_records = {
    for rel, doc in local.ipc_documents : rel => merge(doc, {
      document_id = "ipc-${doc.aircraft_type}-${doc.ata_chapter}-${doc.revision}"

      # The IPC agent reconstructs its citation from the retrieved document
      # title, in the form "Chapter 25-31, 737-6789, AIPC, D638A001-RYR-0137".
      # The trailing manual document number is not derivable from the
      # filenames we have, so the revision stands in for it; set
      # manual_document_number below once the real AIPC numbers are known.
      title = "Chapter ${doc.ata_chapter}, ${doc.aircraft_type}, AIPC, ${doc.revision}"
    })
  }

  ipc_metadata_object_name = "ipc-manuals/metadata/ipc_documents.jsonl"

  knowledge_base_data_store_name = join("/", [
    "projects", var.project_id,
    "locations", var.knowledge_base_location,
    "collections", "default_collection",
    "dataStores", var.knowledge_base_data_store_id,
  ])
}

# ====================================================================
# Staging bucket
# ====================================================================

# Separate from the analytics data bucket for the same reason the logs bucket
# is separate: the Discovery Engine service agent needs read access to the
# manuals, and that grant should not also hand it the workorder corpus.
resource "google_storage_bucket" "knowledge_base_bucket" {
  name                        = "${var.project_id}-${var.project_name}-kb"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_object" "ipc_manual_pdfs" {
  for_each = local.ipc_document_records

  name         = each.value.object_name
  bucket       = google_storage_bucket.knowledge_base_bucket.name
  source       = each.value.local_path
  content_type = "application/pdf"
}

# The metadata JSONL is what makes the aircraft-type separation survive into
# the index. Each line pairs one PDF in GCS with the structData that retrieval
# can filter on; it is imported with dataSchema "document", which is the only
# gcsSource schema that carries structData for unstructured content.
resource "google_storage_bucket_object" "ipc_import_metadata" {
  name         = local.ipc_metadata_object_name
  bucket       = google_storage_bucket.knowledge_base_bucket.name
  content_type = "application/json"

  content = join("\n", concat([
    for rel, doc in local.ipc_document_records : jsonencode({
      id = doc.document_id
      structData = {
        title              = doc.title
        aircraft_type      = doc.aircraft_type
        amos_aircraft_type = doc.amos_type
        ata_chapter        = doc.ata_chapter
        revision           = doc.revision
        manual_type        = "AIPC"
      }
      content = {
        mimeType = "application/pdf"
        uri      = "gs://${google_storage_bucket.knowledge_base_bucket.name}/${doc.object_name}"
      }
    })
  ], [""]))
}

# ====================================================================
# Datastore
# ====================================================================
#
# In this project the datastore already exists and is serving the IPC agent, so
# it is adopted through the import block below rather than created. The count
# guard is for a clean project, where this same config creates it instead.
#
# content_config, industry_vertical, location and data_store_id are all
# ForceNew in hashicorp/google 7.28.0. If any of them disagrees with the live
# datastore, the plan is a destroy/create of a corpus that took a console
# import to build, so prevent_destroy is set deliberately: Terraform will
# refuse the plan instead of quietly replacing it. Remove it consciously, never
# to make a plan go through.
resource "google_discovery_engine_data_store" "ipc" {
  count = var.create_knowledge_base_data_store ? 1 : 0

  project           = var.project_id
  location          = var.knowledge_base_location
  data_store_id     = var.knowledge_base_data_store_id
  display_name      = var.knowledge_base_display_name
  industry_vertical = "GENERIC"
  solution_types    = ["SOLUTION_TYPE_SEARCH"]
  # PDFs are unstructured content the datastore stores and parses itself.
  content_config = "CONTENT_REQUIRED"

  lifecycle {
    prevent_destroy = true

    # document_processing_config is ForceNew and read back from the API, so an
    # empty block here would plan as a replacement of the adopted datastore.
    # Parsing/chunking is left as the console configured it.
    ignore_changes = [document_processing_config]
  }

  depends_on = [google_project_service.services]
}

# Adopts the existing console-created datastore into state. The id carries a
# console-generated numeric suffix (ipc-part-numbers_1789998929768); a
# data_store_id invented here would not match it and would create a second,
# empty datastore alongside the working one, which is why
# var.knowledge_base_data_store_id defaults to the exact existing value.
#
# for_each, rather than a plain import block, so a clean project can set
# adopt_existing_data_store = false and have Terraform create the datastore.
import {
  for_each = var.create_knowledge_base_data_store && var.adopt_existing_data_store ? toset([var.knowledge_base_data_store_id]) : toset([])

  to = google_discovery_engine_data_store.ipc[0]
  id = "projects/${var.project_id}/locations/${var.knowledge_base_location}/collections/default_collection/dataStores/${each.value}"
}

# ====================================================================
# Document ingestion
# ====================================================================
#
# There is no Terraform resource for Discovery Engine documents in
# hashicorp/google 7.28.0 (checked against the provider schema: data_store,
# schema, search_engine, serving_config, target_site and friends exist, no
# document resource). Ingestion is the documents.import REST method, so it is
# driven by a script here rather than faked as a resource.
#
# Disabled by default, and that default matters for this project: the live
# datastore already holds the three PDFs under console-assigned document ids.
# Running this import would add our ids alongside them, duplicating every
# chapter. Enable it only on a datastore whose documents this config owns, or
# after purging the console-imported ones. reconciliationMode is FULL, so once
# enabled the datastore's documents become exactly what the JSONL lists.
resource "terraform_data" "ipc_document_import" {
  count = var.ingest_ipc_documents ? 1 : 0

  # Same idea as the md5 in the BigQuery load job ids: re-run when the staged
  # metadata actually changes, not on every apply.
  triggers_replace = {
    data_store      = local.knowledge_base_data_store_name
    metadata_digest = google_storage_bucket_object.ipc_import_metadata.md5hash
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/ingest_ipc_documents.sh"

    environment = {
      PROJECT_ID    = var.project_id
      LOCATION      = var.knowledge_base_location
      DATA_STORE_ID = var.knowledge_base_data_store_id
      METADATA_URI  = "gs://${google_storage_bucket.knowledge_base_bucket.name}/${local.ipc_metadata_object_name}"
    }
  }

  depends_on = [
    google_storage_bucket_object.ipc_manual_pdfs,
    google_storage_bucket_object.ipc_import_metadata,
    google_storage_bucket_iam_member.discovery_engine_kb_reader,
  ]
}

# ====================================================================
# Access
# ====================================================================

# The Discovery Engine service agent, not the app SA, is what reads the staged
# PDFs during documents.import. google_project_service_identity forces the
# agent to exist before the grant; it is google-beta only, which is the same
# reason apis.tf reaches for google-beta for the Vertex AI agent.
resource "google_project_service_identity" "discovery_engine_sa" {
  provider = google-beta

  project = var.project_id
  service = "discoveryengine.googleapis.com"

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "discovery_engine_kb_reader" {
  bucket = google_storage_bucket.knowledge_base_bucket.name
  role   = "roles/storage.objectViewer"
  member = google_project_service_identity.discovery_engine_sa.member
}
