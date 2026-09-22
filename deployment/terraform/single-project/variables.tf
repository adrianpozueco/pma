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

variable "project_name" {
  type        = string
  description = "Project name used as a base for resource naming"
  default     = "pma-agent"
}

variable "project_id" {
  type        = string
  description = "Google Cloud Project ID for resource deployment."
}

variable "region" {
  type        = string
  description = "Google Cloud region for resource deployment."
  default     = "us-central1"
}

variable "telemetry_logs_filter" {
  type        = string
  description = "Log Sink filter for capturing telemetry data. Captures logs with the `traceloop.association.properties.log_type` attribute set to `tracing`."
  default     = "labels.service_name=\"pma-agent\" labels.type=\"agent_telemetry\""
}

variable "app_sa_roles" {
  description = "List of roles to assign to the application service account"
  type        = list(string)
  default = [

    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/storage.admin",
    "roles/serviceusage.serviceUsageConsumer",
    # Read access to the Vertex AI Search datastore behind the IPC agent.
    # iam.tf grants this list to both the app SA and the Vertex AI service
    # agent, and the deployed Reasoning Engine needs it on both paths.
    "roles/discoveryengine.viewer",
  ]
}

# ====================================================================
# IPC manual knowledge base
# ====================================================================

variable "knowledge_base_data_store_id" {
  type        = string
  description = "Vertex AI Search datastore id holding the IPC manual PDFs. The numeric suffix is console-generated and must match the existing datastore exactly, otherwise Terraform creates a second, empty one."
  default     = "ipc-part-numbers_1789998929768"
}

variable "knowledge_base_display_name" {
  type        = string
  description = "Display name of the IPC manual datastore."
  default     = "ipc-part-numbers"
}

variable "knowledge_base_location" {
  type        = string
  description = "Discovery Engine location for the datastore. Unrelated to var.region: the only accepted values are global, us and eu."
  default     = "global"
}

variable "create_knowledge_base_data_store" {
  type        = bool
  description = "Manage the IPC datastore with Terraform. Set false only when the datastore is owned outside this config."
  default     = true
}

variable "adopt_existing_data_store" {
  type        = bool
  description = "Adopt an already existing datastore through the import block instead of creating one. True for this project, false for a clean project."
  default     = true
}

variable "ingest_ipc_documents" {
  type        = bool
  description = "Run documents.import for the staged IPC PDFs. Off by default because the live datastore already holds them under console-assigned document ids, so importing ours would duplicate every chapter."
  default     = false
}
