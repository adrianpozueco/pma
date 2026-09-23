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
# Maintenance analytics dataset
# ====================================================================
#
# Two source tables, both loaded from files built by scripts/ and staged in
# GCS. Neither is partitioned or clustered: at 8.3k and 299 rows the metadata
# overhead would cost more than it saves, and workorder issue dates span
# 2006-2026, which would shatter a DAY-partitioned table into near-empty
# partitions.
#
# Regenerate the staged files before applying:
#   uv run python scripts/xml_to_ndjson.py
#   uv run python scripts/build_faa_sdr_wo_parts.py

locals {
  repo_root                 = "${path.module}/../../.."
  wo_workorders_local_path  = "${local.repo_root}/data/processed/wo_workorders.ndjson.gz"
  faa_sdr_local_path        = "${local.repo_root}/data/processed/faa_sdr_matching_wo_parts.csv"
  wo_workorders_object_name = "workorders/wo_workorders.ndjson.gz"
  faa_sdr_object_name       = "faa-sdr/faa_sdr_matching_wo_parts.csv"
}

resource "google_bigquery_dataset" "analytics" {
  project       = var.project_id
  dataset_id    = replace("${var.project_name}_analytics", "-", "_")
  friendly_name = "${var.project_name} Maintenance Analytics"
  location      = var.region
  description   = "AMOS workorders and matched FAA Service Difficulty Reports"

  depends_on = [google_project_service.services]
}

# Staging bucket for analytics source data. Kept separate from the telemetry
# logs bucket so a lifecycle or IAM change on one cannot affect the other.
resource "google_storage_bucket" "analytics_data_bucket" {
  name                        = "${var.project_id}-${var.project_name}-data"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_object" "wo_workorders_ndjson" {
  name         = local.wo_workorders_object_name
  bucket       = google_storage_bucket.analytics_data_bucket.name
  source       = local.wo_workorders_local_path
  content_type = "application/gzip"
}

resource "google_storage_bucket_object" "faa_sdr_csv" {
  name         = local.faa_sdr_object_name
  bucket       = google_storage_bucket.analytics_data_bucket.name
  source       = local.faa_sdr_local_path
  content_type = "text/csv"
}

# ====================================================================
# Tables
# ====================================================================

# One row per AMOS workorder. Nested rather than split into child tables: in
# BigQuery a REPEATED RECORD is the child table, co-located and reachable with
# UNNEST instead of a join.
resource "google_bigquery_table" "wo_workorders" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "wo_workorders"
  description         = "AMOS transferWorkorder exports, one row per workorder, nested work steps / actions / component changes"
  deletion_protection = false

  schema = file("${path.module}/../shared/wo_workorders_schema.json")
}

# FAA SDR rows whose part number matches a part seen in the workorders. Narrow
# by design: its value is the PartName / PartCondition enrichment that the
# workorder XML never carries.
resource "google_bigquery_table" "faa_sdr_wo_parts" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "faa_sdr_wo_parts"
  description         = "FAA Service Difficulty Reports matching AMOS workorder part numbers (normalised match on part number)"
  deletion_protection = false

  schema = file("${path.module}/../shared/faa_sdr_wo_parts_schema.json")
}

# ====================================================================
# Load jobs
# ====================================================================
#
# google_bigquery_job is immutable, so the job id embeds the md5 of the local
# file. Editing the data produces a new job id and therefore a real reload,
# instead of Terraform reporting no changes against a job that already ran.
# The table's creation time is mixed in too: job ids are never reusable, so a
# recreated table would otherwise hit a 409 or stay empty.

resource "google_bigquery_job" "load_wo_workorders" {
  project  = var.project_id
  job_id   = "load-wo-workorders-${substr(md5("${filemd5(local.wo_workorders_local_path)}-${google_bigquery_table.wo_workorders.creation_time}"), 0, 12)}"
  location = var.region

  load {
    source_uris = [
      "gs://${google_storage_bucket.analytics_data_bucket.name}/${local.wo_workorders_object_name}",
    ]

    destination_table {
      project_id = var.project_id
      dataset_id = google_bigquery_dataset.analytics.dataset_id
      table_id   = google_bigquery_table.wo_workorders.table_id
    }

    source_format     = "NEWLINE_DELIMITED_JSON"
    write_disposition = "WRITE_TRUNCATE"
    autodetect        = false
  }

  depends_on = [google_storage_bucket_object.wo_workorders_ndjson]
}

# resource "google_bigquery_job" "load_faa_sdr_wo_parts" {
#   project  = var.project_id
#   job_id   = "load-faa-sdr-wo-parts-${substr(filemd5(local.faa_sdr_local_path), 0, 12)}"
#   location = var.region
#
#   load {
#     source_uris = [
#       "gs://${google_storage_bucket.analytics_data_bucket.name}/${local.faa_sdr_object_name}",
#     ]
#
#     destination_table {
#       project_id = var.project_id
#       dataset_id = google_bigquery_dataset.analytics.dataset_id
#       table_id   = google_bigquery_table.faa_sdr_wo_parts.table_id
#     }
#
#     source_format         = "CSV"
#     skip_leading_rows     = 1
#     allow_quoted_newlines = true
#     write_disposition     = "WRITE_TRUNCATE"
#     autodetect            = false
#   }
#
#   depends_on = [google_storage_bucket_object.faa_sdr_csv]
# }
#
