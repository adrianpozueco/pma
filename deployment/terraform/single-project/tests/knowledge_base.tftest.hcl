# Mock every external provider: these plans require no cloud credentials and
# never run the document importer or create real infrastructure.
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
  project_id = "pma-kb-test-project"
}

run "fresh_project_creates_and_populates_datastore" {
  command = plan

  assert {
    condition     = length(google_discovery_engine_data_store.ipc) == 1 && length(terraform_data.ipc_document_import) == 1
    error_message = "A fresh project must create a datastore and schedule its document import."
  }

  assert {
    condition     = google_discovery_engine_data_store.ipc[0].data_store_id == "ipc-part-numbers" && !var.adopt_existing_data_store
    error_message = "Fresh projects must not try to adopt the lab's console-created datastore."
  }

  assert {
    condition     = length(google_storage_bucket_object.ipc_manual_pdfs) == 3 && output.knowledge_base_document_count == 3
    error_message = "All three tracked IPC PDFs must be staged for the new knowledge base."
  }

  assert {
    condition     = length(terraform_data.ipc_document_import[0].triggers_replace.document_digests) == 3 && length(terraform_data.ipc_document_import[0].triggers_replace.script_digest) == 64
    error_message = "PDF content and importer changes must invalidate the completed import."
  }

  assert {
    condition = alltrue([
      for key, pdf in google_storage_bucket_object.ipc_manual_pdfs :
      pdf.detect_md5hash == filemd5(pdf.source)
    ])
    error_message = "Replacing PDF bytes at the same filename must trigger a storage update."
  }

  assert {
    condition = toset([
      for line in compact(split("\n", google_storage_bucket_object.ipc_import_metadata.content)) :
      jsondecode(line).structData.aircraft_type
    ]) == toset(["737-800", "737-8200"])
    error_message = "The import manifest must preserve both aircraft types."
  }

  assert {
    condition = alltrue([
      for service in ["discoveryengine.googleapis.com", "storage.googleapis.com", "bigqueryconnection.googleapis.com", "compute.googleapis.com"] :
      contains(keys(google_project_service.services), service)
    ]) && contains(var.app_sa_roles, "roles/discoveryengine.viewer")
    error_message = "Fresh projects need the source APIs and runtime retrieval permission."
  }
}

run "existing_project_keeps_adoption_without_import" {
  command = plan
  override_resource {
    target = google_discovery_engine_data_store.ipc[0]
    values = {
      project           = "pma-kb-test-project"
      location          = "global"
      data_store_id     = "ipc-part-numbers_1789998929768"
      display_name      = "ipc-part-numbers"
      industry_vertical = "GENERIC"
      solution_types    = ["SOLUTION_TYPE_SEARCH"]
      content_config    = "CONTENT_REQUIRED"
    }
  }
  variables {
    knowledge_base_data_store_id = "ipc-part-numbers_1789998929768"
    adopt_existing_data_store    = true
    ingest_ipc_documents         = false
  }

  assert {
    condition     = length(google_discovery_engine_data_store.ipc) == 1 && length(terraform_data.ipc_document_import) == 0
    error_message = "Adopting the existing project must not re-import its documents."
  }

  assert {
    condition     = output.knowledge_base_data_store_id == "ipc-part-numbers_1789998929768"
    error_message = "The existing datastore id must remain unchanged."
  }
}

run "unmanaged_datastore_is_not_imported_into" {
  command = plan
  variables {
    create_knowledge_base_data_store = false
  }

  assert {
    condition     = length(google_discovery_engine_data_store.ipc) == 0 && length(terraform_data.ipc_document_import) == 0
    error_message = "Disabling datastore management must also disable document import."
  }
}

run "empty_corpus_cannot_claim_populated_knowledge_base" {
  command = plan
  variables {
    ipc_source_dir = "tests/nonexistent-corpus"
  }
  expect_failures = [terraform_data.ipc_document_import[0]]
}

run "empty_datastore_can_be_requested_explicitly" {
  command = plan
  variables {
    ipc_source_dir       = "tests/nonexistent-corpus"
    ingest_ipc_documents = false
  }

  assert {
    condition     = length(google_discovery_engine_data_store.ipc) == 1 && length(terraform_data.ipc_document_import) == 0 && output.knowledge_base_document_count == 0
    error_message = "Explicitly disabling import should allow provisioning an empty datastore."
  }
}
