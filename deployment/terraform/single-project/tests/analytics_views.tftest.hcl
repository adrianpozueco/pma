# Mocked plan contract for the canonical analytics slice. No resources are
# applied and staged full-FAA bytes are deliberately optional until the
# offline builder has run.
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
  project_id = "pma-analytics-test-project"
}

run "canonical_views_and_retrieval_tables" {
  command = plan

  assert {
    condition = alltrue([
      for table in [
        google_bigquery_table.v_work_orders,
        google_bigquery_table.v_symptom_records,
        google_bigquery_table.v_component_changes,
        google_bigquery_table.v_observed_removals,
        google_bigquery_table.v_target_replacements,
        google_bigquery_table.v_faa_component_evidence,
      ] : table.table_id != ""
    ])
    error_message = "All canonical maintenance and FAA views must be declared."
  }

  assert {
    condition     = google_bigquery_table.retrieval_documents.table_id == "retrieval_documents" && google_bigquery_table.retrieval_embeddings.table_id == "retrieval_embeddings"
    error_message = "Retrieval tables must remain canonical and Terraform-owned."
  }

  assert {
    condition     = google_project_iam_member.app_sa_bigquery_job_user.role == "roles/bigquery.jobUser" && google_bigquery_dataset_iam_member.app_sa_analytics_data_viewer.role == "roles/bigquery.dataViewer"
    error_message = "Runtime service account needs query-job and analytics dataset read grants."
  }
}
