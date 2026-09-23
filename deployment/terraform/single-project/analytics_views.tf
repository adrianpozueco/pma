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

locals {
  analytics_view_dir = "${path.module}/../shared/analytics_views"
}

resource "google_bigquery_table" "v_work_orders" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "v_work_orders"
  description         = "Canonical one-row-per-workorder projection with source metadata and quality flags"
  deletion_protection = false

  view {
    query = templatefile("${local.analytics_view_dir}/v_work_orders.sql", {
      project_id = var.project_id
      dataset_id = google_bigquery_dataset.analytics.dataset_id
      wo_table   = google_bigquery_table.wo_workorders.table_id
    })
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.wo_workorders]
}

resource "google_bigquery_table" "v_symptom_records" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "v_symptom_records"
  description         = "Canonical one-row-per-work-step symptom projection; action outcomes remain separate"
  deletion_protection = false

  view {
    query = templatefile("${local.analytics_view_dir}/v_symptom_records.sql", {
      project_id = var.project_id
      dataset_id = google_bigquery_dataset.analytics.dataset_id
      wo_table   = google_bigquery_table.wo_workorders.table_id
    })
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.wo_workorders]
}

resource "google_bigquery_table" "v_component_changes" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "v_component_changes"
  description         = "Canonical one-row-per-component-change projection with both part sides"
  deletion_protection = false

  view {
    query = templatefile("${local.analytics_view_dir}/v_component_changes.sql", {
      project_id = var.project_id
      dataset_id = google_bigquery_dataset.analytics.dataset_id
      wo_table   = google_bigquery_table.wo_workorders.table_id
    })
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.wo_workorders]
}

resource "google_bigquery_table" "v_observed_removals" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "v_observed_removals"
  description         = "All-PN observed outgoing component changes with timing and removal quality flags"
  deletion_protection = false

  view {
    query = templatefile("${local.analytics_view_dir}/v_observed_removals.sql", {
      project_id     = var.project_id
      dataset_id     = google_bigquery_dataset.analytics.dataset_id
      component_view = google_bigquery_table.v_component_changes.table_id
    })
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.v_component_changes]
}

resource "google_bigquery_table" "v_target_replacements" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "v_target_replacements"
  description         = "Three-PN projection of observed removals for serving and final evaluation"
  deletion_protection = false

  view {
    query = templatefile("${local.analytics_view_dir}/v_target_replacements.sql", {
      project_id    = var.project_id
      dataset_id    = google_bigquery_dataset.analytics.dataset_id
      removals_view = google_bigquery_table.v_observed_removals.table_id
    })
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.v_observed_removals]
}

# Retrieval table definitions are intentionally empty of rows. The versioned
# offline document/embedding jobs own inserts and updates; Terraform owns only
# the durable schema and access path.
resource "google_bigquery_table" "retrieval_documents" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "retrieval_documents"
  description         = "Canonical versioned AMOS and FAA text units for historical retrieval"
  deletion_protection = false
  schema              = file("${path.module}/../shared/retrieval_documents_schema.json")

  depends_on = [google_bigquery_dataset.analytics]
}

resource "google_bigquery_table" "retrieval_embeddings" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.analytics.dataset_id
  table_id            = "retrieval_embeddings"
  description         = "Versioned embeddings keyed to canonical retrieval documents"
  deletion_protection = false
  schema              = file("${path.module}/../shared/retrieval_embeddings_schema.json")

  depends_on = [google_bigquery_dataset.analytics, google_bigquery_table.retrieval_documents]
}

# Agent Runtime uses the application service account for BigQuery reads and
# parameterized query jobs. Keep the project-level job role and dataset-level
# data role scoped to that actual runtime identity.
resource "google_project_iam_member" "app_sa_bigquery_job_user" {
  project    = var.project_id
  role       = "roles/bigquery.jobUser"
  member     = "serviceAccount:${google_service_account.app_sa.email}"
  depends_on = [google_project_service.services]
}

resource "google_bigquery_dataset_iam_member" "app_sa_analytics_data_viewer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.analytics.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.app_sa.email}"
  depends_on = [google_bigquery_dataset.analytics]
}
