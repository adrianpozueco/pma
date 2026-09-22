mock_provider "google" {}
mock_provider "google" {
  alias = "api_bootstrap"
}
mock_provider "google" {
  alias = "billing_override"
}
mock_provider "google-beta" {}
mock_provider "random" {}
mock_provider "time" {}

variables {
  project_id = "pma-curated-test-project"
}

run "defaults_create_dataset_tables_and_load_jobs" {
  command = plan

  assert {
    condition = length(google_bigquery_dataset.curated) == 1 && alltrue([
      length(google_bigquery_table.wo_embeddings) == 1,
      length(google_bigquery_table.fct_lead_time_samples) == 1,
      length(google_bigquery_table.dim_focus_components) == 1,
      length(google_bigquery_table.dim_reference_set) == 1,
    ])
    error_message = "Defaults must create the curated dataset and all four Plan B tables."
  }

  assert {
    condition = alltrue([
      length(google_storage_bucket_object.wo_embeddings_ndjson) == 1,
      length(google_storage_bucket_object.fct_lead_time_ndjson) == 1,
      length(google_storage_bucket_object.dim_focus_components_ndjson) == 1,
      length(google_storage_bucket_object.dim_reference_set_ndjson) == 1,
    ])
    error_message = "Defaults must stage all four NDJSON files in GCS."
  }

  assert {
    condition = alltrue([
      length(google_bigquery_job.load_wo_embeddings) == 1,
      length(google_bigquery_job.load_fct_lead_time) == 1,
      length(google_bigquery_job.load_dim_focus_components) == 1,
      length(google_bigquery_job.load_dim_reference_set) == 1,
    ])
    error_message = "Defaults must schedule all four BigQuery load jobs."
  }
}

run "tables_only_skip_data_load" {
  command = plan
  variables {
    load_curated_data = false
  }

  assert {
    condition = length(google_bigquery_dataset.curated) == 1 && alltrue([
      length(google_bigquery_table.wo_embeddings) == 1,
      length(google_bigquery_table.fct_lead_time_samples) == 1,
      length(google_bigquery_table.dim_focus_components) == 1,
      length(google_bigquery_table.dim_reference_set) == 1,
    ])
    error_message = "load_curated_data = false must still create dataset and empty tables."
  }

  assert {
    condition = alltrue([
      length(google_storage_bucket_object.wo_embeddings_ndjson) == 0,
      length(google_storage_bucket_object.fct_lead_time_ndjson) == 0,
      length(google_storage_bucket_object.dim_focus_components_ndjson) == 0,
      length(google_storage_bucket_object.dim_reference_set_ndjson) == 0,
    ])
    error_message = "load_curated_data = false must skip GCS staging."
  }

  assert {
    condition = alltrue([
      length(google_bigquery_job.load_wo_embeddings) == 0,
      length(google_bigquery_job.load_fct_lead_time) == 0,
      length(google_bigquery_job.load_dim_focus_components) == 0,
      length(google_bigquery_job.load_dim_reference_set) == 0,
    ])
    error_message = "load_curated_data = false must skip BigQuery load jobs."
  }
}

run "disabled_creates_no_curated_resources" {
  command = plan
  variables {
    create_curated_tables = false
    load_curated_data     = false
  }

  assert {
    condition = length(google_bigquery_dataset.curated) == 0 && alltrue([
      length(google_bigquery_table.wo_embeddings) == 0,
      length(google_bigquery_table.fct_lead_time_samples) == 0,
      length(google_bigquery_table.dim_focus_components) == 0,
      length(google_bigquery_table.dim_reference_set) == 0,
      length(google_storage_bucket_object.wo_embeddings_ndjson) == 0,
      length(google_bigquery_job.load_wo_embeddings) == 0,
    ])
    error_message = "create_curated_tables = false must provision no Plan B resources."
  }
}

run "load_without_create_is_rejected" {
  command = plan
  variables {
    create_curated_tables = false
    load_curated_data     = true
  }
  expect_failures = [var.load_curated_data]
}
