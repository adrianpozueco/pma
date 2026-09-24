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
  description = "Vertex AI Search datastore id holding the IPC manual PDFs. Terraform creates the datastore under exactly this id. When adopting an existing datastore, set its exact id, including any console-generated suffix."
  default     = "ipc-part-numbers"
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

  validation {
    condition     = contains(["global", "us", "eu"], var.knowledge_base_location)
    error_message = "Discovery Engine location must be global, us, or eu. The current IPC agent uses global."
  }
}

variable "create_knowledge_base_data_store" {
  type        = bool
  description = "Manage the IPC datastore with Terraform. Set false only when the datastore is owned outside this config."
  default     = true
}

variable "adopt_existing_data_store" {
  type        = bool
  description = "Adopt an existing datastore through the import block instead of creating one. The existing project explicitly enables this in vars/env.tfvars; new projects create a datastore by default."
  default     = false
}

variable "ingest_ipc_documents" {
  type        = bool
  description = "Import the staged IPC PDFs when create_knowledge_base_data_store is true. New projects ingest by default. Keep false when adopting a corpus already imported under different document ids, as configured in vars/env.tfvars."
  default     = true
}

variable "ipc_source_dir" {
  type        = string
  description = "Optional IPC PDF source directory, laid out as <AMOS type>/<ATA chapter>___<revision>.pdf. Defaults to the repository's data/ipc_part_numbers. Use an absolute path, or a path relative to the Terraform working directory."
  default     = null
}

# ====================================================================
# Curated dataset (Plan B synthetic tables)
# ====================================================================

variable "create_curated_tables" {
  type        = bool
  description = "Create the curated dataset and Plan B synthetic tables (wo_embeddings, fct_lead_time_samples, dim_focus_components, dim_reference_set)."
  default     = true
}

variable "load_curated_data" {
  type        = bool
  description = "Upload the local NDJSON files to GCS and run the BigQuery load jobs for the Plan B synthetic tables. Requires create_curated_tables = true and the files to exist under data/processed/."
  default     = true

  validation {
    condition     = !var.load_curated_data || var.create_curated_tables
    error_message = "load_curated_data = true requires create_curated_tables = true."
  }
}

variable "run_curated_real_data_pipeline" {
  type        = bool
  description = "Run the curated BigQuery SQL pipeline against real analytics inputs. Keep false to use the Plan B synthetic curated load path."
  default     = false

  validation {
    condition     = !var.run_curated_real_data_pipeline || var.create_curated_tables
    error_message = "run_curated_real_data_pipeline = true requires create_curated_tables = true."
  }

  validation {
    condition     = !(var.run_curated_real_data_pipeline && var.load_curated_data)
    error_message = "run_curated_real_data_pipeline and load_curated_data cannot both be true. Choose synthetic load OR real-data SQL pipeline execution."
  }
}

variable "curated_sim_threshold" {
  type        = number
  description = "Cosine similarity threshold for Step 06 semantic scoring."
  default     = 0.80
}

variable "curated_k_precursors" {
  type        = number
  description = "Top-K cap per replacement workorder for Step 06 semantic scoring."
  default     = 50
}

variable "curated_embedding_endpoint" {
  type        = string
  description = "BigQuery AI.EMBED endpoint for Step 04 embedding generation."
  default     = "text-embedding-005"
}

variable "curated_llm_endpoint" {
  type        = string
  description = "BigQuery AI.GENERATE endpoint for Step 07 adjudication."
  default     = "gemini-3.8-flash"
}

variable "pma_prediction_enabled" {
  type        = string
  description = "Enable PMA online prediction. Set to 'true' only after the data-quality gate passes and the go-live gate is approved."
  default     = "false"
}
