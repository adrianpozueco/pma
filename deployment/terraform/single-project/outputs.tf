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

output "app_service_account_email" {
  description = "Application service account email"
  value       = google_service_account.app_sa.email
}

output "logs_bucket_name" {
  description = "Logs storage bucket name"
  value       = google_storage_bucket.logs_data_bucket.name
}

output "analytics_dataset_id" {
  description = "BigQuery dataset holding workorder and FAA SDR tables"
  value       = google_bigquery_dataset.analytics.dataset_id
}

output "analytics_data_bucket_name" {
  description = "Bucket staging the analytics source files"
  value       = google_storage_bucket.analytics_data_bucket.name
}

output "knowledge_base_bucket_name" {
  description = "Bucket staging the IPC manual PDFs and their import metadata"
  value       = google_storage_bucket.knowledge_base_bucket.name
}

output "knowledge_base_data_store_id" {
  description = "Vertex AI Search datastore id holding the IPC manuals"
  value       = var.knowledge_base_data_store_id
}

# The full resource path is what VertexAiSearchTool wants; pm_agent currently
# rebuilds it from a hardcoded id in ipc_manual_retrieval/agent.py.
output "knowledge_base_data_store_name" {
  description = "Full Discovery Engine resource name of the IPC datastore"
  value       = local.knowledge_base_data_store_name
}
